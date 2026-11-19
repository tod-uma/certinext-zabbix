"""Metric collection and Zabbix trapper push for CertiNext DCV monitoring.

Computes the DCV health metrics pushed to Zabbix by the
``certinext-zabbix-push`` CLI (:mod:`certinext_zabbix.zabbix_push_cli`)
and wraps the send through the Zabbix sender (trapper) protocol via
``zabbix_utils``. The item keys here must match ``CertiNext DCV by Zabbix
trapper`` on the Zabbix server (source of truth:
``templates/template_certinext/7.0/template_certinext.yaml`` in this repo).
Keys are bare (no ``[env]`` parameter): each CertiNext environment has its
own Zabbix host, and the CLI sends :data:`KEY_ENV` every run so the template
can catch a run routed to the wrong host. Zabbix's trapper does not expand
macros in item keys, so the environment cannot live in the key itself
(docs/plans/env-hosts-dns-persist.md).

Three metric families, matching the three designed checks:

- **Domain-list metrics** (cheap, one API list call): total domain count and
  how many are unverified (ACTIVE but not DCV-VERIFIED). Pushed every run.
- **Expiry metrics** (one DCV-method call per scoped domain plus one detail
  call per verified domain), split by DCV method (:func:`fetch_dcv_methods`):
  for token-method domains, how many expire within the renewal lead time
  and the minimum days left; for ``DNS-PERSIST`` domains, only the minimum
  days left of the reuse period, plus an LLD list of their apexes so Zabbix
  agent items can check the ``_validation-persist`` TXT record itself, and
  the record value the CA expects there, so the template can catch a stale
  ``{$CERTINEXT.PERSIST.EXPECTED}`` (:func:`persist_expected_value`).
  Pushed only when the caller opts in (daily run).
- **Order-health metrics** (one unfiltered Orders Report list call, split
  client-side by :func:`bucket_orders`): orders the CA never issued a
  certificate for, orders whose certificate was generated but never
  downloaded, orders that recently failed, days since the last certificate
  was issued, and certificates expiring within a lead time. Pushed only
  when the caller opts in (daily run) — see :func:`collect_order_metrics`
  for why the thresholds are deliberately conservative given the vendor's
  free-text status fields, and :func:`fetch_orders` for why the vendor's
  server-side ``status`` filter is unusable here.
"""

import json
import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple

import httpx
import structlog
from certinext import filter_needs_dcv
from certinext.cli_support import log_caught_exception
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import DcvInfo, Domain
from certinext.orders import OrderAccessor, OrderRecord
from zabbix_utils import ItemValue, Sender
from zabbix_utils.exceptions import ProcessingError
from zabbix_utils.types import TrapperResponse

log = structlog.get_logger()

KEY_TOTAL = "certinext.domains.total"
KEY_UNVERIFIED = "certinext.domains.unverified"
KEY_EXPIRING = "certinext.dcv.expiring"
KEY_MIN_DAYS_LEFT = "certinext.dcv.min_days_left"
KEY_ORDERS_UNISSUED = "certinext.orders.unissued"
KEY_ORDERS_UNDOWNLOADED = "certinext.orders.undownloaded"
KEY_ORDERS_FAILED_RECENT = "certinext.orders.failed_recent"
KEY_ORDERS_DAYS_SINCE_ISSUED = "certinext.orders.days_since_issued"
KEY_ORDERS_EXPIRING = "certinext.orders.expiring"
KEY_ENV = "certinext.env"
KEY_PERSIST_DISCOVERY = "certinext.dcv.persist.discovery"
KEY_TOKEN_DOMAINS = "certinext.dcv.token_domains"
KEY_PERSIST_MIN_DAYS_LEFT = "certinext.dcv.persist_min_days_left"
KEY_PERSIST_APEXES = "certinext.dcv.persist_apexes"
KEY_PERSIST_EXPECTED = "certinext.dcv.persist_expected"

ENV_PROD = "prod"
ENV_SANDBOX = "sandbox"

# DcvInfo.method value (upper-cased by certinext) for SSL.com's dns-persist
# DCV. Every other method is a token method: a one-time challenge whose
# validTill really is the deadline. A persist domain's validTill is only a
# reuse clock — the CA re-reads the _validation-persist TXT record at the
# next issuance — so its record, not its date, is what has to be watched.
DCV_METHOD_PERSIST = "DNS-PERSIST"

MetricValue = int | float | str
"""A trapper item value: numeric metrics, plus the TEXT env and LLD items."""

_SECONDS_PER_DAY = 86400

