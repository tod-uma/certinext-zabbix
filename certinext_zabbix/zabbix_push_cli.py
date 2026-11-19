"""Typer entry point for the ``certinext-zabbix-push`` command.

Command-line wiring only — metric computation and the trapper send live in
:mod:`certinext_zabbix.zabbix_push`. Each run lists the CertiNext
domains, computes DCV health metrics, and pushes them to Zabbix as trapper
item values; ``CertiNext DCV by Zabbix trapper`` on the Zabbix side owns
the matching items and triggers (source:
``templates/template_certinext/7.0/template_certinext.yaml``).

Designed for three schedules on the same host:

- a frequent run (no flags) pushing the cheap domain-list metrics;
- a daily run with ``--expiry-days N`` that additionally fetches
  per-domain details and pushes the DCV-expiry metrics;
- an hourly run with ``--order-health`` that fetches the whole Orders
  Report and pushes the order-health metrics.

``--validated-by CA`` (env ``CERTINEXT_VALIDATED_BY``) restricts the
domain and DCV-expiry metrics to one CA's domain records (ADR 0011).

Item keys are bare: each CertiNext environment has its own Zabbix host,
chosen per run with ``--zabbix-host`` / ``ZABBIX_HOSTNAME``. Every push also
carries ``certinext.env`` (``prod`` / ``sandbox``, derived from the resolved
connection, never user-supplied), which the template compares with the
host's ``{$CERTINEXT.ENV}`` to catch a run sent to the wrong host. Locks are
per environment *and* per job (plain
vs. ``--expiry-days`` vs. ``--order-health``), so optional sandbox
schedules never make prod runs skip a cycle, and one job's run colliding
with another's slot can't make either one skip the other.

On success the pushed metrics are printed to stdout as one JSON object
(stderr carries the logs, as usual).

Verbosity levels (cumulative):
  -v      Show configuration details and run context (correlation_id, pid)
          in interactive output.
  -vvv    Enable script-level DEBUG logging.
  -vvvv   Also enable third-party DEBUG logging (httpx wire, zabbix_utils).
"""

import json
import os
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import httpx
import structlog
import typer
from certinext.cli_options import (
    AccountNumberOption,
    BaseUrlOption,
    ClientSecretOption,
    LogFormatOption,
    LogModeOption,
    ProfileOption,
    SandboxOption,
    TokenUrlOption,
    VerboseOption,
)
from certinext.cli_support import LogFormat, LogMode, build_session, resolve_connection
from certinext.exceptions import CertiNextAPIError
from certinext.models.domains import Domain
from filelock import FileLock, Timeout
from zabbix_utils.exceptions import ProcessingError

from ._cli_shared import (
    DebugLogPathOption,
    VersionOption,
    configure_logging,
    install_sigterm_handler,
    log_caught_exception,
    run_app,
    run_lock,
)
from .zabbix_push import (
    ENV_PROD,
    ENV_SANDBOX,
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
    KEY_PERSIST_EXPECTED,
    KEY_PERSIST_MIN_DAYS_LEFT,
    KEY_TOKEN_DOMAINS,
    KEY_UNVERIFIED,
    DomainScope,
    MetricValue,
    OrderBuckets,
    PersistExpectedError,
    bucket_orders,
    collect_domain_metrics,
    collect_expiry_metrics,
    collect_order_metrics,
    fetch_dcv_methods,
    fetch_orders,
    persist_apex_names,
    persist_discovery_value,
    persist_expected_value,
    push_metrics,
    refresh_domain,
    scope_domains,
    verified_domains,
)

log = structlog.get_logger()

app = typer.Typer(add_completion=False)


def _refresh_any_failed(verified: list[Domain]) -> bool:
    """Refresh every domain's details, logging each failure once.

    Every domain is attempted even after one fails, so a run's log names
    all the failing domains rather than only the first.

    Args:
        verified: ACTIVE+VERIFIED domains to refresh in place.

    Returns:
        True when at least one refresh failed after retries. The caller
        must then skip the expiry metrics: a partial refresh would
        undercount the expiring domains, and a too-low value masks the
        very condition being monitored.
    """
    failed = False
    for d in verified:
        try:
            refresh_domain(d)
        except (CertiNextAPIError, httpx.HTTPError) as exc:
            # Expected, well-typed failure mode (timeout, rate limit,
            # transient API error): one concise line per domain.
            log_caught_exception(
                log, "Failed to refresh domain", exc, level="warning", domain=d.name,
            )
            failed = True
        except Exception as exc:
            # Not a documented failure mode of Domain.refresh(), but
            # still one concise line per domain, same as above.
            log_caught_exception(
                log, "Failed to refresh domain — unexpected error", exc, domain=d.name,
            )
            failed = True
    return failed


