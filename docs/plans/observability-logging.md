---
status: in-progress
depends-on: [] # certinext main library phases (1-3), already shipped/released as certinext v1.2.0a3 on PyPI
implements-adr: []
---

# Phase 4: wire `--log-mode`/`--debug-log-path` into `certinext-zabbix-push`

Sub-plan for `certinext-zabbix`, one leaf of the cross-repo
[observability & logging overhaul](../../../python-libs/certinext/docs/plans/observability-logging/README.md)
(master plan lives in `certinext` since the shared library and both wishlist
items live there). This document covers only the slice that lands in this
repo — Phase 4 of that master's five-phase table.

## Goal and context

On 2026-08-03 the `certinext-zabbix-push` systemd units failed with
`event="maximum recursion depth exceeded while decoding a JSON object..."`
and no traceback — a `RecursionError` (a `RuntimeError` subclass) caught by
`zabbix_push_cli.py`'s broad `except (RuntimeError, CertiNextAPIError)`
branch, which logged only `str(exc)`. That immediate symptom was already
fixed (MR !10, routed through `log_caught_exception`, merged to `main`), but
it exposed the real gap: `log_caught_exception`'s paired DEBUG-level
traceback is dropped by structlog's filtering bound logger unless the run is
at `-vvv`, so an **unattended systemd-timer run can never produce a
traceback**. The prod pusher has in fact been failing silently since
2026-07-28 (Zabbix `nodata()` triggers on host 13760 confirm zero pushes for
several days) — this is the change that finally makes that failure
diagnosable from the journal alone, without needing to re-run interactively
at higher verbosity.

The upstream `certinext` library already implemented and shipped the two
underlying mechanisms this repo needs (Phases 1-3 of the master plan,
released as `certinext==1.2.0a3` on PyPI, 2026-08-04):

- **`--log-mode auto|syslog|verbose`** (IDEA-009) — drops the redundant
  `timestamp`/`pid` fields from non-interactive output when systemd is
  detected (`INVOCATION_ID`/`JOURNAL_STREAM`).
- **`--debug-log-path PATH`** (new, the operationally urgent piece) — an
  always-on JSON-lines DEBUG-level log file, independent of `-v`, so a
  `log_caught_exception` traceback is captured on every run regardless of
  visible verbosity.

Both are exposed as `Annotated` typer option aliases
(`certinext.cli_options.LogModeOption`, `DebugLogPathOption`) and as
parameters on `certinext.cli_support.setup_logging()`. This repo's job is
Phase 4 only: **wire those two options into the single
`certinext-zabbix-push` command** the same way `--zabbix-server` etc. are
already wired (no library changes needed here — they come transitively from
the `certinext` dependency bump).

## Decisions

Reusing decisions already settled at the master-plan level; only the
repo-specific ones are new here.

| # | Decision | Rationale |
|---|----------|-----------|
| — | Debug-log env var: `CERTINEXT_ZABBIX_DEBUG_LOG` | Per the master plan's D7 per-repo path table — one var per repo, no shared library default. |
| — | Deployed path: `/var/log/certinext-zabbix/debug.log` | Same D7 table. |
| — | Pin `certinext>=1.2.0a3,<2` (was `>=1.1.0rc1,<2`) | `1.2.0a3` is the first version carrying the `LogModeOption`/`setup_logging(log_mode=..., debug_log_path=...)` API this phase depends on. |
| — | Define a **local** `DebugLogPathOption` in `_cli_shared.py`, not import `certinext.cli_options.DebugLogPathOption` | The upstream alias bakes in `envvar="CERTINEXT_DEBUG_LOG"` — the certinext CLI's *own* env var (D7 row 4 of the master table). Reusing it verbatim would make this repo's debug log toggle on `CERTINEXT_DEBUG_LOG` instead of its own `CERTINEXT_ZABBIX_DEBUG_LOG`, silently violating the per-repo path table. `LogModeOption` has no baked-in envvar, so it *is* safe to import directly. |

<details>
<summary>Why not hand-roll a debug-log option instead of pinning to a pre-release?</summary>