# OrderRecord.order_status values used to bucket the orders report.
#
# These are the only values we classify positively; everything else is
# treated as a failure (see :func:`bucket_orders`). We deliberately do NOT
# filter server-side on the vendor's ``status`` param: 5 of its 6 documented
# ``pending-*`` values return HTTP 422 (sysadmin/certinext-zabbix#3, proven
# in certinext's test_probe_r16_pending_substatus_rejected against both
# sandbox and prod), so a status-filtered pending fetch cannot work at all.
# ``order_status`` is the field certinext's own docs designate as the
# reliable programmatic check — unlike the free-text ``certificate_status``
# display strings, which comparing against an enum verbatim was the
# pre-1.1.0 certinext bug this design avoids repeating.
ORDER_STATUS_FULFILLED = "Order Fulfilled"
ORDER_STATUS_ACCEPTED = "Order Accepted"
ORDER_STATUS_IN_PROGRESS = "Order In-Progress"

# Terminal, but routine administrative work rather than a failure — see
# docs/adr/0010-cancelled-orders-are-not-failures.md. Cancelling an order is
# something an operator does on purpose, so it must not reach
# certinext.orders.failed_recent.
ORDER_STATUS_CANCELLED = "Order Cancelled"

# Statuses meaning "the order is still moving" — not terminal either way.
# Both are split again on certificate presence by :func:`bucket_orders`.
#
# ``Order In-Progress`` was observed in prod on 2026-08-07 and until then
# fell through to the failed catch-all, which was wrong in two directions:
# it inflated failed_recent, and it kept a genuinely stuck order out of
# unissued where the stuck-age trigger would have found it
# (sysadmin/certinext-zabbix#5).
ORDER_STATUSES_IN_FLIGHT = frozenset({ORDER_STATUS_ACCEPTED, ORDER_STATUS_IN_PROGRESS})


class DomainScope(str, Enum):
    """Which domains a run should operate on.

    See :func:`scope_domains` for the semantics of each member.
    """

    TOP = "top"
    NS_BOUNDARY = "ns-boundary"
    ALL = "all"


def scope_domains(domains: Sequence[Domain], scope: DomainScope) -> list[Domain]:
    """Filter *domains* down to the set a given scope should monitor.

    Reuses :func:`certinext.filter_needs_dcv` — the same account-hierarchy
    filter ``certinext-top-domains``/``dcv-update`` already rely on — rather
    than a new domain-parsing implementation. See
    ``docs/adr/0007-domain-scope-reuses-filter-needs-dcv.md`` for why.

    Args:
        domains: Domains as returned by the CertiNext list endpoint.
        scope: :data:`DomainScope.TOP` excludes any domain with a registered
            ancestor in *domains* (pure string-suffix match, no DNS).
            :data:`DomainScope.NS_BOUNDARY` does the same but re-includes a
            domain that has its own NS records (a real DNS zone cut), even
            when a registered ancestor exists. :data:`DomainScope.ALL`
            returns *domains* unchanged.

    Returns:
        The filtered domain list (or *domains* unchanged for
        :data:`DomainScope.ALL`).
    """
    if scope is DomainScope.ALL:
        return list(domains)
    all_domains = list(domains)
    return filter_needs_dcv(all_domains, all_domains, check_ns=(scope is DomainScope.NS_BOUNDARY))


def refresh_domain(d: Domain, *, attempts: int = 3, retry_delay: float = 5.0) -> None:
    """Refresh one domain's detail, retrying a transient API failure.

    Mirrors :func:`push_metrics`'s retry idiom: a single flaky domain
    (timeout, rate limit, transient API error) shouldn't abort an entire
    run's expiry check. Only the same exception types already treated as an
    expected, well-typed failure mode by the caller are retried — a bare
    ``Exception`` is not a documented failure mode of ``Domain.refresh()``
    and is re-raised immediately, unchanged from before this function
    existed.

    Args:
        d: The domain to refresh in place.
        attempts: Total tries (>= 1) before the exception is re-raised.
        retry_delay: Seconds to wait between tries.

    Raises:
        CertiNextAPIError: On the final attempt, if every retry also failed.
        httpx.HTTPError: Same, for a transport-level failure.
    """
    for attempt in range(1, attempts + 1):
        try:
            d.refresh()
            return
        except (CertiNextAPIError, httpx.HTTPError):
            if attempt >= attempts:
                raise
            log.warning(
                "Domain refresh failed — retrying",
                domain=d.name, attempt=attempt, attempts=attempts, retry_delay=retry_delay,
            )
            time.sleep(retry_delay)


