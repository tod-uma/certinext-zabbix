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
from typing import Annotated, Any, Literal

import typer
from certinext.cli_support import LogFormat, setup_logging
from filelock import FileLock

_TRACEBACK_HINT = "re-run with -vvv for the full traceback"


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


def configure_logging(verbose: int, log_format: LogFormat = LogFormat.LOGFMT) -> None:
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
    """
    setup_logging(
        verbose,
        log_format=log_format,
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


def log_caught_exception(
    log: Any,
    event: str,
    exc: BaseException,
    *,
    level: Literal["warning", "error"] = "error",
    **context: Any,
) -> None:
    """Log a caught exception as one concise, syslog-safe line.

    Cron-fed logs must never carry a raw traceback — one bad run can dump
    one per domain/attempt, turning a syslog alert into a multi-KB stack
    dump. This emits *event* at *level* with the exception's type and
    message plus a hint to re-run at higher verbosity, then pairs it with a
    DEBUG-level record carrying the real traceback. Below ``-vvv``,
    structlog's filtering bound logger drops that debug call before it does
    any work, so the traceback never reaches a normal (INFO-level) run —
    interactively adding ``-vvv`` is what actually surfaces it.

    Args:
        log: The bound structlog logger to emit through.
        event: The log event name/message (used for both records).
        exc: The caught exception.
        level: Log level for the concise line — ``"warning"`` for an
            expected, lower-severity failure mode, ``"error"`` (default)
            otherwise.
        **context: Extra structured fields (e.g. ``domain=...``) attached to
            both the concise line and the paired debug traceback.
    """
    getattr(log, level)(
        event, error=str(exc), error_type=type(exc).__name__, hint=_TRACEBACK_HINT, **context,
    )
    log.debug(event, exc_info=True, **context)


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
