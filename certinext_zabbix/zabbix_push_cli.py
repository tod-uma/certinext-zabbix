"""Typer entry point for the ``certinext-zabbix-push`` command.

Command-line wiring only — metric computation and the trapper send live in
:mod:`certinext_zabbix.zabbix_push`. Each run lists the CertiNext
domains, computes DCV health metrics, and pushes them to Zabbix as trapper
item values; ``CertiNext DCV by Zabbix trapper`` on the Zabbix side owns
the matching items and triggers (source:
``templates/template_certinext/7.0/template_certinext.yaml``).

Designed for two schedules on the same host:

- a frequent run (no flags) pushing the cheap domain-list metrics;
- a daily run with ``--expiry-days N`` that additionally fetches per-domain
  details and pushes the DCV-expiry metrics.

Item keys are parameterized by environment (``[prod]`` / ``[sandbox]``),
derived from the resolved CertiNext connection — a ``--sandbox`` (or
sandbox-profile) run pushes into the sandbox items of the same Zabbix
host, never the prod ones. Locks are per environment *and* per job (plain
vs. ``--expiry-days``), so optional sandbox schedules never make prod runs
skip a cycle, and the daily expiry run colliding with a 15-minute plain-push
slot can't make either one skip the other.

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
from typing import Annotated, Optional

import httpx
import structlog
import typer
from certinext.cli_options import (
    AccountNumberOption,
    BaseUrlOption,
    ClientSecretOption,
    LogFormatOption,
    ProfileOption,
    SandboxOption,
    TokenUrlOption,
    VerboseOption,
)
from certinext.cli_support import LogFormat, build_session, resolve_connection
from certinext.exceptions import CertiNextAPIError
from filelock import FileLock, Timeout
from zabbix_utils.exceptions import ProcessingError

from ._cli_shared import (
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
    KEY_EXPIRING,
    KEY_MIN_DAYS_LEFT,
    KEY_UNVERIFIED,
    collect_domain_metrics,
    collect_expiry_metrics,
    item_key,
    push_metrics,
    verified_domains,
)

log = structlog.get_logger()

app = typer.Typer(add_completion=False)


@app.command()
def run(
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Compute and print the metrics without sending anything to Zabbix",
    )] = False,
    version_: VersionOption = False,
    verbose: VerboseOption = 0,
    log_format: LogFormatOption = LogFormat.LOGFMT,
    # CertiNext connection
    profile: ProfileOption = None,
    sandbox: SandboxOption = False,
    base_url: BaseUrlOption = None,
    token_url: TokenUrlOption = None,
    account_number: AccountNumberOption = None,
    client_secret: ClientSecretOption = None,
    # Expiry check
    expiry_days: Annotated[Optional[int], typer.Option(
        "--expiry-days", metavar="DAYS",
        help=(
            "Also push the DCV-expiry metrics: verified domains whose DCV expires "
            "within DAYS days, and the minimum days left. Fetches domain details "
            "(one API call per verified domain) — schedule on a daily run, not "
            "every 15 minutes. Disabled by default."
        ),
    )] = None,
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

    Always pushes the domain-list metrics (total domains, unverified count);
    with --expiry-days also fetches per-domain details and pushes the
    DCV-expiry metrics (expiring count, minimum days left). The matching
    trapper items live on `CertiNext DCV by Zabbix trapper` in Zabbix.
    """
    correlation_id = str(uuid.uuid4())
    interrupted = False
    had_errors = False
    sent = False
    env = ENV_PROD
    metrics: dict[str, int | float] = {}
    resolved_host = zabbix_host or ""
    lock: Optional[FileLock] = None
    try:
        configure_logging(verbose, log_format)

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
        # which a bare --sandbox check would miss. The environment decides
        # which [env]-parameterized items receive the values — derived, not
        # user-supplied, so sandbox data can never land in the prod items.
        env = ENV_SANDBOX if conn.sandbox else ENV_PROD
        structlog.contextvars.bind_contextvars(env=env)
        if conn.sandbox:
            log.warning("SANDBOX MODE — metrics reflect the CertiNext sandbox, not production")
        if dry_run:
            log.info("DRY RUN — nothing will be sent to Zabbix")

        job = "expiry" if expiry_days is not None else "plain"
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

        sess = build_session(
            conn, account_number=account_number, client_secret=client_secret,
        )

        domains = sess.domain.get_list()
        metrics.update(collect_domain_metrics(domains, env))
        log.info(
            "Collected domain metrics",
            total=len(domains), unverified=metrics[item_key(KEY_UNVERIFIED, env)],
        )

        if expiry_days is not None:
            verified = verified_domains(domains)
            log.info("Fetching details to check expiry", count=len(verified))
            refresh_failed = False
            for d in verified:
                try:
                    d.refresh()
                except (CertiNextAPIError, httpx.HTTPError) as exc:
                    # A partial refresh would undercount the expiring domains —
                    # a too-low value masks the very condition being monitored.
                    # Push nothing for the expiry items instead; the nodata()
                    # trigger flags a persistent failure. Expected, well-typed
                    # failure mode (timeout, rate limit, transient API error).
                    log_caught_exception(
                        log, "Failed to refresh domain", exc, level="warning", domain=d.name,
                    )
                    refresh_failed = True
                except Exception as exc:
                    # Not a documented failure mode of Domain.refresh(), but
                    # still one concise line per domain, same as above.
                    log_caught_exception(
                        log, "Failed to refresh domain — unexpected error", exc, domain=d.name,
                    )
                    refresh_failed = True
            if refresh_failed:
                had_errors = True
                log.error("Skipping expiry metrics — at least one domain refresh failed")
            else:
                metrics.update(collect_expiry_metrics(verified, expiry_days, env))
                log.info(
                    "Collected expiry metrics", days=expiry_days,
                    expiring=metrics[item_key(KEY_EXPIRING, env)],
                    min_days_left=metrics.get(item_key(KEY_MIN_DAYS_LEFT, env)),
                )

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
        had_errors = True
        log.error(str(exc))
    except Exception as exc:
        # Catches anything not already handled above (e.g. domain listing
        # itself failing).
        had_errors = True
        log_caught_exception(log, "Unexpected error", exc)
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