The master plan's [sequencing note](../../../python-libs/certinext/docs/plans/observability-logging/README.md#sequencing-note--fallback)
reserved a hand-wired fallback for this exact repo in case Phase 1
(`certinext`'s shared-option redesign) slipped past the debug-log need. It
didn't slip — Phases 1-3 shipped and released the same day — so the
fallback isn't needed. Pinning to the pre-release is intentional: certinext
hasn't cut a stable `1.2.0` yet, and waiting for one would block the
already-overdue observability fix for no benefit (the pre-release is
already used elsewhere in this dependency chain — see
`ums.pki`/`certinext_scripts` role notes).
</details>

## Implementation steps

1. **Dependency bump.** `pyproject.toml`: `certinext>=1.1.0rc1,<2` →
   `certinext>=1.2.0a3,<2`. Regenerate `uv.lock` (gitignored in this repo,
   so only `pyproject.toml` is committed — see the uv-lock-version-bump
   note in project memory) so local tests actually exercise the new
   `certinext` API.
2. **Wire the two options** into `certinext_zabbix/zabbix_push_cli.py`'s
   `run()` command, alongside the existing `verbose`/`log_format` params:
   - Import `LogModeOption`, `DebugLogPathOption` from
     `certinext.cli_options` (same import line as `LogFormatOption` etc.).
   - Add `log_mode: LogModeOption = LogMode.AUTO` and
     `debug_log_path: DebugLogPathOption = None` parameters.
   - Forward both into `configure_logging()`.
3. **Extend `_cli_shared.configure_logging()`** to accept `log_mode` and
   `debug_log_path` and pass them through to
   `certinext.cli_support.setup_logging()`. Update its docstring (Args
   section) to match.
4. **Test isolation from the first commit.** Per the lesson already
   recorded from `certinext`'s MR !106 (GitHub Actions' `ubuntu-latest`
   runner leaks `INVOCATION_ID` because the Actions Runner itself is a
   systemd service): add a session-wide `autouse` fixture to
   `tests/conftest.py` clearing `INVOCATION_ID`/`JOURNAL_STREAM` before any
   test that exercises `configure_logging`/`setup_logging`, mirroring
   `certinext`'s `tests/conftest.py::_clear_systemd_env_vars`. Do this now,
   not after a second CI provider catches it.
5. **Update/add CLi tests** in `tests/test_zabbix_push_cli_mocked.py`:
   - `test_verbose_count_reaches_configure_logging` currently asserts
     `mocks.logging.assert_called_once_with(3, LogFormat.LOGFMT)` — extend
     the call signature it's asserting against once `configure_logging`
     grows the two new params (positional/keyword form TBD by the actual
     signature chosen in step 3).
   - Add a case confirming `--log-mode`/`--debug-log-path` flags (and
     `CERTINEXT_ZABBIX_DEBUG_LOG` env var) reach `configure_logging`
     unchanged, following the existing `TestCliSupportContract` pattern.
6. **Systemd units** (`examples/systemd/certinext-zabbix-push.service` and
   `-expiry.service`): add `/var/log/certinext-zabbix` to `ReadWritePaths`
   (currently `/tmp` only) so the debug-log file can be written under
   `ProtectSystem=strict`. Do **not** set `PrivateTmp` (already called out
   in both unit files re: the lock file — unrelated to this change, just
   don't touch it).
7. **Env-file examples**: add a commented
   `CERTINEXT_ZABBIX_DEBUG_LOG=/var/log/certinext-zabbix/debug.log` line
   (parallel to the existing commented `CERTINEXT_DOMAIN_SCOPE`) to
   `examples/systemd/certinext-zabbix-push.env.example` and
   `examples/windows/certinext-zabbix.env.example`, and a `--log-mode`
   mention if useful there.
8. **`docs/deployment.md`**: add both new env vars to the "Environment
   variable reference" table and both new flags to the "CLI flag
   reference" table; a short callout in the logging bullet list (top of
   the doc) that an unattended run's traceback is now recoverable via
   `--debug-log-path`/`CERTINEXT_ZABBIX_DEBUG_LOG` without re-running at
   `-vvv`.
9. **Verify**: `pytest`, `ruff check`/`ruff format --check`, `mypy`,
   `pyright` all green; regenerate any `--help` golden-output tests if this
   repo has them (check `tests/` for a goldens pattern before assuming
   none exist).

## Out of scope

- The suspected upstream `zabbix_utils` unbounded-recursion bug
  (`Sender.__send_to_cluster`) that caused the original `RecursionError` —
  tracked separately, not fixed by this phase. This phase only ensures its
  traceback is never lost again.
- `nm` and `ums-certinext-scripts` Phase 4 wiring — each gets its own
  sub-plan, written at the start of that repo's own session.
- Phase 5 (Ansible role: log dir + logrotate for
  `/var/log/certinext-zabbix/`) — depends on this phase merging first.

## References

- [typer — Annotated options](https://typer.tiangolo.com/tutorial/parameter-types/)
- [structlog — processors & filtering bound logger](https://www.structlog.org/en/stable/api.html)
- [systemd.exec — `ReadWritePaths=`](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#ReadWritePaths=)
- Master plan: [`python-libs/certinext/docs/plans/observability-logging/README.md`](../../../python-libs/certinext/docs/plans/observability-logging/README.md)
- ADRs implemented upstream: [ADR 0009](../../../python-libs/certinext/docs/adr/0009-root-callback-for-shared-cli-options.md) (shared options), [ADR 0010](../../../python-libs/certinext/docs/adr/0010-log-mode-tri-state-for-syslog-aware-output.md) (`--log-mode`), [ADR 0011](../../../python-libs/certinext/docs/adr/0011-always-on-json-debug-log-sidecar.md) (`--debug-log-path`)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-08-04. May
> contain inaccuracies or hallucinated details; verify specifics against
> current sources before relying on them.