class DcvMethodSplit(NamedTuple):
    """Domains split by DCV method, as produced by :func:`fetch_dcv_methods`.

    *failed* holds the domains whose method could not be read after
    retries. A non-empty *failed* means *persist* and *token* are both
    incomplete, so a caller must not publish anything derived from them
    (an LLD list missing an apex would delete that apex's items).

    *persist_values* runs parallel to *persist*: each persist domain's
    DNS-PERSIST record value as :func:`persist_record_value` normalised it,
    ``""`` when the CA offered none. Reading it never moves a domain to
    *failed*, so it can't withhold discovery (certinext-zabbix#12).
    """

    persist: list[Domain]
    token: list[Domain]
    failed: list[Domain]
    persist_values: list[str]


def fetch_dcv_methods(
    domains: Sequence[Domain], *, attempts: int = 3, retry_delay: float = 5.0,
) -> DcvMethodSplit:
    """Read each domain's DCV method and split *domains* by it.

    The domain list endpoint does not carry the method; only
    :meth:`Domain.get_dcv` does, one call per domain. A transient failure is
    retried with the same idiom and exception types as
    :func:`refresh_domain`. A domain still failing after its retries — or
    raising anything else, which is not a documented failure mode of
    ``get_dcv()`` — is logged once and lands in ``failed`` rather than
    aborting the run, so the caller can decide what is still safe to send.

    Args:
        domains: The scoped domain list (any DCV status — a domain still
            pending DCV on dns-persist needs its TXT record watched too).
        attempts: Total tries per domain (>= 1).
        retry_delay: Seconds to wait between tries.

    Returns:
        The split, each list in input order. ``DNS-PERSIST`` domains go to
        ``persist``; every other method (``DNS-TXT``, ``HTTP-URL``,
        ``DNS-CNAME``, …) goes to ``token``. Each persist domain's record
        value comes from the same ``get_dcv()`` response, so it costs no
        extra API call.
    """
    split = DcvMethodSplit(persist=[], token=[], failed=[], persist_values=[])
    for d in domains:
        try:
            info = _get_dcv(d, attempts=attempts, retry_delay=retry_delay)
        except (CertiNextAPIError, httpx.HTTPError) as exc:
            # One concise line per domain, as for refresh failures: a
            # degraded API can fail every domain in a run.
            log_caught_exception(
                log, "Failed to read DCV method", exc, level="warning", domain=d.name,
            )
            split.failed.append(d)
            continue
        except Exception as exc:
            log_caught_exception(
                log, "Failed to read DCV method — unexpected error", exc, domain=d.name,
            )
            split.failed.append(d)
            continue
        if info.method == DCV_METHOD_PERSIST:
            split.persist.append(d)
            split.persist_values.append(persist_record_value(info))
        else:
            split.token.append(d)
    return split


def _get_dcv(d: Domain, *, attempts: int, retry_delay: float) -> DcvInfo:
    """Return one domain's DCV info, retrying transient failures.

    Args:
        d: The domain to query.
        attempts: Total tries (>= 1) before the exception is re-raised.
        retry_delay: Seconds to wait between tries.

    Returns:
        The parsed :class:`DcvInfo`; its ``method`` is already upper-cased
        by certinext.

    Raises:
        CertiNextAPIError: On the final attempt, if every retry also failed.
        httpx.HTTPError: Same, for a transport-level failure.
    """
    for attempt in range(1, attempts + 1):
        try:
            return d.get_dcv()
        except (CertiNextAPIError, httpx.HTTPError):
            if attempt >= attempts:
                raise
            log.warning(
                "DCV method read failed — retrying",
                domain=d.name, attempt=attempt, attempts=attempts, retry_delay=retry_delay,
            )
            time.sleep(retry_delay)
    raise AssertionError("unreachable")  # pragma: no cover — loop always returns or raises


def persist_apex_names(persist: Sequence[Domain]) -> list[str]:
    """Return the distinct, sorted apex names the persist discovery is built from.

    :func:`persist_discovery_value` and the :data:`KEY_PERSIST_APEXES` count
    both come from this, so the count always equals the number of
    discovery rows.

    Args:
        persist: Every DNS-PERSIST domain in scope
            (:attr:`DcvMethodSplit.persist`).

    Returns:
        Sorted, de-duplicated domain names; nameless records are dropped.
    """
    # Domain.name is Optional in the model; a nameless record has no TXT
    # name to look up, so it cannot become a discovery row.
    return sorted({d.name for d in persist if d.name})


def persist_discovery_value(persist: Sequence[Domain]) -> str:
    """Build the LLD JSON for :data:`KEY_PERSIST_DISCOVERY`.

    One ``{#APEX}`` row per distinct domain name, sorted so the value is
    stable between runs. The template's agent item prototypes look up
    ``_validation-persist.{#APEX}`` from it. Callers must pass the
    *complete* persist list: Zabbix treats an apex missing from the value
    as a lost resource and deletes its items after the rule's grace period.

    Args:
        persist: Every DNS-PERSIST domain in scope
            (:attr:`DcvMethodSplit.persist`).

    Returns:
        A JSON array, e.g. ``[{"{#APEX}": "example.edu"}]``; ``[]`` when there
        are none, which tells Zabbix the set is genuinely empty.
    """
    return json.dumps([{"{#APEX}": name} for name in persist_apex_names(persist)])


