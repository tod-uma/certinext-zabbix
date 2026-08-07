"""Mocked unit tests for the certinext-zabbix-push CLI — option flow and run outcomes.

The metric math is covered by tests/test_zabbix_push.py; these tests pin the
CLI wiring: connection options must reach resolve_connection/build_session,
Zabbix destination flags and env vars must reach push_metrics, the expiry
path must refresh domains and honor the skip-on-refresh-failure policy, and
exit codes / stdout JSON must reflect the outcome. No config files, keyrings,
or network are touched.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from certinext.cli_support import LogFormat, LogMode
from certinext.orders import OrderRecord
from typer.testing import CliRunner
from zabbix_utils.exceptions import ProcessingError

from certinext_zabbix._cli_shared import run_lock
from certinext_zabbix.zabbix_push import DomainScope
from certinext_zabbix.zabbix_push_cli import app

runner = CliRunner()

_OK_RESPONSE = SimpleNamespace(processed=2, failed=0, total=2)
_TRACEBACK_HINT = "re-run with -vvv for the full traceback"
_DEFAULT_TEST_SERVER = "zbx.example.edu"


def _caught_kwargs(exc: BaseException, **context: Any) -> dict[str, Any]:
    """Build the kwargs log_caught_exception's concise line adds for *exc*."""
    return {"error": str(exc), "error_type": type(exc).__name__, "hint": _TRACEBACK_HINT, **context}


def _verified_domain(name: str, expires: datetime | None = None) -> MagicMock:
    """Build a mock ACTIVE+VERIFIED domain with a no-op refresh.

    Args:
        name: Domain name.
        expires: Value for ``dcv_expires`` after refresh, or None.

    Returns:
        A MagicMock with the Domain attributes the CLI touches.
    """
    domain = MagicMock(
        status="ACTIVE", dcv_status="VERIFIED", needs_dcv=False, dcv_expires=expires,
    )
    # `name` is a reserved MagicMock constructor kwarg — set the attribute directly.
    domain.name = name
    # No covering ancestor by default, so the default (top) --domain-scope
    # keeps this domain instead of silently filtering it out via
    # certinext.filter_needs_dcv's dcv_covering_parent() check.
    domain.dcv_covering_parent.return_value = None
    return domain


def _order(order_status: str = "Order Accepted", order_date: str | None = None) -> OrderRecord:
    """Build an OrderRecord from wire-format fields for order-health tests.

    Args:
        order_status: Value for the ``orderStatus`` wire field.
        order_date: ISO timestamp for ``orderDate``, or None to omit it.

    Returns:
        A validated OrderRecord (no API client attached — field access only).
    """
    payload: dict[str, Any] = {"orderStatus": order_status}
    if order_date is not None:
        payload["orderDate"] = order_date
    return OrderRecord.model_validate(payload)


def _run(
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
    domains: list[Any] | None = None,
    orders_by_status: dict[str, list[Any]] | None = None,
    response: Any = _OK_RESPONSE,
    sandbox: bool = False,
) -> tuple[Any, SimpleNamespace]:
    """Invoke certinext-zabbix-push with all external layers mocked out.

    resolve_connection / build_session / push_metrics / configure_logging are
    patched in the CLI module's namespace. The mocked session returns
    *domains* (default empty) and, for the orders accessor, looks up each
    status query in *orders_by_status* (default: every status returns an
    empty list). push_metrics returns *response*, and the resolved
    connection reports *sandbox* (drives the [env] key parameter).
    ``ZABBIX_SERVER`` defaults to a test value since the CLI now requires it
    with no built-in default — pass ``env={"ZABBIX_SERVER": ...}`` to
    override, or ``env={"ZABBIX_SERVER": None}`` to unset it entirely.

    Returns:
        The CliRunner result and a namespace of the mocks for assertions.
    """
    mock_sess = MagicMock()
    mock_sess.domain.get_list.return_value = domains if domains is not None else []
    by_status = orders_by_status or {}
    mock_sess.orders.get_list.side_effect = lambda status: by_status.get(status, [])
    mock_conn = MagicMock(sandbox=sandbox)
    full_env = {"ZABBIX_SERVER": _DEFAULT_TEST_SERVER, **(env or {})}

    with patch("certinext_zabbix.zabbix_push_cli.resolve_connection",
               return_value=mock_conn) as mock_resolve, \
         patch("certinext_zabbix.zabbix_push_cli.build_session",
               return_value=mock_sess) as mock_build, \
         patch("certinext_zabbix.zabbix_push_cli.push_metrics",
               return_value=response) as mock_push, \
         patch("certinext_zabbix.zabbix_push_cli.configure_logging") as mock_logging, \
         patch("certinext_zabbix.zabbix_push_cli.log") as mock_log:
        result = runner.invoke(app, argv or [], env=full_env)

    return result, SimpleNamespace(
        sess=mock_sess, conn=mock_conn, resolve=mock_resolve,
        build=mock_build, push=mock_push, logging=mock_logging, log=mock_log,
    )


