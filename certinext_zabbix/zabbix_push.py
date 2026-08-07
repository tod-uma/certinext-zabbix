"""Metric collection and Zabbix trapper push for CertiNext DCV monitoring.

Computes the DCV health metrics pushed to Zabbix by the
``certinext-zabbix-push`` CLI (:mod:`certinext_zabbix.zabbix_push_cli`)
and wraps the send through the Zabbix sender (trapper) protocol via
``zabbix_utils``. The item keys here must match ``CertiNext DCV by Zabbix
trapper`` on the Zabbix server (source of truth:
``templates/template_certinext/7.0/template_certinext.yaml`` in this repo).
Keys are parameterized by environment (``[prod]`` /
``[sandbox]``, derived from the resolved connection) so both environments
can be monitored on the same Zabbix host without colliding.

Three metric families, matching the three designed checks:

- **Domain-list metrics** (cheap, one API list call): total domain count and
  how many are unverified (ACTIVE but not DCV-VERIFIED). Pushed every run.
- **Expiry metrics** (one API detail call per verified domain): how many
  verified domains' DCV expires within the renewal lead time, and the
  minimum days left. Pushed only when the caller opts in (daily run).
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

import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple

import httpx
import structlog
from certinext import filter_needs_dcv
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import Domain
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

ENV_PROD = "prod"
ENV_SANDBOX = "sandbox"

_SECONDS_PER_DAY = 86400

# OrderRecord.order_status values used to bucket the orders report.
#
# These are the only two values we classify positively; everything else is
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


def item_key(base: str, env: str) -> str:
    """Return the environment-parameterized Zabbix item key.

    The template defines every item once per environment using Zabbix key
    parameters (``certinext.domains.total[prod]`` /
    ``certinext.domains.total[sandbox]``), so prod and sandbox pushes to the
    same Zabbix host land in separate items. The caller derives *env* from
    the resolved CertiNext connection — never from a user-supplied label —
    so sandbox data cannot masquerade as prod.

    Args:
        base: One of the ``KEY_*`` constants.
        env: :data:`ENV_PROD` or :data:`ENV_SANDBOX`.

    Returns:
        The full item key, e.g. ``certinext.domains.unverified[prod]``.
    """
    return f"{base}[{env}]"


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


def collect_domain_metrics(domains: Sequence[Domain], env: str) -> dict[str, int | float]:
    """Compute the domain-list metrics from a CertiNext domain listing.

    ``certinext.domains.total`` guards against a silently empty/truncated
    list (a zero here means the API returned nothing — see the vendor
    pagination/search history in the certinext repo); an
    ``unverified == 0`` alone would look healthy in that failure mode.

    Args:
        domains: Domains as returned by the CertiNext list endpoint.
        env: Environment key parameter (:data:`ENV_PROD` /
            :data:`ENV_SANDBOX`) — see :func:`item_key`.

    Returns:
        Mapping of environment-keyed Zabbix item key to value for
        :data:`KEY_TOTAL` and :data:`KEY_UNVERIFIED`.
    """
    return {
        item_key(KEY_TOTAL, env): len(domains),
        item_key(KEY_UNVERIFIED, env): sum(1 for d in domains if d.needs_dcv),
    }


def collect_expiry_metrics(
    verified: Sequence[Domain],
    expiry_days: int,
    env: str,
    *,
    now: datetime | None = None,
) -> dict[str, int | float]:
    """Compute the DCV-expiry metrics from refreshed verified domains.

    The caller must have refreshed each domain first — the CertiNext list
    endpoint does not include ``validTill``; only the per-domain detail
    endpoint does. Domains whose expiry is unknown (``dcv_expires is None``)
    are excluded from both metrics.

    Args:
        verified: Refreshed ACTIVE+VERIFIED domains
            (see :func:`verified_domains`).
        expiry_days: Renewal lead time in days; a domain counts as expiring
            when its DCV expiry falls within this window (already-expired
            included).
        env: Environment key parameter (:data:`ENV_PROD` /
            :data:`ENV_SANDBOX`) — see :func:`item_key`.
        now: Reference time for the day math (timezone-aware). Defaults to
            the current UTC time; injectable for tests.

    Returns:
        Mapping with :data:`KEY_EXPIRING` (count) and, when at least one
        expiry is known, :data:`KEY_MIN_DAYS_LEFT` (float days, may be
        negative when a DCV has already lapsed), both environment-keyed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now + timedelta(days=expiry_days)
    metrics: dict[str, int | float] = {
        item_key(KEY_EXPIRING, env): sum(
            1 for d in verified if d.dcv_expires is not None and d.dcv_expires <= cutoff
        ),
    }
    days_left = [
        (d.dcv_expires - now).total_seconds() / _SECONDS_PER_DAY
        for d in verified
        if d.dcv_expires is not None
    ]
    if days_left:
        metrics[item_key(KEY_MIN_DAYS_LEFT, env)] = round(min(days_left), 2)
    return metrics