def normalise_persist_value(raw: str | None) -> str:
    """Normalise a dns-persist record value to the macro's unquoted form.

    ``{$CERTINEXT.PERSIST.EXPECTED}`` holds the value without its TXT quotes
    (the template's ``find()`` adds them), so the value sent for the drift
    trigger must be bare too. The API returns it bare today; one pair of
    surrounding double quotes is stripped anyway, so a CA that starts
    quoting it doesn't open a false drift alert.

    Args:
        raw: The wire ``recordValue``, or ``None``.

    Returns:
        The trimmed value with one pair of surrounding quotes removed;
        ``""`` for ``None`` or blank.
    """
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


def persist_record_value(info: DcvInfo) -> str:
    """Return the normalised DNS-PERSIST record value from one DCV response.

    Prefers the ``DNS-PERSIST`` entry in :attr:`DcvInfo.options`, the same
    option ``dcv-update`` publishes from. Only when the payload lists no
    options at all does it fall back to the selected option's top-level
    :attr:`DcvInfo.record_value` (the persist one, since only persist
    domains reach this). Options listed without a persist entry contradict
    the method, so the top-level value, which may be another option's
    token, is not trusted. Never raises: a missing value is ``""``, which
    :func:`persist_expected_value` refuses to send.

    Args:
        info: A ``get_dcv()`` result whose method is ``DNS-PERSIST``.

    Returns:
        The value as :func:`normalise_persist_value` leaves it, or ``""``.
    """
    options = getattr(info, "options", None) or []
    opt = next((o for o in options if getattr(o, "method", "") == DCV_METHOD_PERSIST), None)
    if opt is not None:
        raw = opt.record_value
    elif not options:
        raw = getattr(info, "record_value", "")
    else:
        raw = ""
    return normalise_persist_value(raw if isinstance(raw, str) else None)


class PersistExpectedError(ValueError):
    """The persist domains don't yield one usable record value to send.

    Raised by :func:`persist_expected_value` when a value is missing or the
    apexes disagree. The caller sends nothing for
    :data:`KEY_PERSIST_EXPECTED` (fail closed) and the item's ``nodata()``
    trigger reports the gap.

    Attributes:
        apexes_by_value: Each distinct value (``""`` for missing) mapped to
            the sorted domain names that returned it, for the log.
    """

    def __init__(self, message: str, apexes_by_value: dict[str, list[str]]) -> None:
        """Store the message and the per-value breakdown.

        Args:
            message: Why no value can be sent.
            apexes_by_value: Distinct value to the domains that returned it.
        """
        super().__init__(message)
        self.apexes_by_value = apexes_by_value


def persist_expected_value(persist: Sequence[Domain], values: Sequence[str]) -> str | None:
    """Return the one dns-persist record value the CA expects, for the drift check.

    The value is per CertiNext account, so every persist apex in one
    environment should report the same string. ``{$CERTINEXT.PERSIST.EXPECTED}``
    is one value per host (ADR 0014), so this never picks a winner among
    different values: it fails closed instead (certinext-zabbix#12).

    Args:
        persist: The persist domains (:attr:`DcvMethodSplit.persist`).
        values: Their normalised values, parallel to *persist*
            (:attr:`DcvMethodSplit.persist_values`).

    Returns:
        The shared value, or ``None`` when there are no persist domains
        (nothing to compare; not an error).

    Raises:
        PersistExpectedError: When any domain has no value, or the domains
            report more than one distinct value.
        ValueError: When *persist* and *values* differ in length.
    """
    by_value: dict[str, list[str]] = {}
    for d, v in zip(persist, values, strict=True):
        by_value.setdefault(v, []).append(d.name or "(unnamed)")
    by_value = {v: sorted(names) for v, names in by_value.items()}
    if not by_value:
        return None
    if "" in by_value:
        raise PersistExpectedError(
            "CA returned no DNS-PERSIST record value for some persist domains", by_value,
        )
    if len(by_value) > 1:
        raise PersistExpectedError(
            "persist domains report different DNS-PERSIST record values", by_value,
        )
    (value,) = by_value
    return value


