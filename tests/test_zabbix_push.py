"""Unit tests for the zabbix_push metric computation and item building.

All tests use detached Domain models (no API client) and a fixed reference
time, so the metric math is deterministic and offline. The trapper send is
covered by patching the Sender class — no sockets are opened.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import Domain
from certinext.orders import OrderRecord
from zabbix_utils.exceptions import ProcessingError

from certinext_zabbix.zabbix_push import (
    ENV_PROD,
    ENV_SANDBOX,
    KEY_EXPIRING,
    KEY_MIN_DAYS_LEFT,
    KEY_ORDERS_DAYS_SINCE_ISSUED,
    KEY_ORDERS_EXPIRING,
    KEY_ORDERS_FAILED_RECENT,
    KEY_ORDERS_UNDOWNLOADED,
    KEY_ORDERS_UNISSUED,
    KEY_TOTAL,
    KEY_UNVERIFIED,
    DomainScope,
    OrderBuckets,
    bucket_orders,
    collect_domain_metrics,
    collect_expiry_metrics,
    collect_order_metrics,
    fetch_orders,
    item_key,
    push_metrics,
    refresh_domain,
    scope_domains,
    verified_domains,
)

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _domain(
    name: str,
    status: str = "ACTIVE",
    dcv_status: str = "VERIFIED",
    valid_till: str | None = None,
) -> Domain:
    """Build a detached Domain from wire-format fields.

    Args:
        name: Domain name.
        status: Domain status (``ACTIVE``, ``INACTIVE``, ``EXPIRED``).
        dcv_status: DCV status (``VERIFIED``, ``PENDING``, ``REJECTED``).
        valid_till: ISO timestamp for the ``validTill`` field, or None to
            omit it (as the list endpoint does).

    Returns:
        A Domain usable for field access only (no API client attached).
    """
    payload: dict[str, Any] = {
        "domainName": name, "status": status, "dcvStatus": dcv_status,
    }
    if valid_till is not None:
        payload["validTill"] = valid_till
    return Domain.from_payload(None, payload)


class TestItemKey:
    """Environment parameterization of the item keys."""

    def test_prod_and_sandbox_keys(self) -> None:
        assert item_key(KEY_TOTAL, ENV_PROD) == "certinext.domains.total[prod]"
        assert item_key(KEY_TOTAL, ENV_SANDBOX) == "certinext.domains.total[sandbox]"


class TestCollectDomainMetrics:
    """Total and unverified counts from a domain listing."""

    def test_counts_mixture(self) -> None:
        domains = [
            _domain("verified.edu"),
            _domain("pending.edu", dcv_status="PENDING"),
            _domain("rejected.edu", dcv_status="REJECTED"),
            _domain("inactive.edu", status="INACTIVE", dcv_status="PENDING"),
            _domain("expired.edu", status="EXPIRED", dcv_status="VERIFIED"),
        ]
        metrics = collect_domain_metrics(domains, ENV_PROD)
        # Only ACTIVE + not-VERIFIED counts as unverified (needs_dcv);
        # the INACTIVE pending domain does not.
        assert metrics == {
            "certinext.domains.total[prod]": 5,
            "certinext.domains.unverified[prod]": 2,
        }

    def test_sandbox_env_reaches_keys(self) -> None:
        metrics = collect_domain_metrics([_domain("verified.edu")], ENV_SANDBOX)
        assert metrics == {
            "certinext.domains.total[sandbox]": 1,
            "certinext.domains.unverified[sandbox]": 0,
        }

    def test_empty_list_is_visible_as_zero_total(self) -> None:
        assert collect_domain_metrics([], ENV_PROD) == {
            "certinext.domains.total[prod]": 0,
            "certinext.domains.unverified[prod]": 0,
        }


class TestVerifiedDomains:
    """Selection of the ACTIVE+VERIFIED subset for the expiry check."""

    def test_filters_to_active_verified(self) -> None:
        keep = _domain("verified.edu")
        domains = [
            keep,
            _domain("pending.edu", dcv_status="PENDING"),
            _domain("inactive.edu", status="INACTIVE"),
        ]
        assert verified_domains(domains) == [keep]


class TestCollectExpiryMetrics:
    """Expiring count and min-days-left from refreshed verified domains."""

    def test_expiring_window_and_min_days(self) -> None:
        verified = [
            _domain("soon.edu", valid_till="2026-07-18T12:00:00Z"),      # +5d → expiring
            _domain("later.edu", valid_till="2026-08-22T12:00:00Z"),     # +40d → fine
            _domain("lapsed.edu", valid_till="2026-07-12T12:00:00Z"),    # -1d → expiring
            _domain("unknown.edu"),                                      # no validTill → excluded
        ]
        metrics = collect_expiry_metrics(verified, 14, ENV_PROD, now=_NOW)
        assert metrics[item_key(KEY_EXPIRING, ENV_PROD)] == 2
        assert metrics[item_key(KEY_MIN_DAYS_LEFT, ENV_PROD)] == -1.0

    def test_boundary_day_counts_as_expiring(self) -> None:
        verified = [_domain("edge.edu", valid_till="2026-07-27T12:00:00Z")]  # exactly +14d
        metrics = collect_expiry_metrics(verified, 14, ENV_PROD, now=_NOW)
        assert metrics[item_key(KEY_EXPIRING, ENV_PROD)] == 1
        assert metrics[item_key(KEY_MIN_DAYS_LEFT, ENV_PROD)] == 14.0

    def test_no_known_expiries_omits_min_days(self) -> None:
        metrics = collect_expiry_metrics([_domain("unknown.edu")], 14, ENV_PROD, now=_NOW)
        assert metrics == {item_key(KEY_EXPIRING, ENV_PROD): 0}

    def test_no_verified_domains(self) -> None:
        assert collect_expiry_metrics([], 14, ENV_SANDBOX, now=_NOW) == {
            "certinext.dcv.expiring[sandbox]": 0,
        }


def _order(
    order_status: str = "Order Accepted",
    order_date: str | None = None,
    certificate_expiry_date: str | None = None,
) -> OrderRecord:
    """Build an OrderRecord from wire-format fields for order-health tests.

    Args:
        order_status: Value for the ``orderStatus`` wire field.
        order_date: ISO timestamp for ``orderDate``, or None to omit it.
        certificate_expiry_date: ISO timestamp for ``certificateExpiryDate``,
            or None to omit it.

    Returns:
        A validated OrderRecord (no API client attached — field access only).
    """
    payload: dict[str, Any] = {"orderStatus": order_status}
    if order_date is not None:
        payload["orderDate"] = order_date
    if certificate_expiry_date is not None:
        payload["certificateExpiryDate"] = certificate_expiry_date
    return OrderRecord.model_validate(payload)


class TestFetchOrders:
    """The orders fetch is unfiltered, date-boundable, and retried."""

    def test_never_sends_a_status_filter(self) -> None:
        # Regression guard for sysadmin/certinext-zabbix#3: 5 of the 6
        # documented pending-* status values return HTTP 422, so any
        # status-filtered fetch is dead on arrival. Bucketing is client-side.
        accessor = MagicMock()
        accessor.get_list.return_value = [_order()]
        fetch_orders(accessor)
        assert accessor.get_list.call_args_list == [((), {"since": None})]

    def test_since_passed_through(self) -> None:
        accessor = MagicMock()
        accessor.get_list.return_value = [_order()]
        cutoff = date(2026, 7, 1)
        fetch_orders(accessor, since=cutoff)
        assert accessor.get_list.call_args_list == [((), {"since": cutoff})]

    def test_retries_transient_failure_then_succeeds(self) -> None:
        accessor = MagicMock()
        accessor.get_list.side_effect = [httpx.ReadTimeout("timed out"), [_order()]]
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            records = fetch_orders(accessor)
        assert len(records) == 1
        mock_sleep.assert_called_once_with(5.0)

    def test_exhausts_attempts_and_raises(self) -> None:
        accessor = MagicMock()
        accessor.get_list.side_effect = CertiNextAPIError(503, "service unavailable")
        with patch("certinext_zabbix.zabbix_push.time.sleep"), \
             pytest.raises(CertiNextAPIError):
            fetch_orders(accessor, attempts=2, retry_delay=0.1)
        assert accessor.get_list.call_count == 2

    def test_rejected_status_filter_surfaces_rather_than_zeroing(self) -> None:
        # The exact failure issue #3 predicted: a 422 must propagate so the
        # caller skips the metrics loudly, never silently push a zero.
        accessor = MagicMock()
        accessor.get_list.side_effect = CertiNextAPIError(422, "invalid status")
        with patch("certinext_zabbix.zabbix_push.time.sleep"), \
             pytest.raises(CertiNextAPIError) as excinfo:
            fetch_orders(accessor, attempts=1)
        assert excinfo.value.status_code == 422


class TestBucketOrders:
    """Client-side bucketing on order_status, with an unknown-is-failed rule."""

    def test_splits_the_four_buckets(self) -> None:
        buckets = bucket_orders([
            _order("Order Fulfilled", certificate_expiry_date="2027-01-01T00:00:00"),
            _order("Order Accepted"),
            _order("Order Accepted", certificate_expiry_date="2027-01-01T00:00:00"),
            _order("Order Cancelled"),
        ])
        assert len(buckets.issued) == 1
        assert len(buckets.unissued) == 1
        assert len(buckets.undownloaded) == 1
        assert len(buckets.failed) == 1

    def test_accepted_split_keys_on_expiry_date_not_display_string(self) -> None:
        """An accepted order with a cert expiry is generated-but-unfetched.

        Keys on the typed certificate_expiry_date, never on the free-text
        certificate_status display string. The two agree across the whole
        prod+sandbox corpus; only the date is safe to compare against.
        """
        generated = _order(
            "Order Accepted", certificate_expiry_date="2027-01-01T00:00:00",
        )
        awaiting = _order("Order Accepted")
        buckets = bucket_orders([generated, awaiting])
        assert buckets.undownloaded == [generated]
        assert buckets.unissued == [awaiting]

    def test_unrecognized_status_counts_as_failed(self) -> None:
        # Rejected/expired/revoked orders have never been observed live, so
        # their order_status strings are unknown. An unknown terminal status
        # must surface on failed-recent, not vanish from every bucket.
        buckets = bucket_orders([_order("Order Revoked"), _order("Something New")])
        assert len(buckets.failed) == 2
        assert not buckets.unissued and not buckets.undownloaded and not buckets.issued

    def test_missing_status_counted_nowhere(self) -> None:
        buckets = bucket_orders([OrderRecord.model_validate({}), _order("Order Accepted")])
        assert len(buckets.unissued) == 1
        assert not buckets.issued and not buckets.undownloaded and not buckets.failed

    def test_empty_input(self) -> None:
        assert bucket_orders([]) == OrderBuckets(
            unissued=[], undownloaded=[], failed=[], issued=[],
        )


def _buckets(
    unissued: list[OrderRecord] | None = None,
    undownloaded: list[OrderRecord] | None = None,
    failed: list[OrderRecord] | None = None,
    issued: list[OrderRecord] | None = None,
) -> OrderBuckets:
    """Build an OrderBuckets with only the buckets a test cares about.

    Args:
        unissued: Orders accepted with no certificate generated.
        undownloaded: Orders whose certificate exists but was never fetched.
        failed: Orders in a terminal non-fulfilled state.
        issued: Orders fulfilled (certificate generated and downloaded).

    Returns:
        An OrderBuckets with unsupplied buckets empty.
    """
    return OrderBuckets(
        unissued=unissued or [], undownloaded=undownloaded or [],
        failed=failed or [], issued=issued or [],
    )


class TestCollectOrderMetrics:
    """Counts and days-since-issued derived from pre-bucketed orders."""

    def test_unissued_and_undownloaded_count_their_buckets_verbatim(self) -> None:
        # bucket_orders owns the classification; this must not re-filter, or
        # a record it deliberately placed here would be silently dropped.
        metrics = collect_order_metrics(
            _buckets(
                unissued=[_order(), _order()],
                undownloaded=[_order(certificate_expiry_date="2027-01-01T00:00:00")],
            ),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_UNISSUED, ENV_PROD)] == 2
        assert metrics[item_key(KEY_ORDERS_UNDOWNLOADED, ENV_PROD)] == 1

    def test_failed_recent_excludes_orders_outside_lookback(self) -> None:
        failed = [
            _order(order_date="2026-07-01T00:00:00"),   # 12d old — within 30d lookback
            _order(order_date="2025-01-01T00:00:00"),    # ancient — outside lookback
        ]
        metrics = collect_order_metrics(
            _buckets(failed=failed),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_FAILED_RECENT, ENV_PROD)] == 1

    def test_failed_recent_boundary_day_counts(self) -> None:
        failed = [_order(order_date="2026-06-13T12:00:00")]  # exactly 30d before _NOW
        metrics = collect_order_metrics(
            _buckets(failed=failed),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_FAILED_RECENT, ENV_PROD)] == 1

    def test_days_since_issued_uses_max_order_date(self) -> None:
        issued = [
            _order(order_date="2026-07-10T12:00:00"),  # 3d ago
            _order(order_date="2026-07-01T12:00:00"),  # 12d ago — not the max
        ]
        metrics = collect_order_metrics(
            _buckets(issued=issued),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_DAYS_SINCE_ISSUED, ENV_PROD)] == 3.0

    def test_days_since_issued_counts_undownloaded_certs_too(self) -> None:
        # The CA issued it; nobody fetched it. That still proves issuance is
        # working, which is the only thing this metric claims to measure.
        metrics = collect_order_metrics(
            _buckets(
                issued=[_order(order_date="2026-07-01T12:00:00")],       # 12d ago
                undownloaded=[_order(order_date="2026-07-10T12:00:00")],  # 3d ago
            ),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_DAYS_SINCE_ISSUED, ENV_PROD)] == 3.0

    def test_no_issued_orders_omits_days_since_issued(self) -> None:
        metrics = collect_order_metrics(
            _buckets(), ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert item_key(KEY_ORDERS_DAYS_SINCE_ISSUED, ENV_PROD) not in metrics

    def test_issued_order_with_no_order_date_is_excluded(self) -> None:
        metrics = collect_order_metrics(
            _buckets(issued=[_order()]),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert item_key(KEY_ORDERS_DAYS_SINCE_ISSUED, ENV_PROD) not in metrics

    def test_sandbox_env_reaches_keys(self) -> None:
        metrics = collect_order_metrics(
            _buckets(), ENV_SANDBOX, failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics == {
            item_key(KEY_ORDERS_UNISSUED, ENV_SANDBOX): 0,
            item_key(KEY_ORDERS_UNDOWNLOADED, ENV_SANDBOX): 0,
            item_key(KEY_ORDERS_FAILED_RECENT, ENV_SANDBOX): 0,
            item_key(KEY_ORDERS_EXPIRING, ENV_SANDBOX): 0,
        }

    def test_defaults_now_to_current_time(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        metrics = collect_order_metrics(
            _buckets(issued=[_order(order_date=recent.isoformat())]),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=30,
        )
        assert metrics[item_key(KEY_ORDERS_DAYS_SINCE_ISSUED, ENV_PROD)] == pytest.approx(1.0, abs=0.01)

    def test_expiring_counts_issued_certs_within_lead_time(self) -> None:
        issued = [
            _order(certificate_expiry_date="2026-07-20T12:00:00"),  # +7d → expiring
            _order(certificate_expiry_date="2026-09-01T12:00:00"),  # +50d → fine
            _order(certificate_expiry_date="2026-07-10T12:00:00"),  # -3d → expiring (lapsed)
            _order(),                                                # no expiry date → excluded
        ]
        metrics = collect_order_metrics(
            _buckets(issued=issued),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=14, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_EXPIRING, ENV_PROD)] == 2

    def test_expiring_counts_undownloaded_certs_too(self) -> None:
        # An unfetched certificate still expires on the CA's schedule.
        metrics = collect_order_metrics(
            _buckets(
                issued=[_order(certificate_expiry_date="2026-07-20T12:00:00")],
                undownloaded=[_order(certificate_expiry_date="2026-07-18T12:00:00")],
            ),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=14, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_EXPIRING, ENV_PROD)] == 2

    def test_expiring_boundary_day_counts(self) -> None:
        issued = [_order(certificate_expiry_date="2026-07-27T12:00:00")]  # exactly +14d
        metrics = collect_order_metrics(
            _buckets(issued=issued),
            ENV_PROD, failing_lookback_days=30, cert_expiry_days=14, now=_NOW,
        )
        assert metrics[item_key(KEY_ORDERS_EXPIRING, ENV_PROD)] == 1


class TestPushMetrics:
    """The trapper send stringifies values and targets the right server."""

    def test_items_and_destination(self) -> None:
        mock_sender = MagicMock()
        with patch("certinext_zabbix.zabbix_push.Sender",
                   return_value=mock_sender) as mock_cls:
            push_metrics(
                {
                    item_key(KEY_UNVERIFIED, ENV_PROD): 3,
                    item_key(KEY_TOTAL, ENV_PROD): 120,
                    item_key(KEY_MIN_DAYS_LEFT, ENV_PROD): 20.66,
                },
                zabbix_host="host.example.edu",
                server="zabbix.example.edu", port=10051,
            )
        mock_cls.assert_called_once_with(server="zabbix.example.edu", port=10051, timeout=10)
        (items,) = mock_sender.send.call_args.args
        assert [(i.host, i.key, i.value) for i in items] == [
            ("host.example.edu", "certinext.dcv.min_days_left[prod]", "20.66"),
            ("host.example.edu", "certinext.domains.total[prod]", "120"),
            ("host.example.edu", "certinext.domains.unverified[prod]", "3"),
        ]

    def test_timeout_forwarded(self) -> None:
        mock_sender = MagicMock()
        with patch("certinext_zabbix.zabbix_push.Sender",
                   return_value=mock_sender) as mock_cls:
            push_metrics(
                {item_key(KEY_TOTAL, ENV_PROD): 1},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
                timeout=3,
            )
        mock_cls.assert_called_once_with(server="zbx.example.edu", port=10051, timeout=3)


class TestPushMetricsRetry:
    """Transient transport failures retry; server-side rejections do not."""

    def test_transient_failure_then_success(self) -> None:
        mock_sender = MagicMock()
        response = MagicMock(failed=0)
        mock_sender.send.side_effect = [ProcessingError("connection refused"), response]
        with patch("certinext_zabbix.zabbix_push.Sender", return_value=mock_sender), \
             patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            result = push_metrics(
                {item_key(KEY_TOTAL, ENV_PROD): 1},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
            )
        assert result is response
        assert mock_sender.send.call_count == 2
        mock_sleep.assert_called_once_with(5.0)

    def test_gives_up_after_attempts(self) -> None:
        mock_sender = MagicMock()
        mock_sender.send.side_effect = ProcessingError("connection refused")
        with patch("certinext_zabbix.zabbix_push.Sender", return_value=mock_sender), \
             patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep, \
             pytest.raises(ProcessingError):
            push_metrics(
                {item_key(KEY_TOTAL, ENV_PROD): 1},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
                attempts=3, retry_delay=0.1,
            )
        assert mock_sender.send.call_count == 3
        assert mock_sleep.call_count == 2

    def test_socket_errors_also_retry(self) -> None:
        mock_sender = MagicMock()
        response = MagicMock(failed=0)
        mock_sender.send.side_effect = [ConnectionResetError("reset"), response]
        with patch("certinext_zabbix.zabbix_push.Sender", return_value=mock_sender), \
             patch("certinext_zabbix.zabbix_push.time.sleep"):
            result = push_metrics(
                {item_key(KEY_TOTAL, ENV_PROD): 1},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
            )
        assert result is response
        assert mock_sender.send.call_count == 2

    def test_redirect_loop_is_not_retried_and_names_the_cause(self) -> None:
        # zabbix_utils follows a proxy-group redirect by unbounded recursion, so
        # a proxy redirecting to its own address blows the stack. Retrying would
        # replay a ~1000-connection storm against an already-struggling proxy,
        # and a bare RecursionError leaves the operator a traceback whose
        # innermost frames are unrelated to the fault.
        mock_sender = MagicMock()
        mock_sender.send.side_effect = RecursionError(
            "maximum recursion depth exceeded in comparison"
        )
        with patch("certinext_zabbix.zabbix_push.Sender", return_value=mock_sender), \
             patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep, \
             pytest.raises(ProcessingError, match="Redirect loop") as excinfo:
            push_metrics(
                {item_key(KEY_TOTAL, ENV_PROD): 1},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
                attempts=3, retry_delay=0.1,
            )

        assert mock_sender.send.call_count == 1, "a redirect loop must not be retried"
        assert mock_sleep.call_count == 0
        # The endpoint is named, so the operator knows which one to look at.
        assert "zbx.example.edu:10051" in str(excinfo.value)
        # The original RecursionError stays reachable for the debug sidecar.
        assert isinstance(excinfo.value.__cause__, RecursionError)

    def test_rejected_values_are_not_retried(self) -> None:
        # failed > 0 means the server rejected values (bad host/key/allowed
        # hosts) — a retry cannot fix configuration, so exactly one send.
        mock_sender = MagicMock()
        response = MagicMock(failed=2)
        mock_sender.send.return_value = response
        with patch("certinext_zabbix.zabbix_push.Sender", return_value=mock_sender), \
             patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            result = push_metrics(
                {item_key(KEY_TOTAL, ENV_PROD): 1},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
            )
        assert result is response
        assert mock_sender.send.call_count == 1
        mock_sleep.assert_not_called()


class TestScopeDomains:
    """The three --domain-scope modes over a small apex/subdomain tree."""

    def test_all_returns_domains_unchanged(self) -> None:
        domains = [_domain("example.edu"), _domain("dept.example.edu")]
        assert scope_domains(domains, DomainScope.ALL) == domains

    def test_top_excludes_registered_subdomain(self) -> None:
        apex = _domain("example.edu")
        sub = _domain("dept.example.edu")
        assert scope_domains([apex, sub], DomainScope.TOP) == [apex]

    def test_top_keeps_domain_with_no_registered_ancestor(self) -> None:
        unrelated = _domain("otherschool.edu")
        apex = _domain("example.edu")
        result = scope_domains([apex, unrelated], DomainScope.TOP)
        assert result == [apex, unrelated]

    def test_ns_boundary_reincludes_zone_cut_subdomain(self) -> None:
        apex = _domain("example.edu")
        zone_cut_sub = _domain("dept.example.edu")
        with patch(
            "certinext.models.domains._has_ns_records",
            side_effect=lambda name: name == "dept.example.edu",
        ):
            result = scope_domains([apex, zone_cut_sub], DomainScope.NS_BOUNDARY)
        assert result == [apex, zone_cut_sub]

    def test_ns_boundary_still_excludes_plain_subdomain(self) -> None:
        apex = _domain("example.edu")
        plain_sub = _domain("dept.example.edu")
        with patch("certinext.models.domains._has_ns_records", return_value=False):
            result = scope_domains([apex, plain_sub], DomainScope.NS_BOUNDARY)
        assert result == [apex]


class TestRefreshDomain:
    """Retry-once idiom mirroring push_metrics, scoped to one domain refresh."""

    def test_succeeds_on_second_attempt(self) -> None:
        d = MagicMock(name="flaky.edu")
        d.refresh.side_effect = [httpx.ReadTimeout("timed out"), None]
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            refresh_domain(d)
        assert d.refresh.call_count == 2
        mock_sleep.assert_called_once_with(5.0)

    def test_exhausts_attempts_and_raises(self) -> None:
        d = MagicMock(name="down.edu")
        exc = CertiNextAPIError(503, "service unavailable")
        d.refresh.side_effect = exc
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep, \
             pytest.raises(CertiNextAPIError):
            refresh_domain(d, attempts=3, retry_delay=0.1)
        assert d.refresh.call_count == 3
        assert mock_sleep.call_count == 2

    def test_non_retryable_exception_raises_immediately(self) -> None:
        d = MagicMock(name="weird.edu")
        d.refresh.side_effect = ValueError("unparseable response")
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep, \
             pytest.raises(ValueError, match="unparseable response"):
            refresh_domain(d)
        assert d.refresh.call_count == 1
        mock_sleep.assert_not_called()
