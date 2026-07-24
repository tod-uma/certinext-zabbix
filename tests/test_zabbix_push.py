"""Unit tests for the zabbix_push metric computation and item building.

All tests use detached Domain models (no API client) and a fixed reference
time, so the metric math is deterministic and offline. The trapper send is
covered by patching the Sender class — no sockets are opened.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import Domain
from zabbix_utils.exceptions import ProcessingError

from certinext_zabbix.zabbix_push import (
    ENV_PROD,
    ENV_SANDBOX,
    KEY_EXPIRING,
    KEY_MIN_DAYS_LEFT,
    KEY_TOTAL,
    KEY_UNVERIFIED,
    DomainScope,
    collect_domain_metrics,
    collect_expiry_metrics,
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