def _stdout_json(result: Any) -> dict[str, Any]:
    """Parse the JSON object the CLI prints to stdout as its data output."""
    data = json.loads(result.output.strip().splitlines()[-1])
    assert isinstance(data, dict)
    return data


class TestCliSupportContract:
    """Option values must flow into the certinext cli_support layer unchanged."""

    def test_clean_run_exits_zero(self) -> None:
        result, _ = _run()
        assert result.exit_code == 0

    def test_defaults_reach_resolve_connection(self) -> None:
        result, mocks = _run()
        assert result.exit_code == 0
        mocks.resolve.assert_called_once_with(
            profile=None, sandbox=False, base_url=None, token_url=None,
        )

    def test_credentials_reach_build_session(self) -> None:
        _, mocks = _run(argv=["--client-id", "acct-1", "--client-secret", "s3cret"])
        mocks.build.assert_called_once_with(
            mocks.conn, account_number="acct-1", client_secret="s3cret",
        )

    def test_verbose_count_reaches_configure_logging(self) -> None:
        _, mocks = _run(argv=["-vvv"])
        mocks.logging.assert_called_once_with(3, LogFormat.LOGFMT, LogMode.AUTO, None)

    def test_log_mode_and_debug_log_path_flags_reach_configure_logging(self) -> None:
        _, mocks = _run(argv=["--log-mode", "syslog", "--debug-log-path", "/tmp/zbx-debug.log"])
        mocks.logging.assert_called_once_with(
            0, LogFormat.LOGFMT, LogMode.SYSLOG, Path("/tmp/zbx-debug.log"),
        )

    def test_debug_log_path_env_var_reaches_configure_logging(self) -> None:
        _, mocks = _run(env={"CERTINEXT_ZABBIX_DEBUG_LOG": "/tmp/env-debug.log"})
        mocks.logging.assert_called_once_with(
            0, LogFormat.LOGFMT, LogMode.AUTO, Path("/tmp/env-debug.log"),
        )


class TestZabbixDestination:
    """Zabbix flags and env vars must reach push_metrics."""

    def test_defaults(self) -> None:
        _, mocks = _run()
        kwargs = mocks.push.call_args.kwargs
        assert kwargs["server"] == _DEFAULT_TEST_SERVER
        assert kwargs["port"] == 10051
        assert kwargs["timeout"] == 10
        assert kwargs["zabbix_host"]  # resolved from the local FQDN

    def test_flags_forwarded(self) -> None:
        _, mocks = _run(argv=["--zabbix-server", "zbx.example.edu",
                              "--zabbix-port", "10099",
                              "--zabbix-host", "monitored.example.edu",
                              "--zabbix-timeout", "30"])
        kwargs = mocks.push.call_args.kwargs
        assert kwargs["server"] == "zbx.example.edu"
        assert kwargs["port"] == 10099
        assert kwargs["zabbix_host"] == "monitored.example.edu"
        assert kwargs["timeout"] == 30

    def test_env_vars_forwarded(self) -> None:
        _, mocks = _run(env={"ZABBIX_SERVER": "zbx-env.example.edu",
                             "ZABBIX_HOSTNAME": "host-env.example.edu",
                             "ZABBIX_TIMEOUT": "7"})
        kwargs = mocks.push.call_args.kwargs
        assert kwargs["server"] == "zbx-env.example.edu"
        assert kwargs["zabbix_host"] == "host-env.example.edu"
        assert kwargs["timeout"] == 7

    def test_missing_server_and_env_var_errors(self) -> None:
        # No built-in default here (unlike the UMS-internal deployment,
        # which supplies its own) — an unset server must fail fast rather
        # than silently target some particular host.
        with patch("certinext_zabbix.zabbix_push_cli.push_metrics") as mock_push:
            result = runner.invoke(app, [], env={"ZABBIX_SERVER": None})
        assert result.exit_code != 0
        assert "--zabbix-server" in result.output
        mock_push.assert_not_called()