def verified_domains(domains: Sequence[Domain]) -> list[Domain]:
    """Return the ACTIVE, DCV-verified subset of *domains*.

    These are the domains whose DCV expiry is worth checking — unverified
    domains have no ``validTill`` and are already counted by
    :func:`collect_domain_metrics`.

    Args:
        domains: Domains as returned by the CertiNext list endpoint.

    Returns:
        Domains with ``status == "ACTIVE"`` and ``dcv_status == "VERIFIED"``.
    """
    return [d for d in domains if d.status == "ACTIVE" and d.dcv_status == "VERIFIED"]


def collect_domain_metrics(domains: Sequence[Domain]) -> dict[str, MetricValue]:
    """Compute the domain-list metrics from a CertiNext domain listing.

    ``certinext.domains.total`` guards against a silently empty/truncated
    list (a zero here means the API returned nothing — see the vendor
    pagination/search history in the certinext repo); an
    ``unverified == 0`` alone would look healthy in that failure mode.

    Args:
        domains: Domains as returned by the CertiNext list endpoint.

    Returns:
        Mapping of Zabbix item key to value for :data:`KEY_TOTAL` and
        :data:`KEY_UNVERIFIED`.
    """
    return {
        KEY_TOTAL: len(domains),
        KEY_UNVERIFIED: sum(1 for d in domains if d.needs_dcv),
    }


def _days_left(domains: Sequence[Domain], now: datetime) -> list[float]:
    """Return each known DCV expiry in *domains* as fractional days from *now*.

    Args:
        domains: Refreshed domains; those with ``dcv_expires is None`` are
            skipped.
        now: Reference time (timezone-aware).

    Returns:
        Days left per domain with a known expiry; negative once lapsed.
    """
    return [
        (d.dcv_expires - now).total_seconds() / _SECONDS_PER_DAY
        for d in domains
        if d.dcv_expires is not None
    ]