@app.command()
def run(
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Compute and print the metrics without sending anything to Zabbix",
    )] = False,
    version_: VersionOption = False,
    verbose: VerboseOption = 0,
    log_format: LogFormatOption = LogFormat.LOGFMT,
    log_mode: LogModeOption = LogMode.AUTO,
    debug_log_path: DebugLogPathOption = None,
    # CertiNext connection
    profile: ProfileOption = None,
    sandbox: SandboxOption = False,
    base_url: BaseUrlOption = None,
    token_url: TokenUrlOption = None,
    account_number: AccountNumberOption = None,
    client_secret: ClientSecretOption = None,
    # Domain scope
    domain_scope: Annotated[DomainScope, typer.Option(
        "--domain-scope", envvar="CERTINEXT_DOMAIN_SCOPE",
        help=(
            "Which domains to monitor. 'top' (default) excludes any domain with a "
            "registered ancestor in the account (e.g. skips dept.example.edu when "
            "example.edu is also registered) — no DNS lookups. 'ns-boundary' does the "
            "same but re-includes a domain that has its own NS records (a real DNS "
            "zone cut). 'all' monitors every domain, the old unfiltered behavior. "
            "Applies to all four metrics, not just the expiry pair — switching away "
            "from 'all' causes a one-time drop in certinext.domains.total, expected."
        ),
    )] = DomainScope.TOP,
    validated_by: Annotated[Optional[str], typer.Option(
        "--validated-by", metavar="CA", envvar="CERTINEXT_VALIDATED_BY",
        help=(
            "Only monitor domain records validated by this CA, e.g. SSLCOM or "
            "EMSIGN (case-insensitive). An organization holds one record per CA "
            "per name, so without this every CA's records are counted together. "
            "Affects the domain and DCV-expiry metrics only, not order health. "
            "Unset or empty (the default) monitors every CA, the old behavior. "
            "Item keys are unchanged: switching CA changes what the items "
            "measure, so expect a step change in their values."
        ),
    )] = None,
    # Expiry check
    expiry_days: Annotated[Optional[int], typer.Option(
        "--expiry-days", metavar="DAYS",
        help=(
            "Also push the DCV-expiry metrics, split by DCV method: token-method "
            "domains whose DCV expires within DAYS days and their minimum days "
            "left; the minimum days left of DNS-PERSIST reuse periods; and the "
            "LLD list of DNS-PERSIST apexes whose TXT records Zabbix checks, "
            "plus the record value the CA expects in them. "
            "Reads every scoped domain's DCV method and every verified domain's "
            "details (two API calls per domain, expensive) — schedule on its own "
            "daily run, not more often. Disabled by default."
        ),
    )] = None,
    # Order health check
    order_health: Annotated[bool, typer.Option(
        "--order-health", envvar="CERTINEXT_ORDER_HEALTH",
        help=(
            "Also push order-health metrics: orders awaiting issuance, "
            "certificates generated but never downloaded, orders that "
            "recently failed, days since the last certificate was issued, "
            "and certificates expiring within a lead time. Fetches the "
            "whole orders report (paginated, cheap) — schedule on its own "
            "hourly run, on a separate timer from --expiry-days: the "
            "order-stuck triggers' sustain window needs several samples "
            "inside it to work as intended. Disabled by default."
        ),
    )] = False,
    order_failing_lookback_days: Annotated[int, typer.Option(
        "--order-failing-lookback-days", metavar="DAYS",
        envvar="CERTINEXT_ORDER_FAILING_LOOKBACK_DAYS",
        help=(
            "Only count failed orders from the last DAYS days toward the "
            "failed-recent metric — older history is normal, not something "
            "to alert on forever."
        ),
    )] = 30,
    order_history_days: Annotated[int, typer.Option(
        "--order-history-days", metavar="DAYS",
        envvar="CERTINEXT_ORDER_HISTORY_DAYS",
        help=(
            "Bound the orders-report fetch to the last DAYS days, so it "
            "does not grow without limit as order history accumulates. "
            "Must stay comfortably longer than the longest certificate "
            "lifetime in the account: an issued cert older than this "
            "window drops out of the expiring-soon metric. Public TLS "
            "certificates cap at 398 days, so the 3-year default has ample "
            "margin; raise it only if the account holds longer-lived certs."
        ),
    )] = 1095,
    order_cert_expiry_days: Annotated[int, typer.Option(
        "--order-cert-expiry-days", metavar="DAYS",
        envvar="CERTINEXT_ORDER_CERT_EXPIRY_DAYS",
        help=(
            "An issued order's certificate counts toward the expiring-soon "
            "metric when its certificate_expiry_date falls within DAYS "
            "days (already-expired included) — same lead-time semantics "
            "as --expiry-days, but for the certificate itself rather than "
            "DCV verification."
        ),
    )] = 30,
    # Zabbix destination
    zabbix_server: Annotated[str, typer.Option(
        "--zabbix-server", metavar="HOST", envvar="ZABBIX_SERVER",
        help="Zabbix server (trapper) address",
    )] = ...,  # type: ignore[assignment]  # typer's required-Annotated-option sentinel
    zabbix_port: Annotated[int, typer.Option(
        "--zabbix-port", metavar="PORT", envvar="ZABBIX_PORT",
        help="Zabbix trapper port",
    )] = 10051,
    zabbix_host: Annotated[Optional[str], typer.Option(
        "--zabbix-host", metavar="NAME", envvar="ZABBIX_HOSTNAME",
        help=(
            "Host name as registered in Zabbix. Set this explicitly in production — "
            "the FQDN fallback depends on /etc/hosts and reverse DNS, and a silent "
            "mismatch makes the server reject every value"
        ),
    )] = None,
    zabbix_timeout: Annotated[int, typer.Option(
        "--zabbix-timeout", metavar="SECONDS", envvar="ZABBIX_TIMEOUT",
        help="Socket timeout for the trapper send",
    )] = 10,
) -> None:
    """Push CertiNext DCV health metrics to Zabbix as trapper item values.

    Always pushes the domain-list metrics (total domains, unverified count)
    and the environment name; with --expiry-days also reads each domain's
    DCV method and details and pushes the DCV-expiry metrics (token-method
    expiring count and minimum days left, DNS-PERSIST minimum days left,
    and the DNS-PERSIST apex discovery list); with
    --order-health also fetches the orders report and pushes the
    order-health metrics (awaiting-issuance count, generated-but-never-
    downloaded count, failed-recent count, days since last issued, and
    certificates expiring within --order-cert-expiry-days). The matching
    trapper items live on `CertiNext DCV by Zabbix trapper` in Zabbix.
    """
    correlation_id = str(uuid.uuid4())
    interrupted = False
    had_errors = False
    sent = False
    env = ENV_PROD
    metrics: dict[str, MetricValue] = {}
    resolved_host = zabbix_host or ""
    lock: Optional[FileLock] = None
    try:
        configure_logging(verbose, log_format, log_mode, debug_log_path)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        install_sigterm_handler()

        if not zabbix_host:
            # The fallback is best-effort: getfqdn() can return "localhost"
            # or a short name depending on /etc/hosts and reverse DNS, and
            # any mismatch with the Zabbix host entry rejects every value.
            resolved_host = socket.getfqdn()
            if resolved_host.startswith("localhost") or "." not in resolved_host:
                log.warning(
                    "FQDN fallback looks unusable as a Zabbix host name — "
                    "set --zabbix-host or ZABBIX_HOSTNAME explicitly",
                    resolved=resolved_host,
                )

        conn = resolve_connection(
            profile=profile, sandbox=sandbox, base_url=base_url, token_url=token_url,
        )
        # conn.sandbox also covers profiles configured with sandbox = true,
        # which a bare --sandbox check would miss. Derived, not
        # user-supplied: it is pushed as certinext.env, and the template's
        # misroute trigger compares it with the target host's
        # {$CERTINEXT.ENV}, so sandbox data sent to the prod host alerts.
        env = ENV_SANDBOX if conn.sandbox else ENV_PROD
        # An empty value (e.g. a templated unit file leaving
        # CERTINEXT_VALIDATED_BY="") means unset, not "a CA named ''" —
        # the latter would filter out every record and trip the
        # empty-list trigger. Bound on every line so each push says
        # which CA it measured ("ALL" = unfiltered).
        ca_filter = (validated_by or "").strip().upper() or None
        structlog.contextvars.bind_contextvars(env=env, validated_by=ca_filter or "ALL")
        if conn.sandbox:
            log.warning("SANDBOX MODE — metrics reflect the CertiNext sandbox, not production")
        if dry_run:
            log.info("DRY RUN — nothing will be sent to Zabbix")

        # order_health has its own lock tier, separate from expiry_days:
        # they now run on independent schedules (order-health hourly,
        # expiry-days daily — sysadmin/certinext-zabbix#8), so sharing a
        # tier would reintroduce the same silent-skip-on-collision bug the
        # 2026-07-15 incident fixed for plain-vs-expiry (see
        # TestLockScoping). A combined invocation (both flags at once)
        # takes the order_health tier, so it still can't double-push
        # order-health metrics alongside a standalone hourly run.
        job = "order_health" if order_health else ("expiry" if expiry_days is not None else "plain")
        lock = run_lock(f"certinext_zabbix_push_{env}_{job}")
        try:
            lock.acquire()
        except Timeout:
            log.info("Another instance is already running — exiting", lock=lock.lock_file)
            return

        structlog.contextvars.bind_contextvars(pid=os.getpid())
        log.info("Starting run")

        if verbose:
            log.info(
                "Zabbix destination",
                server=zabbix_server, port=zabbix_port, host=resolved_host,
            )
            if expiry_days is not None:
                log.info("Expiry check enabled", days=expiry_days)
            if order_health:
                log.info(
                    "Order-health check enabled",
                    failing_lookback_days=order_failing_lookback_days,
                    cert_expiry_days=order_cert_expiry_days,
                )

        sess = build_session(
            conn, account_number=account_number, client_secret=client_secret,
        )

        # Filter by CA *before* scope_domains: its covering-ancestor check
        # is org-scoped but not CA-aware, so on a mixed list an SSL.com
        # child would count as covered by an emSign apex record (ADR 0011).
        # No kwarg when unset keeps the pre-flag call shape exactly.
        domains = (
            sess.domain.get_list(validated_by=ca_filter)
            if ca_filter is not None
            else sess.domain.get_list()
        )
        scoped_domains = scope_domains(domains, domain_scope)
        log.info(
            "Domain scope applied",
            scope=domain_scope.value, before=len(domains), after=len(scoped_domains),
        )
        domains = scoped_domains
        metrics.update(collect_domain_metrics(domains))
        log.info(
            "Collected domain metrics",
            total=len(domains), unverified=metrics[KEY_UNVERIFIED],
        )

        if expiry_days is not None:
            log.info("Reading DCV methods", count=len(domains))
            split = fetch_dcv_methods(domains)
            log.info(
                "DCV methods read",
                persist=len(split.persist), token=len(split.token), failed=len(split.failed),
            )
            if split.failed:
                # A domain with an unknown method is missing from both
                # halves: the discovery list would drop its apex (Zabbix then
                # deletes that apex's items) and the token counts would come
                # out too low. Same skip-rather-than-undercount policy as a
                # refresh failure; nodata() flags a persistent failure.
                had_errors = True
                log.error(
                    "Skipping persist discovery and expiry metrics — "
                    "at least one DCV method read failed",
                    failed=len(split.failed),
                )
            else:
                # The method split is complete, so the discovery list is
                # too — send it even if a detail refresh fails below.
                metrics[KEY_PERSIST_DISCOVERY] = persist_discovery_value(split.persist)
                # Sent with the list, never without it: the template alerts
                # when this drops below its recent maximum, which catches a
                # silently partial domain list before Zabbix deletes the
                # missing apexes' checks (and a deliberate removal, once).
                metrics[KEY_PERSIST_APEXES] = len(persist_apex_names(split.persist))
                # The CA's record value, for the template's drift trigger
                # against {$CERTINEXT.PERSIST.EXPECTED} (#12). Fail closed:
                # a missing or disagreeing value sends nothing (nodata()
                # reports it) but never holds back discovery or expiry.
                try:
                    expected = persist_expected_value(split.persist, split.persist_values)
                except PersistExpectedError as exc:
                    had_errors = True
                    log.error(
                        "Skipping dns-persist expected value",
                        reason=str(exc), apexes_by_value=exc.apexes_by_value,
                    )
                else:
                    if expected is not None:
                        metrics[KEY_PERSIST_EXPECTED] = expected
                token_verified = verified_domains(split.token)
                persist_verified = verified_domains(split.persist)
                log.info(
                    "Fetching details to check expiry",
                    count=len(token_verified) + len(persist_verified),
                )
                if _refresh_any_failed([*token_verified, *persist_verified]):
                    had_errors = True
                    log.error("Skipping expiry metrics — at least one domain refresh failed")
                else:
                    metrics.update(collect_expiry_metrics(
                        token_verified, persist_verified, expiry_days,
                    ))
                    log.info(
                        "Collected expiry metrics", days=expiry_days,
                        expiring=metrics[KEY_EXPIRING],
                        token_domains=metrics[KEY_TOKEN_DOMAINS],
                        min_days_left=metrics.get(KEY_MIN_DAYS_LEFT),
                        persist_min_days_left=metrics.get(KEY_PERSIST_MIN_DAYS_LEFT),
                    )

        if order_health:
            log.info("Fetching orders report for order-health check")
            order_fetch_failed = False
            buckets = OrderBuckets(
                unissued=[], undownloaded=[], failed=[], issued=[], cancelled=[],
            )
            try:
                # One unfiltered fetch, bucketed client-side. The vendor's
                # server-side status filter can't be used: 5 of its 6
                # pending-* values return HTTP 422 (issue #3). Bounded only
                # by the multi-year history horizon — see the flag's help
                # for why that's safe for every metric derived here.
                history_since = (
                    datetime.now(timezone.utc) - timedelta(days=order_history_days)
                ).date()
                buckets = bucket_orders(fetch_orders(sess.orders, since=history_since))
            except (CertiNextAPIError, httpx.HTTPError) as exc:
                # Same skip-rather-than-undercount policy as the expiry
                # check above: a partial fetch would misreport pending/
                # failed counts, which masks the very condition monitored.
                log_caught_exception(
                    log, "Failed to fetch orders report", exc, level="warning",
                )
                order_fetch_failed = True
            except Exception as exc:
                log_caught_exception(
                    log, "Failed to fetch orders report — unexpected error", exc,
                )
                order_fetch_failed = True
            if order_fetch_failed:
                had_errors = True
                log.error("Skipping order-health metrics — orders report fetch failed")
            else:
                metrics.update(collect_order_metrics(
                    buckets,
                    failing_lookback_days=order_failing_lookback_days,
                    cert_expiry_days=order_cert_expiry_days,
                ))
                log.info(
                    "Collected order-health metrics",
                    unissued=metrics[KEY_ORDERS_UNISSUED],
                    undownloaded=metrics[KEY_ORDERS_UNDOWNLOADED],
                    failed_recent=metrics[KEY_ORDERS_FAILED_RECENT],
                    expiring=metrics[KEY_ORDERS_EXPIRING],
                    days_since_issued=metrics.get(KEY_ORDERS_DAYS_SINCE_ISSUED),
                )

        # Every run, so the misroute trigger always has a fresh value to
        # compare. Added last: a run that failed before collecting anything
        # still exits with no stdout JSON (metrics stays empty).
        metrics[KEY_ENV] = env

        if dry_run:
            log.info("DRY RUN — metrics not sent", count=len(metrics))
        else:
            try:
                response = push_metrics(
                    metrics, zabbix_host=resolved_host,
                    server=zabbix_server, port=zabbix_port, timeout=zabbix_timeout,
                )
            except (ProcessingError, OSError) as exc:
                # push_metrics already retried; this is the final-attempt
                # failure. Expected during a Zabbix outage/misconfiguration.
                had_errors = True
                log_caught_exception(
                    log,
                    "Could not reach Zabbix trapper after retries — check server "
                    "reachability, firewall, and --zabbix-server/--zabbix-port",
                    exc, server=zabbix_server, port=zabbix_port,
                )
            else:
                if response.failed:
                    had_errors = True
                    log.error(
                        "Zabbix rejected item values — check host name, template link, "
                        "and the item's allowed-hosts setting",
                        processed=response.processed, failed=response.failed,
                        total=response.total,
                    )
                else:
                    sent = True
                    log.info("Pushed metrics to Zabbix", processed=response.processed)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        interrupted = True
    except (RuntimeError, CertiNextAPIError) as exc:
        # include_traceback: this handler wraps the whole run, so it fires at
        # most once — unlike the per-domain calls above, which must stay
        # concise or one systemic failure dumps a stack per domain (ADR 0014).
        had_errors = True
        log_caught_exception(log, "Unexpected error", exc, include_traceback=True)
    except Exception as exc:
        # Catches anything not already handled above (e.g. domain listing
        # itself failing).
        had_errors = True
        log_caught_exception(log, "Unexpected error", exc, include_traceback=True)
    finally:
        if lock is not None:
            lock.release(force=True)
        if interrupted:
            log.warning("Interrupted")
        elif had_errors:
            log.warning("Ending run with errors")
        else:
            log.info("Ending run")

    if not interrupted and metrics:
        typer.echo(json.dumps(
            {"env": env, "zabbix_host": resolved_host, "sent": sent, "metrics": metrics},
            sort_keys=True,
        ))
    if interrupted:
        sys.exit(130)
    if had_errors:
        sys.exit(1)


def main() -> None:
    """Run the certinext-zabbix-push CLI via the shared interrupt-safe wrapper."""
    run_app(app)


if __name__ == "__main__":
    main()