class TestRunOutcomes:
    """Exit codes and stdout JSON must reflect what happened."""

    def test_metrics_pushed_and_printed(self) -> None:
        result, mocks = _run()
        assert result.exit_code == 0
        (metrics,) = mocks.push.call_args.args
        assert metrics == {
            "certinext.domains.total[prod]": 0,
            "certinext.domains.unverified[prod]": 0,
        }
        data = _stdout_json(result)
        assert data["sent"] is True
        assert data["env"] == "prod"
        assert data["metrics"] == metrics

    def test_sandbox_connection_uses_sandbox_keys(self) -> None:
        result, mocks = _run(sandbox=True)
        assert result.exit_code == 0
        (metrics,) = mocks.push.call_args.args
        # The [env] parameter comes from the resolved connection, so sandbox
        # data can never land in the prod items.
        assert metrics == {
            "certinext.domains.total[sandbox]": 0,
            "certinext.domains.unverified[sandbox]": 0,
        }
        assert _stdout_json(result)["env"] == "sandbox"

    def test_dry_run_pushes_nothing(self) -> None:
        result, mocks = _run(argv=["--dry-run"])
        assert result.exit_code == 0
        mocks.push.assert_not_called()
        assert _stdout_json(result)["sent"] is False

    def test_rejected_values_exit_one(self) -> None:
        result, _ = _run(response=SimpleNamespace(processed=1, failed=1, total=2))
        assert result.exit_code == 1
        assert _stdout_json(result)["sent"] is False

    def test_list_failure_exits_one_with_no_stdout(self) -> None:
        mock_sess = MagicMock()
        mock_sess.domain.get_list.side_effect = RuntimeError("boom")
        with patch("certinext_zabbix.zabbix_push_cli.resolve_connection",
                   return_value=MagicMock(sandbox=False)), \
             patch("certinext_zabbix.zabbix_push_cli.build_session",
                   return_value=mock_sess), \
             patch("certinext_zabbix.zabbix_push_cli.push_metrics") as mock_push, \
             patch("certinext_zabbix.zabbix_push_cli.configure_logging"), \
             patch("certinext_zabbix.zabbix_push_cli.log"):
            result = runner.invoke(app, [], env={"ZABBIX_SERVER": _DEFAULT_TEST_SERVER})
        assert result.exit_code == 1
        mock_push.assert_not_called()
        assert result.output.strip() == ""

    def test_unclassified_failure_logs_concisely_not_a_traceback(self) -> None:
        """Nothing this script doesn't already have a named branch for —
        e.g. domain listing raising something outside RuntimeError/
        CertiNextAPIError — should still fall through to one clean line,
        never log.exception's full traceback."""
        mock_sess = MagicMock()
        mock_sess.domain.get_list.side_effect = ValueError("unparseable response")
        with patch("certinext_zabbix.zabbix_push_cli.resolve_connection",
                   return_value=MagicMock(sandbox=False)), \
             patch("certinext_zabbix.zabbix_push_cli.build_session",
                   return_value=mock_sess), \
             patch("certinext_zabbix.zabbix_push_cli.push_metrics"), \
             patch("certinext_zabbix.zabbix_push_cli.configure_logging"), \
             patch("certinext_zabbix.zabbix_push_cli.log") as mock_log:
            result = runner.invoke(app, [], env={"ZABBIX_SERVER": _DEFAULT_TEST_SERVER})
        assert result.exit_code == 1
        exc = ValueError("unparseable response")
        mock_log.error.assert_any_call("Unexpected error", **_caught_kwargs(exc))
        mock_log.debug.assert_any_call("Unexpected error", exc_info=True)
        mock_log.exception.assert_not_called()