def collect_expiry_metrics(
    token_verified: Sequence[Domain],
    persist_verified: Sequence[Domain],
    expiry_days: int,
    *,
    now: datetime | None = None,
) -> dict[str, MetricValue]:
    """Compute the DCV-expiry metrics from refreshed verified domains.

    The caller must have refreshed each domain first — the CertiNext list
    endpoint does not include ``validTill``; only the per-domain detail
    endpoint does. Domains whose expiry is unknown (``dcv_expires is None``)
    are excluded from every metric.

    The two DCV method families are measured separately because their
    ``validTill`` means different things. For a token method it is the
    real deadline, so :data:`KEY_EXPIRING` and :data:`KEY_MIN_DAYS_LEFT`
    count token domains only. For ``DNS-PERSIST`` it is just a reuse clock
    that lapses every ~10 days by design, and the CA re-checks the TXT
    record at the next issuance; alerting on it would fire every cycle, so
    persist domains feed only :data:`KEY_PERSIST_MIN_DAYS_LEFT`, which the
    template turns into an INFORMATION event.

    :data:`KEY_TOKEN_DOMAINS` is the number of token domains with a known
    expiry, i.e. exactly the set :data:`KEY_MIN_DAYS_LEFT` is computed
    over. The template gates the days-left triggers on it being above zero,
    so when the last token domain goes away the triggers stop evaluating
    the stale ``last()`` of a ``min_days_left`` that is no longer sent.

    Args:
        token_verified: Refreshed ACTIVE+VERIFIED token-method domains.
        persist_verified: Refreshed ACTIVE+VERIFIED ``DNS-PERSIST`` domains.
        expiry_days: Renewal lead time in days; a token domain counts as
            expiring when its DCV expiry falls within this window
            (already-expired included).
        now: Reference time for the day math (timezone-aware). Defaults to
            the current UTC time; injectable for tests.

    Returns:
        Mapping with :data:`KEY_EXPIRING` and :data:`KEY_TOKEN_DOMAINS`
        (always present); :data:`KEY_MIN_DAYS_LEFT` when at least one
        token expiry is known; :data:`KEY_PERSIST_MIN_DAYS_LEFT` when at
        least one persist expiry is known. Days are floats rounded to two
        places and may be negative once lapsed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now + timedelta(days=expiry_days)
    token_days = _days_left(token_verified, now)
    persist_days = _days_left(persist_verified, now)
    metrics: dict[str, MetricValue] = {
        KEY_EXPIRING: sum(
            1 for d in token_verified if d.dcv_expires is not None and d.dcv_expires <= cutoff
        ),
        KEY_TOKEN_DOMAINS: len(token_days),
    }
    if token_days:
        metrics[KEY_MIN_DAYS_LEFT] = round(min(token_days), 2)
    if persist_days:
        metrics[KEY_PERSIST_MIN_DAYS_LEFT] = round(min(persist_days), 2)
    return metrics


class OrderBuckets(NamedTuple):
    """The orders report split into the buckets the metrics need.

    Produced by :func:`bucket_orders` and consumed by
    :func:`collect_order_metrics`. The names deliberately avoid the word
    "pending": the vendor's ``status`` param uses ``pending-*`` for the
    whole pre-fulfillment space, which spans both :attr:`unissued` and
    :attr:`undownloaded`, so reusing it for either half alone would
    mislead anyone cross-referencing the API docs.

    :attr:`cancelled` is tracked separately from :attr:`failed` and feeds
    no metric today — it exists so deliberately-cancelled orders stay
    countable in the run log instead of vanishing, and so a data-only
    metric can be added later without re-plumbing the bucketing. See
    ``docs/adr/0010-cancelled-orders-are-not-failures.md``.
    """

    unissued: list[OrderRecord]
    undownloaded: list[OrderRecord]
    failed: list[OrderRecord]
    issued: list[OrderRecord]
    cancelled: list[OrderRecord]


def fetch_orders(
    orders: OrderAccessor,
    *,
    since: date | None = None,
    attempts: int = 3,
    retry_delay: float = 5.0,
) -> list[OrderRecord]:
    """Fetch the orders report unfiltered, with retries.

    Deliberately passes no ``status`` filter. The vendor's ``status`` param
    rejects 5 of its 6 documented ``pending-*`` values with HTTP 422
    (sysadmin/certinext-zabbix#3), so any status-filtered pending query
    fails outright; and ``pending-approval`` alone catches only a fraction
    of genuinely-pending orders (5 of 17 in a 100-row prod sample). One
    unfiltered pass plus :func:`bucket_orders` is both correct and cheaper
    — it replaces what were three separate status-filtered fetches.

    Pages are handled internally by :meth:`OrderAccessor.get_list`; this
    only adds the same retry idiom as :func:`refresh_domain`.

    Args:
        orders: The session's order accessor (``sess.orders``).
        since: Optional start date (inclusive) bounding the report, passed
            through to :meth:`OrderAccessor.get_list`. Bounds unbounded
            growth of the fetch as order history accumulates; see the
            ``--order-history-days`` CLI flag for why a multi-year horizon
            is safe for every metric derived from these records.
        attempts: Total tries (>= 1) before re-raising.
        retry_delay: Seconds to wait between tries.

    Returns:
        Every order record in the (optionally date-bounded) report.

    Raises:
        CertiNextAPIError: On the final attempt.
        httpx.HTTPError: Same, for a transport-level failure.
    """
    for attempt in range(1, attempts + 1):
        try:
            return orders.get_list(since=since)
        except (CertiNextAPIError, httpx.HTTPError):
            if attempt >= attempts:
                raise
            log.warning(
                "Order fetch failed — retrying",
                attempt=attempt, attempts=attempts, retry_delay=retry_delay,
            )
            time.sleep(retry_delay)
    # Unreachable: the loop either returns or re-raises on the final attempt.
    raise AssertionError("fetch_orders exhausted its retry loop without returning")


def bucket_orders(records: Sequence[OrderRecord]) -> OrderBuckets:
    """Split *records* on ``order_status`` and certificate presence.

    :data:`ORDER_STATUS_FULFILLED`, the :data:`ORDER_STATUSES_IN_FLIGHT`
    members and :data:`ORDER_STATUS_CANCELLED` are classified positively;
    every other non-empty ``order_status`` falls into
    :attr:`~OrderBuckets.failed`. That catch-all is deliberate — the
    vendor's rejected/expired/revoked orders have never been observed and
    their ``order_status`` strings are undocumented. Treating an unknown
    terminal status as a failure surfaces it on the failed-recent metric
    rather than silently dropping it from every bucket. Unrecognized
    values are logged once per run so drift is visible.

    Cancellations are terminal but **not** failures: cancelling an order
    is routine administrative work, and every ``failed`` row observed in
    production has been a cancellation, so alerting on them would have put
    the failed-recent trigger in PROBLEM 83% of the measured period while
    signalling nothing. They go to :attr:`~OrderBuckets.cancelled`, which
    feeds no metric. See
    ``docs/adr/0010-cancelled-orders-are-not-failures.md``.

    An in-flight order is split again on whether a certificate exists for
    it, because the two halves need different remediation: an order the CA
    never issued is chased through the issuance workflow (approval, DCV,
    CSR), whereas an issued-but-unfetched certificate is a delivery/
    automation failure — someone has a cert they paid for and never
    deployed.

    That split keys on ``certificate_expiry_date`` being populated, *not*
    on the ``certificate_status`` display string. The date is a typed field
    that only exists once the CA has generated a certificate; the display
    string is vendor free text, and comparing it against an enum verbatim
    was the pre-1.1.0 certinext bug this design avoids repeating. The two
    agree perfectly across the full 158-row prod+sandbox corpus: every
    ``"Certificate Generated"`` / ``"Certificate Downloaded"`` row has the
    date set, and every other row has it null.

    Records with no ``order_status`` at all are counted nowhere (they carry
    no signal either way) and logged.

    Args:
        records: Order records as returned by :func:`fetch_orders`.

    Returns:
        An :class:`OrderBuckets` quadruple. Order within each bucket
        follows *records*.
    """
    buckets = OrderBuckets(
        unissued=[], undownloaded=[], failed=[], issued=[], cancelled=[],
    )
    unrecognized: set[str] = set()
    missing_status = 0
    for record in records:
        status = record.order_status
        if status == ORDER_STATUS_FULFILLED:
            buckets.issued.append(record)
        elif status in ORDER_STATUSES_IN_FLIGHT:
            if record.certificate_expiry_date is not None:
                buckets.undownloaded.append(record)
            else:
                buckets.unissued.append(record)
        elif status == ORDER_STATUS_CANCELLED:
            buckets.cancelled.append(record)
        elif status:
            unrecognized.add(status)
            buckets.failed.append(record)
        else:
            missing_status += 1
    if buckets.cancelled:
        log.info(
            "Cancelled orders excluded from the failed bucket",
            count=len(buckets.cancelled),
        )
    if unrecognized:
        log.info(
            "Bucketed orders with unrecognized order_status as failed",
            statuses=sorted(unrecognized),
        )
    if missing_status:
        log.warning(
            "Skipped orders with no order_status — counted in no bucket",
            count=missing_status,
        )
    return buckets


def collect_order_metrics(
    buckets: OrderBuckets,
    *,
    failing_lookback_days: int,
    cert_expiry_days: int,
    now: datetime | None = None,
) -> dict[str, MetricValue]:
    """Compute the order-health metrics from pre-bucketed orders.

    Deliberately avoids ``OrderRecord.certificate_status`` — a vendor
    free-text display string. :func:`bucket_orders` has already classified
    on ``order_status`` and ``certificate_expiry_date``, both typed and
    confirmed reliable against live data (see
    :class:`~certinext.models.orders.OrderRecord`); nothing here
    re-inspects either status field.

    The unissued and undownloaded counts are pushed raw, current-state,
    with no age filtering here: the Zabbix side applies the age threshold
    via ``min()`` window triggers, the same pattern already used for
    ``certinext.domains.unverified``. The failed-recent count *is*
    date-filtered here, because it has no equivalent Zabbix-side history to
    filter on — a rejected/expired/revoked order is a terminal vendor-side
    event timestamped by ``order_date``, not something that stays
    continuously true in Zabbix's own item history. Cancellations are
    excluded from the bucket entirely (see :func:`bucket_orders`), so they
    never reach this count.

    The expiring-soon count spans both cert-bearing buckets (*issued* and
    *undownloaded*) — a generated certificate expires on the CA's schedule
    whether or not anyone ever fetched it. It is a distinct signal from
    ``certinext.dcv.expiring`` (:func:`collect_expiry_metrics`): that
    metric tracks DCV *verification* expiry, this tracks the certificate's
    own expiry per the order record, independent of DCV state.
    Days-since-issued likewise spans both, since it measures whether the CA
    is still issuing at all, not whether we collected the result.

    Args:
        buckets: The report split by :func:`bucket_orders`.
        failing_lookback_days: Only failed orders whose ``order_date``
            falls within this many days of *now* count toward
            :data:`KEY_ORDERS_FAILED_RECENT` — an older failure is normal
            history, not something to alert on forever.
        cert_expiry_days: A cert-bearing order counts toward
            :data:`KEY_ORDERS_EXPIRING` when its ``certificate_expiry_date``
            falls within this many days of *now* (already-expired
            included) — the same lead-time semantics as
            :func:`collect_expiry_metrics`'s ``expiry_days``.
        now: Reference time for the day math (timezone-aware). Defaults to
            the current UTC time; injectable for tests.

    Returns:
        Mapping with :data:`KEY_ORDERS_UNISSUED`,
        :data:`KEY_ORDERS_UNDOWNLOADED`, :data:`KEY_ORDERS_FAILED_RECENT`
        and :data:`KEY_ORDERS_EXPIRING` (always present) and, when at least
        one cert-bearing order carries an ``order_date``,
        :data:`KEY_ORDERS_DAYS_SINCE_ISSUED` (float days).
    """
    now = now or datetime.now(timezone.utc)
    failing_cutoff = now - timedelta(days=failing_lookback_days)
    expiry_cutoff = now + timedelta(days=cert_expiry_days)
    # Both buckets hold orders the CA has generated a certificate for; only
    # the download step differs, which cert expiry doesn't care about.
    with_cert = [*buckets.issued, *buckets.undownloaded]
    metrics: dict[str, MetricValue] = {
        # No re-filtering: bucket_orders already classified on the two
        # typed fields the vendor's data supports checking programmatically.
        KEY_ORDERS_UNISSUED: len(buckets.unissued),
        KEY_ORDERS_UNDOWNLOADED: len(buckets.undownloaded),
        KEY_ORDERS_FAILED_RECENT: sum(
            1 for o in buckets.failed
            if o.order_date is not None and o.order_date >= failing_cutoff
        ),
        KEY_ORDERS_EXPIRING: sum(
            1 for o in with_cert
            if o.certificate_expiry_date is not None and o.certificate_expiry_date <= expiry_cutoff
        ),
    }
    issued_dates = [o.order_date for o in with_cert if o.order_date is not None]
    if issued_dates:
        days_since = (now - max(issued_dates)).total_seconds() / _SECONDS_PER_DAY
        metrics[KEY_ORDERS_DAYS_SINCE_ISSUED] = round(days_since, 2)
    return metrics


def push_metrics(
    metrics: dict[str, MetricValue],
    *,
    zabbix_host: str,
    server: str,
    port: int,
    timeout: int = 10,
    attempts: int = 3,
    retry_delay: float = 5.0,
) -> TrapperResponse:
    """Send *metrics* to the Zabbix server via the sender (trapper) protocol.

    Values are stringified per the protocol; one TCP exchange sends all
    items. Transport failures (connection refused, timeout, unparsable
    response) are retried up to *attempts* total tries — a lost datapoint
    otherwise ages into a false ``nodata()`` alert on the server side.
    A non-zero ``failed`` count in the response is **not** retried: the
    server accepted the connection but rejected item values (unknown host,
    unknown item key, or the sender not matching the item's allowed-hosts
    list), which is a configuration problem a retry cannot fix — the caller
    must treat it as an error.

    Args:
        metrics: Mapping of Zabbix trapper item key to value.
        zabbix_host: Host name exactly as registered in Zabbix (the
            "Host name" field, not the visible name).
        server: Zabbix server (trapper) address.
        port: Zabbix trapper port (normally 10051).
        timeout: Socket timeout in seconds.
        attempts: Total send tries (>= 1) before the transport error is
            re-raised.
        retry_delay: Seconds to wait between tries.

    Returns:
        The trapper response (``processed`` / ``failed`` / ``total`` counts).

    Raises:
        zabbix_utils.exceptions.ProcessingError: When the server response
            cannot be obtained or parsed on the final attempt, or when a
            proxy-group redirect loop exhausted the recursion limit (see below).
        OSError: When the connection fails at the socket level on the final
            attempt.
    """
    items = [ItemValue(zabbix_host, key, str(value)) for key, value in sorted(metrics.items())]
    for attempt in range(1, attempts + 1):
        try:
            # A fresh Sender per attempt — the previous one's socket state is
            # unknown after a failure.
            return Sender(server=server, port=port, timeout=timeout).send(items)
        except RecursionError as exc:
            # zabbix_utils follows a Zabbix 7.0 proxy-group redirect by calling
            # Sender.__send_to_cluster recursively, with no hop limit and no
            # loop detection. A proxy that redirects to its own address — which
            # a misconfigured proxy group really does, observed 965 times in one
            # second against lv-o-zabbix-proxy03 — recurses until the stack ends.
            #
            # Not retried, and deliberately re-raised as ProcessingError rather
            # than left as RecursionError: retrying replays the whole connection
            # storm against an already-struggling proxy, and the caller's
            # existing ProcessingError branch gives a clean exit instead of an
            # unhandled stack. Today RecursionError escapes the retry loop
            # anyway (it is not a ProcessingError or OSError), so this changes
            # the message rather than the control flow — but it pins that, and
            # replaces a traceback whose innermost frames are unrelated to the
            # cause with something an operator can act on.
            raise ProcessingError(
                f"Redirect loop sending to {server}:{port} — the Zabbix endpoint "
                "kept redirecting to an address that redirects back to itself, "
                "until the recursion limit was hit. Check the proxy group's "
                "per-proxy 'Address for active agents' settings, or point "
                "--zabbix-server directly at the proxy that owns this host."
            ) from exc
        except (ProcessingError, OSError):
            if attempt >= attempts:
                raise
            log.warning(
                "Trapper send failed — retrying",
                attempt=attempt, attempts=attempts, retry_delay=retry_delay,
            )
            time.sleep(retry_delay)
    raise AssertionError("unreachable")  # pragma: no cover — loop always returns or raises
