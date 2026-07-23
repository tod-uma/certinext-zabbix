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

Two metric families, matching the two designed checks:

- **Domain-list metrics** (cheap, one API list call): total domain count and
  how many are unverified (ACTIVE but not DCV-VERIFIED). Pushed every run.
- **Expiry metrics** (one API detail call per verified domain): how many
  verified domains' DCV expires within the renewal lead time, and the
  minimum days left. Pushed only when the caller opts in (daily run).
"""

import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum

import httpx
import structlog
from certinext import filter_needs_dcv
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import Domain
from zabbix_utils import ItemValue, Sender
from zabbix_utils.exceptions import ProcessingError
from zabbix_utils.types import TrapperResponse

log = structlog.get_logger()

KEY_TOTAL = "certinext.domains.total"
KEY_UNVERIFIED = "certinext.domains.unverified"
KEY_EXPIRING = "certinext.dcv.expiring"
KEY_MIN_DAYS_LEFT = "certinext.dcv.min_days_left"

ENV_PROD = "prod"
ENV_SANDBOX = "sandbox"

_SECONDS_PER_DAY = 86400


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
    all_names = {d.name for d in domains if d.name}
    return filter_needs_dcv(list(domains), all_names, check_ns=(scope is DomainScope.NS_BOUNDARY))


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
            cannot be obtained or parsed on the final attempt.
        OSError: When the connection fails at the socket level on the final
            attempt.
    """
    items = [ItemValue(zabbix_host, key, str(value)) for key, value in sorted(metrics.items())]
    for attempt in range(1, attempts + 1):
        try:
            # A fresh Sender per attempt — the previous one's socket state is
            # unknown after a failure.
            return Sender(server=server, port=port, timeout=timeout).send(items)
        except (ProcessingError, OSError):
            if attempt >= attempts:
                raise
            log.warning(
                "Trapper send failed — retrying",
                attempt=attempt, attempts=attempts, retry_delay=retry_delay,
            )
            time.sleep(retry_delay)
    raise AssertionError("unreachable")  # pragma: no cover — loop always returns or raises
