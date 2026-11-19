"""Unit tests for the zabbix_push metric computation and item building.

All tests use detached Domain models (no API client) and a fixed reference
time, so the metric math is deterministic and offline. The trapper send is
covered by patching the Sender class — no sockets are opened.
"""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import DcvInfo, Domain
from certinext.orders import OrderRecord
from zabbix_utils.exceptions import ProcessingError

from certinext_zabbix import zabbix_push
from certinext_zabbix.zabbix_push import (
    DCV_METHOD_PERSIST,
    KEY_ENV,
    KEY_EXPIRING,
    KEY_MIN_DAYS_LEFT,
    KEY_ORDERS_DAYS_SINCE_ISSUED,
    KEY_ORDERS_EXPIRING,
    KEY_ORDERS_FAILED_RECENT,
    KEY_ORDERS_UNDOWNLOADED,
    KEY_ORDERS_UNISSUED,
    KEY_PERSIST_APEXES,
    KEY_PERSIST_DISCOVERY,
    KEY_PERSIST_MIN_DAYS_LEFT,
    KEY_TOKEN_DOMAINS,
    KEY_TOTAL,
    KEY_UNVERIFIED,
    PERSIST_VALUE_UNAVAILABLE,
    DomainScope,
    OrderBuckets,
    bucket_orders,
    collect_domain_metrics,
    collect_expiry_metrics,
    collect_order_metrics,
    fetch_dcv_methods,
    fetch_orders,
    normalise_persist_value,
    persist_apex_names,
    persist_discovery_value,
    persist_expected_by_apex,
    persist_record_value,
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
    validated_by: str | None = None,
) -> Domain:
    """Build a detached Domain from wire-format fields.

    Args:
        name: Domain name.
        status: Domain status (``ACTIVE``, ``INACTIVE``, ``EXPIRED``).
        dcv_status: DCV status (``VERIFIED``, ``PENDING``, ``REJECTED``).
        valid_till: ISO timestamp for the ``validTill`` field, or None to
            omit it (as the list endpoint does).
        validated_by: Wire ``validatedBy`` (e.g. ``"SSLCOM"``), or None to
            omit it — the library then reads the record as emSign.

    Returns:
        A Domain usable for field access only (no API client attached).
    """
    payload: dict[str, Any] = {
        "domainName": name, "status": status, "dcvStatus": dcv_status,
    }
    if valid_till is not None:
        payload["validTill"] = valid_till
    if validated_by is not None:
        payload["validatedBy"] = validated_by
    return Domain.from_payload(None, payload)