class TestExpiryPath:
    """--expiry-days refreshes verified domains and honors the skip policy."""

    def test_expiry_metrics_included(self) -> None:
        soon = datetime.now(timezone.utc)
        domains = [_verified_domain("soon.edu", expires=soon)]
        result, mocks = _run(argv=["--expiry-days", "14"], domains=domains)
        assert result.exit_code == 0
        domains[0].refresh.assert_called_once_with()
        (metrics,) = mocks.push.call_args.args
        assert metrics["certinext.dcv.expiring[prod]"] == 1
        assert "certinext.dcv.min_days_left[prod]" in metrics

    def test_without_flag_no_refresh(self) -> None:
        domains = [_verified_domain("verified.edu")]
        _run(domains=domains)
        domains[0].refresh.assert_not_called()

    def test_refresh_failure_skips_expiry_metrics_but_pushes_rest(self) -> None:
        bad = _verified_domain("bad.edu")
        bad.refresh.side_effect = RuntimeError("api down")
        result, mocks = _run(argv=["--expiry-days", "14"], domains=[bad])
        assert result.exit_code == 1
        (metrics,) = mocks.push.call_args.args
        # A partial refresh must not push an undercounted expiring value.
        assert "certinext.dcv.expiring[prod]" not in metrics
        assert metrics["certinext.domains.total[prod]"] == 1

    def test_expected_refresh_error_logs_concisely_not_a_traceback(self) -> None:
        """A degraded API can time out on every domain in a run — one log
        line per domain via log.warning, not a full traceback via
        log.exception, or that's hours of stack traces in syslog."""
        bad = _verified_domain("timeout.edu")
        exc = httpx.ReadTimeout("timed out")
        bad.refresh.side_effect = exc
        result, mocks = _run(argv=["--expiry-days", "14"], domains=[bad])
        assert result.exit_code == 1
        mocks.log.warning.assert_any_call(
            "Failed to refresh domain", **_caught_kwargs(exc, domain="timeout.edu"),
        )
        mocks.log.debug.assert_any_call(
            "Failed to refresh domain", exc_info=True, domain="timeout.edu",
        )
        mocks.log.exception.assert_not_called()

    def test_unexpected_refresh_error_also_logs_concisely(self) -> None:
        """Anything other than the documented CertiNextAPIError/httpx.HTTPError
        contract gets the same no-traceback treatment — a clean log file
        must hold even for failures nobody anticipated."""
        bad = _verified_domain("weird.edu")
        exc = RuntimeError("api down")
        bad.refresh.side_effect = exc
        result, mocks = _run(argv=["--expiry-days", "14"], domains=[bad])
        assert result.exit_code == 1
        mocks.log.error.assert_any_call(
            "Failed to refresh domain — unexpected error",
            **_caught_kwargs(exc, domain="weird.edu"),
        )
        mocks.log.debug.assert_any_call(
            "Failed to refresh domain — unexpected error", exc_info=True, domain="weird.edu",
        )
        mocks.log.exception.assert_not_called()


