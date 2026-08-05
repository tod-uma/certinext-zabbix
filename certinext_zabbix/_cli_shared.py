"""Shared CLI glue for the certinext-zabbix typer entry point.

The CertiNext connection options come from the public
:mod:`certinext.cli_options` aliases; this module adds the logging,
locking, and signal/exit scaffolding the entry-point module needs.
"""

import signal
import sys
import tempfile
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated

import typer
from certinext.cli_support import (
    LogFormat,
    LogMode,
    setup_logging,
)
from certinext.cli_support import (
    # Re-exported: this module is the entry point's single source of CLI glue,
    # and the helper lived here as a private copy until certinext ADR 0013
    # promoted it into the library. Importers keep working unchanged.
    log_caught_exception as log_caught_exception,
)
from filelock import FileLock


def _version_callback(show_version: bool) -> None:
    """Print the installed certinext-zabbix version and exit.

    Args:
        show_version: The ``--version`` flag's value; a no-op when False.

    Raises:
        typer.Exit: Always, when show_version is True, so no subcommand runs.
    """
    if show_version:
        typer.echo(_pkg_version("certinext-zabbix"))
        raise typer.Exit()


VersionOption = Annotated[bool, typer.Option(
    "--version", callback=_version_callback, is_eager=True,
    help="Show the installed certinext-zabbix version and exit.",
)]

# Own alias, not certinext.cli_options.DebugLogPathOption — that one bakes in
# envvar="CERTINEXT_DEBUG_LOG" for the certinext CLI itself; this repo's own
# env var per the observability-logging plan's per-repo path table is
# CERTINEXT_ZABBIX_DEBUG_LOG.
DebugLogPathOption = Annotated[Path | None, typer.Option(
    "--debug-log-path", metavar="PATH", envvar="CERTINEXT_ZABBIX_DEBUG_LOG",
    help=(
        "Append a JSON-lines DEBUG-level log (with full tracebacks) to this path, "
        "independent of --verbose (env: CERTINEXT_ZABBIX_DEBUG_LOG; default: off). "
        "Rotation is the deployer's responsibility (e.g. logrotate). This is what "
        "makes an unattended systemd-timer run's traceback recoverable — "
        "log_caught_exception's paired DEBUG record is otherwise dropped below -vvv."
    ),
)]


def configure_logging(
    verbose: int,
    log_format: LogFormat = LogFormat.LOGFMT,
    log_mode: LogMode = LogMode.AUTO,
    debug_log_path: Path | None = None,
) -> None:
    """Configure structlog/stdlib logging with this package's run context.

    Delegates to :func:`certinext.cli_support.setup_logging`: correlation_id
    and pid keep a stable field order in non-interactive output and are
    hidden from interactive output at verbosity 0; filelock joins the
    third-party loggers quieted below ``-vvvv``.

    Args:
        verbose: Verbosity count from -v flags (0=INFO, 3+=DEBUG,
            4+=third-party DEBUG).
        log_format: Non-interactive (cron/redirected) log line format — see
            :class:`certinext.cli_support.LogFormat`. Ignored on a TTY.
        log_mode: Whether non-interactive output drops the redundant
            ``timestamp``/``pid`` fields — see
            :class:`certinext.cli_support.LogMode`. Ignored on a TTY.
        debug_log_path: When set, append a JSON-lines DEBUG-level log
            (with full tracebacks) to this path, independent of
            ``verbose`` — see :func:`certinext.cli_support.setup_logging`.
            No default; unset means off.
    """
    setup_logging(
        verbose,
        log_format=log_format,
        log_mode=log_mode,
        debug_log_path=debug_log_path,
        extra_priority_keys=("correlation_id", "pid"),
        console_quiet_keys=("correlation_id", "pid"),
        quiet_loggers=("filelock",),
    )


def run_lock(name: str) -> FileLock:
    """Return the zero-timeout single-instance lock file for a run.

    The lock lives in the system temp directory as ``<name>.lock``. Zero
    timeout means a second invocation gives up immediately instead of
    queueing behind the running one — schedulers retry soon enough.

    The shared temp directory is load-bearing: any systemd unit deploying
    this script must not set ``PrivateTmp=yes``, or every invocation gets
    its own empty /tmp and the lock stops excluding anything.

    Args:
        name: Lock file basename without the ``.lock`` suffix (e.g.
            ``certinext_zabbix_push_prod_expiry``).

    Returns:
        A zero-timeout :class:`filelock.FileLock`, not yet acquired.
    """
    return FileLock(str(Path(tempfile.gettempdir()) / f"{name}.lock"), timeout=0)


def _sigterm_handler(_signum: int, _frame: object) -> None:
    """Raise KeyboardInterrupt on SIGTERM so a run logs cleanly and exits 130.

    Cron schedulers and process supervisors send SIGTERM before SIGKILL.
    Without a handler the process dies silently with no log entry and no
    correlation_id.
    """
    raise KeyboardInterrupt


def install_sigterm_handler() -> None:
    """Install the SIGTERM→KeyboardInterrupt handler for the current process.

    Call once early in a command body, before any long-running work, so a
    supervisor-initiated stop takes the same clean-shutdown path as Ctrl-C.
    """
    signal.signal(signal.SIGTERM, _sigterm_handler)


def run_app(app: typer.Typer) -> None:
    """Run a typer app, exiting 130 on interrupts outside the command body.

    The command body handles KeyboardInterrupt itself (to release its file
    lock and log the interruption); this wrapper covers interrupts during
    argument parsing and credential prompts.

    Args:
        app: The typer application to invoke.

    Raises:
        SystemExit: With code 130 when interrupted.
    """
    try:
        app()
    except KeyboardInterrupt:
        sys.stderr.write("\nAborted.\n")
        raise SystemExit(130) from None