class TestBareKeys:
    """Item keys carry no [env] parameter: each environment has its own host.

    Zabbix's trapper does not expand macros in item keys, so the
    environment cannot be a key parameter the template fills in; it moved
    to the target host and the certinext.env misroute guard.
    """

    def test_no_key_has_a_parameter(self) -> None:
        keys = [v for k, v in vars(zabbix_push).items() if k.startswith("KEY_")]
        assert keys, "no KEY_* constants found"
        assert all("[" not in key for key in keys), keys

    def test_new_keys(self) -> None:
        assert KEY_ENV == "certinext.env"
        assert KEY_PERSIST_DISCOVERY == "certinext.dcv.persist.discovery"
        assert KEY_TOKEN_DOMAINS == "certinext.dcv.token_domains"
        assert KEY_PERSIST_MIN_DAYS_LEFT == "certinext.dcv.persist_min_days_left"
        assert KEY_PERSIST_APEXES == "certinext.dcv.persist_apexes"

    def test_no_persist_expected_key(self) -> None:
        # Removed in #12's redesign: the value rides in the discovery rows.
        assert not hasattr(zabbix_push, "KEY_PERSIST_EXPECTED")


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
        metrics = collect_domain_metrics(domains)
        # Only ACTIVE + not-VERIFIED counts as unverified (needs_dcv);
        # the INACTIVE pending domain does not.
        assert metrics == {
            "certinext.domains.total": 5,
            "certinext.domains.unverified": 2,
        }

    def test_empty_list_is_visible_as_zero_total(self) -> None:
        assert collect_domain_metrics([]) == {
            "certinext.domains.total": 0,
            "certinext.domains.unverified": 0,
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
    """Expiry metrics, measured separately for token and persist domains."""

    def test_expiring_window_and_min_days(self) -> None:
        token = [
            _domain("soon.edu", valid_till="2026-07-18T12:00:00Z"),      # +5d → expiring
            _domain("later.edu", valid_till="2026-08-22T12:00:00Z"),     # +40d → fine
            _domain("lapsed.edu", valid_till="2026-07-12T12:00:00Z"),    # -1d → expiring
            _domain("unknown.edu"),                                      # no validTill → excluded
        ]
        metrics = collect_expiry_metrics(token, [], 14, now=_NOW)
        assert metrics[KEY_EXPIRING] == 2
        assert metrics[KEY_MIN_DAYS_LEFT] == -1.0
        # unknown.edu has no expiry, so it is not part of min_days_left's set.
        assert metrics[KEY_TOKEN_DOMAINS] == 3
        assert KEY_PERSIST_MIN_DAYS_LEFT not in metrics

    def test_boundary_day_counts_as_expiring(self) -> None:
        token = [_domain("edge.edu", valid_till="2026-07-27T12:00:00Z")]  # exactly +14d
        metrics = collect_expiry_metrics(token, [], 14, now=_NOW)
        assert metrics[KEY_EXPIRING] == 1
        assert metrics[KEY_MIN_DAYS_LEFT] == 14.0

    def test_no_known_expiries_omits_min_days(self) -> None:
        metrics = collect_expiry_metrics([_domain("unknown.edu")], [], 14, now=_NOW)
        assert metrics == {KEY_EXPIRING: 0, KEY_TOKEN_DOMAINS: 0}

    def test_no_verified_domains(self) -> None:
        assert collect_expiry_metrics([], [], 14, now=_NOW) == {
            "certinext.dcv.expiring": 0,
            "certinext.dcv.token_domains": 0,
        }

    def test_persist_domains_never_count_as_expiring(self) -> None:
        # A persist domain's validTill is a ~10-day reuse clock; lapsing is
        # routine, so it must not feed the token expiring/min-days triggers.
        token = [_domain("token.edu", valid_till="2026-08-22T12:00:00Z")]     # +40d
        persist = [
            _domain("persist-a.edu", valid_till="2026-07-12T12:00:00Z"),       # -1d
            _domain("persist-b.edu", valid_till="2026-07-15T12:00:00Z"),       # +2d
        ]
        metrics = collect_expiry_metrics(token, persist, 14, now=_NOW)
        assert metrics[KEY_EXPIRING] == 0
        assert metrics[KEY_MIN_DAYS_LEFT] == 40.0
        assert metrics[KEY_TOKEN_DOMAINS] == 1
        assert metrics[KEY_PERSIST_MIN_DAYS_LEFT] == -1.0

    def test_all_persist_sends_no_token_min_days(self) -> None:
        # token_domains=0 is what gates the days-left triggers off, so a
        # stale last() of an old min_days_left cannot keep them firing.
        persist = [_domain("persist.edu", valid_till="2026-07-15T12:00:00Z")]
        metrics = collect_expiry_metrics([], persist, 14, now=_NOW)
        assert metrics == {
            KEY_EXPIRING: 0,
            KEY_TOKEN_DOMAINS: 0,
            KEY_PERSIST_MIN_DAYS_LEFT: 2.0,
        }


# Placeholder only: the real per-account value must never land in this
# repo (it mirrors publicly — certinext-zabbix#12).
_PERSIST_VALUE = "ca.example; accounturi=https://ca.example.invalid/acct/PLACEHOLDER"


def _dcv_info(method: str, persist_value: str | None = _PERSIST_VALUE) -> DcvInfo:
    """Build a real ``DcvInfo`` like an SSL.com ``get_dcv()`` response.

    Args:
        method: Upper-case DCV method (the selected option).
        persist_value: ``recordValue`` of the offered DNS-PERSIST option, or
            None to offer no persist option at all.

    Returns:
        The parsed DcvInfo.
    """
    options: list[dict[str, Any]] = [
        {"optionId": "txt", "method": "dns-txt", "recordValue": "token123"},
    ]
    if persist_value is not None:
        options.append({"optionId": "persist", "method": "dns-persist", "recordValue": persist_value})
    selected = next((o for o in options if o["method"].upper() == method), options[0])
    return DcvInfo.from_wire({
        "method": method, "validatedBy": "SSLCOM", "optionId": selected["optionId"],
        "recordValue": selected["recordValue"], "options": options,
    })


def _method_domain(
    name: str, method: str = "DNS-TXT", persist_value: str | None = _PERSIST_VALUE,
) -> MagicMock:
    """Build a mock domain whose ``get_dcv()`` reports *method*.

    Args:
        name: Domain name.
        method: Upper-case DCV method returned as ``DcvInfo.method``.
        persist_value: The DNS-PERSIST option's record value, or None for
            no persist option.

    Returns:
        A MagicMock with ``name`` and a ``get_dcv`` returning a DcvInfo.
    """
    domain = MagicMock()
    domain.name = name
    domain.get_dcv.return_value = _dcv_info(method, persist_value)
    return domain


class TestFetchDcvMethods:
    """Splitting the scoped domains by DCV method via get_dcv()."""

    def test_splits_persist_from_every_token_method(self) -> None:
        persist = _method_domain("persist.edu", DCV_METHOD_PERSIST)
        txt = _method_domain("txt.edu", "DNS-TXT")
        http = _method_domain("http.edu", "HTTP-URL")
        cname = _method_domain("cname.edu", "DNS-CNAME")
        split = fetch_dcv_methods([txt, persist, http, cname])
        assert split.persist == [persist]
        assert split.token == [txt, http, cname]
        assert split.failed == []

    def test_retries_transient_failure_then_succeeds(self) -> None:
        flaky = _method_domain("flaky.edu", DCV_METHOD_PERSIST)
        flaky.get_dcv.side_effect = [
            httpx.ReadTimeout("timed out"), _dcv_info(DCV_METHOD_PERSIST),
        ]
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            split = fetch_dcv_methods([flaky], retry_delay=5.0)
        assert split.persist == [flaky]
        assert flaky.get_dcv.call_count == 2
        mock_sleep.assert_called_once_with(5.0)

    def test_exhausted_retries_land_in_failed_and_the_rest_continue(self) -> None:
        bad = _method_domain("bad.edu")
        bad.get_dcv.side_effect = CertiNextAPIError(503, "service unavailable")
        good = _method_domain("good.edu", DCV_METHOD_PERSIST)
        with patch("certinext_zabbix.zabbix_push.time.sleep"):
            split = fetch_dcv_methods([bad, good], attempts=2)
        assert split.failed == [bad]
        assert bad.get_dcv.call_count == 2
        assert split.persist == [good]

    def test_unexpected_error_is_not_retried_but_still_failed(self) -> None:
        weird = _method_domain("weird.edu")
        weird.get_dcv.side_effect = RuntimeError("surprise")
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            split = fetch_dcv_methods([weird])
        assert split.failed == [weird]
        assert weird.get_dcv.call_count == 1
        mock_sleep.assert_not_called()

    def test_persist_values_parallel_to_persist(self) -> None:
        a = _method_domain("a.edu", DCV_METHOD_PERSIST)
        txt = _method_domain("txt.edu", "DNS-TXT")
        b = _method_domain("b.edu", DCV_METHOD_PERSIST, persist_value=None)
        split = fetch_dcv_methods([a, txt, b])
        assert split.persist == [a, b]
        assert split.persist_values == [_PERSIST_VALUE, ""]

    def test_missing_persist_value_is_not_a_failed_domain(self) -> None:
        # The value read must never withhold discovery (#12).
        d = _method_domain("p.edu", DCV_METHOD_PERSIST)
        d.get_dcv.return_value = SimpleNamespace(method=DCV_METHOD_PERSIST)
        split = fetch_dcv_methods([d])
        assert split.persist == [d]
        assert split.failed == []
        assert split.persist_values == [""]


class TestPersistRecordValue:
    """Reading and normalising the DNS-PERSIST option's record value."""

    def test_prefers_the_persist_option(self) -> None:
        assert persist_record_value(_dcv_info(DCV_METHOD_PERSIST)) == _PERSIST_VALUE

    def test_falls_back_to_selected_record_value(self) -> None:
        info = DcvInfo.from_wire({
            "method": "dns-persist", "validatedBy": "SSLCOM", "recordValue": _PERSIST_VALUE,
        })
        assert persist_record_value(info) == _PERSIST_VALUE

    def test_options_without_persist_entry_fail_closed(self) -> None:
        # The top-level value then belongs to some other option (a token);
        # sending it would fail open.
        info = _dcv_info(DCV_METHOD_PERSIST, persist_value=None)
        assert info.record_value == "token123"
        assert persist_record_value(info) == ""

    @pytest.mark.parametrize("raw", [
        _PERSIST_VALUE, f'"{_PERSIST_VALUE}"', f"  {_PERSIST_VALUE}\n", f'  " {_PERSIST_VALUE} " ',
    ])
    def test_normalises_to_unquoted_form(self, raw: str) -> None:
        # {#PERSIST.EXPECTED} holds the value without quotes; the template adds them.
        assert normalise_persist_value(raw) == _PERSIST_VALUE

    @pytest.mark.parametrize("raw", [None, "", "   ", '""'])
    def test_blank_is_empty(self, raw: str | None) -> None:
        assert normalise_persist_value(raw) == ""

    def test_lone_quote_kept(self) -> None:
        assert normalise_persist_value('"') == '"'


class TestPersistExpectedByApex:
    """Each apex gets the CA's value, or a never-matching placeholder (certinext-zabbix#12)."""

    _OTHER = "other.example; accounturi=https://other.example.invalid/acct/2"

    def test_each_apex_gets_its_own_value(self) -> None:
        # No account-wide assumption: different apexes may differ.
        result = persist_expected_by_apex(
            [_domain("a.edu"), _domain("b.edu")], [_PERSIST_VALUE, self._OTHER],
        )
        assert result.values == {"a.edu": _PERSIST_VALUE, "b.edu": self._OTHER}
        assert result.problems == {}

    def test_no_persist_domains_is_empty_not_an_error(self) -> None:
        assert persist_expected_by_apex([], []) == ({}, {})

    def test_missing_value_gets_placeholder_and_problem(self) -> None:
        result = persist_expected_by_apex([_domain("a.edu"), _domain("b.edu")], [_PERSIST_VALUE, ""])
        assert result.values == {"a.edu": _PERSIST_VALUE, "b.edu": PERSIST_VALUE_UNAVAILABLE}
        assert set(result.problems) == {"b.edu"}

    def test_records_disagreeing_for_one_apex_fail_closed(self) -> None:
        result = persist_expected_by_apex(
            [_domain("a.edu"), _domain("a.edu")], [_PERSIST_VALUE, self._OTHER],
        )
        assert result.values == {"a.edu": PERSIST_VALUE_UNAVAILABLE}
        assert "disagree" in result.problems["a.edu"]

    def test_duplicate_records_that_agree_are_fine(self) -> None:
        result = persist_expected_by_apex(
            [_domain("a.edu"), _domain("a.edu")], [_PERSIST_VALUE, _PERSIST_VALUE],
        )
        assert result == ({"a.edu": _PERSIST_VALUE}, {})

    @pytest.mark.parametrize("bad", ['ssl.com; x="y"', "ssl.com; x=\\y"])
    def test_quote_or_backslash_is_refused(self, bad: str) -> None:
        # Either would have to survive LLD substitution into a quoted
        # trigger-function parameter.
        result = persist_expected_by_apex([_domain("a.edu")], [bad])
        assert result.values == {"a.edu": PERSIST_VALUE_UNAVAILABLE}
        assert "a.edu" in result.problems

    def test_nameless_record_is_dropped_like_discovery(self) -> None:
        result = persist_expected_by_apex([_domain(None), _domain("a.edu")], ["", _PERSIST_VALUE])  # type: ignore[arg-type]
        assert result == ({"a.edu": _PERSIST_VALUE}, {})

    def test_keys_always_equal_apex_names(self) -> None:
        # Every apex keeps its row, problem or not, so none loses its checks.
        persist = [_domain("b.edu"), _domain("a.edu"), _domain("c.edu")]
        result = persist_expected_by_apex(persist, [_PERSIST_VALUE, "", self._OTHER])
        assert sorted(result.values) == persist_apex_names(persist)

    def test_placeholder_cannot_match_a_real_record(self) -> None:
        assert '"' not in PERSIST_VALUE_UNAVAILABLE
        assert PERSIST_VALUE_UNAVAILABLE not in _PERSIST_VALUE

    def test_length_mismatch_is_a_bug_not_a_silent_pass(self) -> None:
        with pytest.raises(ValueError):
            persist_expected_by_apex([_domain("a.edu")], [])


class TestPersistDiscoveryValue:
    """The LLD JSON the template's {#APEX}/{#PERSIST.EXPECTED} prototypes are built from."""

    def test_sorted_rows_carry_the_expected_value(self) -> None:
        value = persist_discovery_value({"b.example.edu": "vb", "a.example.edu": "va"})
        assert json.loads(value) == [
            {"{#APEX}": "a.example.edu", "{#PERSIST.EXPECTED}": "va"},
            {"{#APEX}": "b.example.edu", "{#PERSIST.EXPECTED}": "vb"},
        ]

    def test_empty_is_an_empty_array(self) -> None:
        # [] is a complete answer ("no persist domains"), not a missing one.
        assert persist_discovery_value({}) == "[]"

    def test_apex_names_match_discovery_rows(self) -> None:
        # The apex-count metric is len() of these, so it must equal the rows.
        persist = [_domain("b.example.edu"), _domain("a.example.edu"), _domain("b.example.edu")]
        names = persist_apex_names(persist)
        assert names == ["a.example.edu", "b.example.edu"]
        expected = persist_expected_by_apex(persist, [_PERSIST_VALUE] * 3)
        assert [r["{#APEX}"] for r in json.loads(persist_discovery_value(expected.values))] == names


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

    def test_splits_every_bucket(self) -> None:
        buckets = bucket_orders([
            _order("Order Fulfilled", certificate_expiry_date="2027-01-01T00:00:00"),
            _order("Order Accepted"),
            _order("Order Accepted", certificate_expiry_date="2027-01-01T00:00:00"),
            _order("Order Cancelled"),
            _order("Order Something Unknown"),
        ])
        assert len(buckets.issued) == 1
        assert len(buckets.unissued) == 1
        assert len(buckets.undownloaded) == 1
        assert len(buckets.cancelled) == 1
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

    def test_in_progress_is_in_flight_not_failed(self) -> None:
        """'Order In-Progress' buckets like 'Order Accepted', not as a failure.

        Regression guard for sysadmin/certinext-zabbix#5. This value was
        observed in prod on 2026-08-07 and fell through the failed
        catch-all, which both inflated failed_recent and kept a genuinely
        stuck order out of unissued, where the stuck-age trigger looks.
        """
        awaiting = _order("Order In-Progress")
        generated = _order(
            "Order In-Progress", certificate_expiry_date="2027-01-01T00:00:00",
        )
        buckets = bucket_orders([awaiting, generated])
        assert buckets.unissued == [awaiting]
        assert buckets.undownloaded == [generated]
        assert not buckets.failed
        assert not buckets.issued

    def test_cancelled_is_not_a_failure(self) -> None:
        """Cancelling an order is routine admin work, not an incident.

        Every row ever observed in the failed bucket in production was a
        cancellation, so counting them would have held the failed-recent
        trigger in PROBLEM 83% of the measured period while signalling
        nothing. See docs/adr/0010-cancelled-orders-are-not-failures.md.
        """
        cancelled = _order("Order Cancelled", order_date="2026-08-16T00:00:00")
        buckets = bucket_orders([cancelled])
        assert buckets.cancelled == [cancelled]
        assert not buckets.failed

    def test_cancelled_excluded_from_failed_recent_metric(self) -> None:
        # End-to-end guard: a fresh cancellation must not raise the metric
        # the WARNING trigger reads, however recent it is.
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        buckets = bucket_orders([_order("Order Cancelled", order_date="2026-08-16T00:00:00")])
        metrics = collect_order_metrics(
            buckets, failing_lookback_days=30, cert_expiry_days=30, now=now,
        )
        assert metrics["certinext.orders.failed_recent"] == 0

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
            unissued=[], undownloaded=[], failed=[], issued=[], cancelled=[],
        )