class OrderBuckets(NamedTuple):
    """The orders report split into the four buckets the metrics need.

    Produced by :func:`bucket_orders` and consumed by
    :func:`collect_order_metrics`. The names deliberately avoid the word
    "pending": the vendor's ``status`` param uses ``pending-*`` for the
    whole pre-fulfillment space, which spans both :attr:`unissued` and
    :attr:`undownloaded`, so reusing it for either half alone would
    mislead anyone cross-referencing the API docs.
    """

    unissued: list[OrderRecord]
    undownloaded: list[OrderRecord]
    failed: list[OrderRecord]
    issued: list[OrderRecord]


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
    """Split *records* four ways on ``order_status`` and cert presence.

    Only :data:`ORDER_STATUS_FULFILLED` and :data:`ORDER_STATUS_ACCEPTED`
    are classified positively; every other non-empty ``order_status`` falls
    into :attr:`~OrderBuckets.failed`. That catch-all is deliberate — a
    live 100-row prod sample carried only ``"Order Cancelled"`` as a third
    value, but the vendor's rejected/expired/revoked orders have never been
    observed and their ``order_status`` strings are undocumented. Treating
    an unknown terminal status as a failure surfaces it on the
    failed-recent metric rather than silently dropping it from every
    bucket. Unrecognized values are logged once per run so drift is visible.

    An accepted order is split again on whether a certificate exists for
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
    buckets = OrderBuckets(unissued=[], undownloaded=[], failed=[], issued=[])
    unrecognized: set[str] = set()
    missing_status = 0
    for record in records:
        status = record.order_status
        if status == ORDER_STATUS_FULFILLED:
            buckets.issued.append(record)
        elif status == ORDER_STATUS_ACCEPTED:
            if record.certificate_expiry_date is not None:
                buckets.undownloaded.append(record)
            else:
                buckets.unissued.append(record)
        elif status:
            unrecognized.add(status)
            buckets.failed.append(record)
        else:
            missing_status += 1
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
    env: str,
    *,
    failing_lookback_days: int,
    cert_expiry_days: int,
    now: datetime | None = None,
) -> dict[str, int | float]:
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
    filter on — a rejected/cancelled/expired/revoked order is a terminal
    vendor-side event timestamped by ``order_date``, not something that
    stays continuously true in Zabbix's own item history.

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
        env: Environment key parameter (:data:`ENV_PROD` /
            :data:`ENV_SANDBOX`) — see :func:`item_key`.
        failing_lookback_days: Only failed orders whose ``order_date``
            falls within this many days of *now* count toward
            :data:`KEY_ORDERS_FAILED_RECENT` — older rejections/
            cancellations are normal history, not something to alert on
            forever.
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
        :data:`KEY_ORDERS_DAYS_SINCE_ISSUED` (float days), all
        environment-keyed.
    """
    now = now or datetime.now(timezone.utc)
    failing_cutoff = now - timedelta(days=failing_lookback_days)
    expiry_cutoff = now + timedelta(days=cert_expiry_days)
    # Both buckets hold orders the CA has generated a certificate for; only
    # the download step differs, which cert expiry doesn't care about.
    with_cert = [*buckets.issued, *buckets.undownloaded]
    metrics: dict[str, int | float] = {
        # No re-filtering: bucket_orders already classified on the two
        # typed fields the vendor's data supports checking programmatically.
        item_key(KEY_ORDERS_UNISSUED, env): len(buckets.unissued),
        item_key(KEY_ORDERS_UNDOWNLOADED, env): len(buckets.undownloaded),
        item_key(KEY_ORDERS_FAILED_RECENT, env): sum(
            1 for o in buckets.failed
            if o.order_date is not None and o.order_date >= failing_cutoff
        ),
        item_key(KEY_ORDERS_EXPIRING, env): sum(
            1 for o in with_cert
            if o.certificate_expiry_date is not None and o.certificate_expiry_date <= expiry_cutoff
        ),
    }
    issued_dates = [o.order_date for o in with_cert if o.order_date is not None]
    if issued_dates:
        days_since = (now - max(issued_dates)).total_seconds() / _SECONDS_PER_DAY
        metrics[item_key(KEY_ORDERS_DAYS_SINCE_ISSUED, env)] = round(days_since, 2)
    return metrics


def push_metrics(
    metrics: dict[str, int | float],
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