class TestOrderHealthPath:
    """--order-health fetches the orders report and honors the skip policy."""

    def test_order_health_metrics_included(self) -> None:
        orders_by_status = {
            "pending-dcv": [_order("Order Accepted")],
            "rejected": [_order(order_date="2026-01-01T00:00:00")],
            "issued": [_order(order_date="2026-01-01T00:00:00")],
        }
        result, mocks = _run(argv=["--order-health"], orders_by_status=orders_by_status)
        assert result.exit_code == 0
        (metrics,) = mocks.push.call_args.args
        assert metrics["certinext.orders.pending[prod]"] == 1
        assert "certinext.orders.failed_recent[prod]" in metrics
        assert "certinext.orders.expiring[prod]" in metrics
        assert "certinext.orders.days_since_issued[prod]" in metrics

    def test_without_flag_orders_not_fetched(self) -> None:
        _, mocks = _run()
        mocks.sess.orders.get_list.assert_not_called()

    def test_fetches_all_documented_status_filters(self) -> None:
        _, mocks = _run(argv=["--order-health"])
        queried = {call.kwargs["status"] for call in mocks.sess.orders.get_list.call_args_list}
        assert queried == {
            "pending-dcv", "pending-organization-verification", "pending-csr",
            "pending-documents", "pending-agreement", "pending-approval",
            "rejected", "cancelled", "expired", "revoked",
            "issued",
        }

    def test_lookback_flag_reaches_collect_order_metrics(self) -> None:
        with patch("certinext_zabbix.zabbix_push_cli.collect_order_metrics",
                   return_value={}) as mock_collect:
            _run(argv=["--order-health", "--order-failing-lookback-days", "10"])
        assert mock_collect.call_args.kwargs["failing_lookback_days"] == 10

    def test_lookback_default_reaches_collect_order_metrics(self) -> None:
        with patch("certinext_zabbix.zabbix_push_cli.collect_order_metrics",
                   return_value={}) as mock_collect:
            _run(argv=["--order-health"])
        assert mock_collect.call_args.kwargs["failing_lookback_days"] == 30

    def test_cert_expiry_flag_reaches_collect_order_metrics(self) -> None:
        with patch("certinext_zabbix.zabbix_push_cli.collect_order_metrics",
                   return_value={}) as mock_collect:
            _run(argv=["--order-health", "--order-cert-expiry-days", "60"])
        assert mock_collect.call_args.kwargs["cert_expiry_days"] == 60

    def test_cert_expiry_default_reaches_collect_order_metrics(self) -> None:
        with patch("certinext_zabbix.zabbix_push_cli.collect_order_metrics",
                   return_value={}) as mock_collect:
            _run(argv=["--order-health"])
        assert mock_collect.call_args.kwargs["cert_expiry_days"] == 30

    def test_fetch_failure_skips_order_metrics_but_pushes_rest(self) -> None:
        mock_sess = MagicMock()
        mock_sess.domain.get_list.return_value = []
        mock_sess.orders.get_list.side_effect = RuntimeError("api down")
        with patch("certinext_zabbix.zabbix_push_cli.resolve_connection",
                   return_value=MagicMock(sandbox=False)), \
             patch("certinext_zabbix.zabbix_push_cli.build_session",
                   return_value=mock_sess), \
             patch("certinext_zabbix.zabbix_push_cli.push_metrics",
                   return_value=_OK_RESPONSE) as mock_push, \
             patch("certinext_zabbix.zabbix_push_cli.configure_logging"), \
             patch("certinext_zabbix.zabbix_push_cli.log"):
            result = runner.invoke(
                app, ["--order-health"], env={"ZABBIX_SERVER": _DEFAULT_TEST_SERVER},
            )
        assert result.exit_code == 1
        (metrics,) = mock_push.call_args.args
        assert "certinext.orders.pending[prod]" not in metrics
        assert metrics["certinext.domains.total[prod]"] == 0

    def test_expected_fetch_error_logs_concisely_not_a_traceback(self) -> None:
        exc = httpx.ReadTimeout("timed out")
        mock_sess = MagicMock()
        mock_sess.domain.get_list.return_value = []
        mock_sess.orders.get_list.side_effect = exc
        with patch("certinext_zabbix.zabbix_push_cli.resolve_connection",
                   return_value=MagicMock(sandbox=False)), \
             patch("certinext_zabbix.zabbix_push_cli.build_session",
                   return_value=mock_sess), \
             patch("certinext_zabbix.zabbix_push_cli.push_metrics",
                   return_value=_OK_RESPONSE), \
             patch("certinext_zabbix.zabbix_push_cli.configure_logging"), \
             patch("certinext_zabbix.zabbix_push.time.sleep"), \
             patch("certinext_zabbix.zabbix_push_cli.log") as mock_log:
            result = runner.invoke(
                app, ["--order-health"], env={"ZABBIX_SERVER": _DEFAULT_TEST_SERVER},
            )
        assert result.exit_code == 1
        mock_log.warning.assert_any_call(
            "Failed to fetch orders report", **_caught_kwargs(exc),
        )
        mock_log.exception.assert_not_called()

    def test_uses_expiry_lock_tier_not_a_third_lock(self) -> None:
        held = run_lock("certinext_zabbix_push_prod_expiry")
        held.acquire()
        try:
            result, mocks = _run(argv=["--order-health"])
        finally:
            held.release(force=True)
        assert result.exit_code == 0
        mocks.push.assert_not_called()


class TestZabbixUnreachable:
    """A Zabbix outage after push_metrics's own retries must log one concise
    line, not fall through to the generic exception handler's traceback."""

    def test_processing_error_logs_concisely_and_exits_one(self) -> None:
        exc = ProcessingError("Couldn't connect to all of cluster nodes")
        mock_sess = MagicMock()
        mock_sess.domain.get_list.return_value = []
        with patch("certinext_zabbix.zabbix_push_cli.resolve_connection",
                   return_value=MagicMock(sandbox=False)), \
             patch("certinext_zabbix.zabbix_push_cli.build_session",
                   return_value=mock_sess), \
             patch("certinext_zabbix.zabbix_push_cli.push_metrics", side_effect=exc), \
             patch("certinext_zabbix.zabbix_push_cli.configure_logging"), \
             patch("certinext_zabbix.zabbix_push_cli.log") as mock_log:
            result = runner.invoke(app, [], env={"ZABBIX_SERVER": _DEFAULT_TEST_SERVER})
        assert result.exit_code == 1
        event = (
            "Could not reach Zabbix trapper after retries — check server "
            "reachability, firewall, and --zabbix-server/--zabbix-port"
        )
        mock_log.error.assert_any_call(
            event, **_caught_kwargs(exc, server=_DEFAULT_TEST_SERVER, port=10051),
        )
        mock_log.debug.assert_any_call(
            event, exc_info=True, server=_DEFAULT_TEST_SERVER, port=10051,
        )
        mock_log.exception.assert_not_called()