def _buckets(
    unissued: list[OrderRecord] | None = None,
    undownloaded: list[OrderRecord] | None = None,
    failed: list[OrderRecord] | None = None,
    issued: list[OrderRecord] | None = None,
    cancelled: list[OrderRecord] | None = None,
) -> OrderBuckets:
    """Build an OrderBuckets with only the buckets a test cares about.

    Args:
        unissued: Orders accepted with no certificate generated.
        undownloaded: Orders whose certificate exists but was never fetched.
        failed: Orders in a terminal non-fulfilled state.
        issued: Orders fulfilled (certificate generated and downloaded).
        cancelled: Orders deliberately cancelled — terminal but routine.

    Returns:
        An OrderBuckets with unsupplied buckets empty.
    """
    return OrderBuckets(
        unissued=unissued or [], undownloaded=undownloaded or [],
        failed=failed or [], issued=issued or [], cancelled=cancelled or [],
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
            failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[KEY_ORDERS_UNISSUED] == 2
        assert metrics[KEY_ORDERS_UNDOWNLOADED] == 1

    def test_failed_recent_excludes_orders_outside_lookback(self) -> None:
        failed = [
            _order(order_date="2026-07-01T00:00:00"),   # 12d old — within 30d lookback
            _order(order_date="2025-01-01T00:00:00"),    # ancient — outside lookback
        ]
        metrics = collect_order_metrics(
            _buckets(failed=failed),
            failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[KEY_ORDERS_FAILED_RECENT] == 1

    def test_failed_recent_boundary_day_counts(self) -> None:
        failed = [_order(order_date="2026-06-13T12:00:00")]  # exactly 30d before _NOW
        metrics = collect_order_metrics(
            _buckets(failed=failed),
            failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[KEY_ORDERS_FAILED_RECENT] == 1

    def test_days_since_issued_uses_max_order_date(self) -> None:
        issued = [
            _order(order_date="2026-07-10T12:00:00"),  # 3d ago
            _order(order_date="2026-07-01T12:00:00"),  # 12d ago — not the max
        ]
        metrics = collect_order_metrics(
            _buckets(issued=issued),
            failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[KEY_ORDERS_DAYS_SINCE_ISSUED] == 3.0

    def test_days_since_issued_counts_undownloaded_certs_too(self) -> None:
        # The CA issued it; nobody fetched it. That still proves issuance is
        # working, which is the only thing this metric claims to measure.
        metrics = collect_order_metrics(
            _buckets(
                issued=[_order(order_date="2026-07-01T12:00:00")],       # 12d ago
                undownloaded=[_order(order_date="2026-07-10T12:00:00")],  # 3d ago
            ),
            failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics[KEY_ORDERS_DAYS_SINCE_ISSUED] == 3.0

    def test_no_issued_orders_omits_days_since_issued(self) -> None:
        metrics = collect_order_metrics(
            _buckets(), failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert KEY_ORDERS_DAYS_SINCE_ISSUED not in metrics

    def test_issued_order_with_no_order_date_is_excluded(self) -> None:
        metrics = collect_order_metrics(
            _buckets(issued=[_order()]),
            failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert KEY_ORDERS_DAYS_SINCE_ISSUED not in metrics

    def test_empty_buckets_still_report_every_count(self) -> None:
        metrics = collect_order_metrics(
            _buckets(), failing_lookback_days=30, cert_expiry_days=30, now=_NOW,
        )
        assert metrics == {
            KEY_ORDERS_UNISSUED: 0,
            KEY_ORDERS_UNDOWNLOADED: 0,
            KEY_ORDERS_FAILED_RECENT: 0,
            KEY_ORDERS_EXPIRING: 0,
        }

    def test_defaults_now_to_current_time(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        metrics = collect_order_metrics(
            _buckets(issued=[_order(order_date=recent.isoformat())]),
            failing_lookback_days=30, cert_expiry_days=30,
        )
        assert metrics[KEY_ORDERS_DAYS_SINCE_ISSUED] == pytest.approx(1.0, abs=0.01)

    def test_expiring_counts_issued_certs_within_lead_time(self) -> None:
        issued = [
            _order(certificate_expiry_date="2026-07-20T12:00:00"),  # +7d → expiring
            _order(certificate_expiry_date="2026-09-01T12:00:00"),  # +50d → fine
            _order(certificate_expiry_date="2026-07-10T12:00:00"),  # -3d → expiring (lapsed)
            _order(),                                                # no expiry date → excluded
        ]
        metrics = collect_order_metrics(
            _buckets(issued=issued),
            failing_lookback_days=30, cert_expiry_days=14, now=_NOW,
        )
        assert metrics[KEY_ORDERS_EXPIRING] == 2

    def test_expiring_counts_undownloaded_certs_too(self) -> None:
        # An unfetched certificate still expires on the CA's schedule.
        metrics = collect_order_metrics(
            _buckets(
                issued=[_order(certificate_expiry_date="2026-07-20T12:00:00")],
                undownloaded=[_order(certificate_expiry_date="2026-07-18T12:00:00")],
            ),
            failing_lookback_days=30, cert_expiry_days=14, now=_NOW,
        )
        assert metrics[KEY_ORDERS_EXPIRING] == 2

    def test_expiring_boundary_day_counts(self) -> None:
        issued = [_order(certificate_expiry_date="2026-07-27T12:00:00")]  # exactly +14d
        metrics = collect_order_metrics(
            _buckets(issued=issued),
            failing_lookback_days=30, cert_expiry_days=14, now=_NOW,
        )
        assert metrics[KEY_ORDERS_EXPIRING] == 1


class TestPushMetrics:
    """The trapper send stringifies values and targets the right server."""

    def test_items_and_destination(self) -> None:
        mock_sender = MagicMock()
        with patch("certinext_zabbix.zabbix_push.Sender",
                   return_value=mock_sender) as mock_cls:
            push_metrics(
                {
                    KEY_UNVERIFIED: 3,
                    KEY_TOTAL: 120,
                    KEY_MIN_DAYS_LEFT: 20.66,
                },
                zabbix_host="host.example.edu",
                server="zabbix.example.edu", port=10051,
            )
        mock_cls.assert_called_once_with(server="zabbix.example.edu", port=10051, timeout=10)
        (items,) = mock_sender.send.call_args.args
        assert [(i.host, i.key, i.value) for i in items] == [
            ("host.example.edu", "certinext.dcv.min_days_left", "20.66"),
            ("host.example.edu", "certinext.domains.total", "120"),
            ("host.example.edu", "certinext.domains.unverified", "3"),
        ]

    def test_str_values_sent_verbatim(self) -> None:
        # The TEXT env item and the LLD JSON must reach Zabbix unchanged —
        # str() of a str, not a repr() that would add quotes.
        discovery = '[{"{#APEX}": "example.edu"}]'
        mock_sender = MagicMock()
        with patch("certinext_zabbix.zabbix_push.Sender", return_value=mock_sender):
            push_metrics(
                {KEY_ENV: "sandbox", KEY_PERSIST_DISCOVERY: discovery},
                zabbix_host="h.example.edu", server="zbx.example.edu", port=10051,
            )
        (items,) = mock_sender.send.call_args.args
        assert {i.key: i.value for i in items} == {
            "certinext.env": "sandbox",
            "certinext.dcv.persist.discovery": discovery,
        }

    def test_timeout_forwarded(self) -> None:
        mock_sender = MagicMock()
        with patch("certinext_zabbix.zabbix_push.Sender",
                   return_value=mock_sender) as mock_cls:
            push_metrics(
                {KEY_TOTAL: 1},
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
                {KEY_TOTAL: 1},
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
                {KEY_TOTAL: 1},
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
                {KEY_TOTAL: 1},
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
                {KEY_TOTAL: 1},
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
                {KEY_TOTAL: 1},
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

    def test_ca_filtered_list_keeps_child_of_other_ca_apex(self) -> None:
        # ADR 0011: the CLI filters by CA before scoping. The SSL.com child's
        # only registered ancestor is an emSign record, so in the SSL.com-only
        # list it has no covering ancestor and must be kept.
        emsign_apex = _domain("example.edu")
        sslcom_child = _domain("dept.example.edu", validated_by="SSLCOM")
        sslcom_only = [d for d in (emsign_apex, sslcom_child) if d.ca == "SSLCOM"]
        assert scope_domains(sslcom_only, DomainScope.TOP) == [sslcom_child]

    def test_mixed_ca_list_hides_child_behind_other_ca_apex(self) -> None:
        # The hazard the filter-first ordering avoids: coverage is org-scoped
        # but not CA-aware, so an unfiltered mixed list drops the SSL.com child
        # as "covered" by the emSign apex.
        emsign_apex = _domain("example.edu")
        sslcom_child = _domain("dept.example.edu", validated_by="SSLCOM")
        assert scope_domains([emsign_apex, sslcom_child], DomainScope.TOP) == [emsign_apex]

    def test_ca_filtered_list_still_scopes_same_ca_subdomain(self) -> None:
        apex = _domain("example.edu", validated_by="SSLCOM")
        sub = _domain("dept.example.edu", validated_by="SSLCOM")
        assert scope_domains([apex, sub], DomainScope.TOP) == [apex]


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