class TestDomainScope:
    """--domain-scope must reach scope_domains and gate what gets counted."""

    def test_default_domain_scope_reaches_scope_domains(self) -> None:
        raw = [_verified_domain("a.edu")]
        with patch("certinext_zabbix.zabbix_push_cli.scope_domains",
                   return_value=[]) as mock_scope:
            result, _mocks = _run(domains=raw)
        assert result.exit_code == 0
        mock_scope.assert_called_once_with(raw, DomainScope.TOP)

    def test_domain_scope_flag_forwarded(self) -> None:
        with patch("certinext_zabbix.zabbix_push_cli.scope_domains",
                   return_value=[]) as mock_scope:
            _run(argv=["--domain-scope", "ns-boundary"])
        assert mock_scope.call_args.args[1] == DomainScope.NS_BOUNDARY

    def test_domain_scope_envvar_forwarded(self) -> None:
        with patch("certinext_zabbix.zabbix_push_cli.scope_domains",
                   return_value=[]) as mock_scope:
            _run(env={"CERTINEXT_DOMAIN_SCOPE": "all"})
        assert mock_scope.call_args.args[1] == DomainScope.ALL

    def test_scoped_count_used_in_metrics(self) -> None:
        # scope_domains trims 3 domains down to 1 — metrics must reflect the
        # scoped list, not the raw listing, for all four metrics.
        raw = [_verified_domain("a.edu"), _verified_domain("b.edu"), _verified_domain("c.edu")]
        with patch("certinext_zabbix.zabbix_push_cli.scope_domains",
                   return_value=[raw[0]]) as mock_scope:
            result, mocks = _run(domains=raw)
        assert result.exit_code == 0
        mock_scope.assert_called_once_with(raw, mock_scope.call_args.args[1])
        (metrics,) = mocks.push.call_args.args
        assert metrics["certinext.domains.total[prod]"] == 1

    def test_expiry_refresh_retries_transient_failure(self) -> None:
        # Proves the CLI calls refresh_domain (which retries), not a bare
        # d.refresh() — a single ReadTimeout must not fail the run.
        flaky = _verified_domain("flaky.edu", expires=datetime.now(timezone.utc))
        flaky.refresh.side_effect = [httpx.ReadTimeout("timed out"), None]
        with patch("certinext_zabbix.zabbix_push.time.sleep") as mock_sleep:
            result, mocks = _run(argv=["--expiry-days", "14"], domains=[flaky])
        assert result.exit_code == 0
        assert flaky.refresh.call_count == 2
        mock_sleep.assert_called_once_with(5.0)
        (metrics,) = mocks.push.call_args.args
        assert "certinext.dcv.expiring[prod]" in metrics


class TestLockScoping:
    """Regression coverage for the prod incident (2026-07-15) where the daily
    --expiry-days run and the 15-minute plain run shared one lock and one
    silently skipped the other whenever their schedules landed on the same
    quarter-hour."""

    def test_plain_lock_does_not_block_expiry_run(self) -> None:
        held = run_lock("certinext_zabbix_push_prod_plain")
        held.acquire()
        try:
            result, mocks = _run(argv=["--expiry-days", "14"])
        finally:
            held.release(force=True)
        assert result.exit_code == 0
        mocks.push.assert_called_once()

    def test_expiry_lock_does_not_block_plain_run(self) -> None:
        held = run_lock("certinext_zabbix_push_prod_expiry")
        held.acquire()
        try:
            result, mocks = _run()
        finally:
            held.release(force=True)
        assert result.exit_code == 0
        mocks.push.assert_called_once()

    def test_same_job_same_env_still_collides(self) -> None:
        held = run_lock("certinext_zabbix_push_prod_plain")
        held.acquire()
        try:
            result, mocks = _run()
        finally:
            held.release(force=True)
        assert result.exit_code == 0
        mocks.push.assert_not_called()
