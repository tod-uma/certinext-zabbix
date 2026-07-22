# AGENTS.md

Operational facts for coding agents working in this repository (Claude
Code, Codex, Cursor, etc.). For a factual map of the project's docs, see
[llms.txt](llms.txt). For usage and deployment, see [README.md](README.md)
and [docs/deployment.md](docs/deployment.md).

## Setup

```bash
uv venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
uv sync --extra dev
```

All runtime dependencies (`certinext`, `zabbix-utils`, `filelock`,
`structlog`, `typer`) are on public PyPI — no private index, no
credentials needed to sync.

## Test, lint, type-check

```bash
uv run pytest -v
uv run ruff check .
uv run mypy certinext_zabbix    # strict
uv run pyright
```

All tests are offline (mocked CertiNext sessions, typer `CliRunner`) — no
credentials needed, safe anywhere.

**CI gotcha:** `.gitlab-ci.yml` sets `PY_COLORS='1'`, which typer bakes
into `rich_utils.FORCE_TERMINAL` at import time, forcing ANSI codes into
captured `--help` output. `tests/conftest.py` neutralizes this with
`_TYPER_FORCE_DISABLE_TERMINAL=1` in `pytest_configure` (must run before
typer is imported — a fixture is too late). Keep that hook if you touch
conftest.

## Project layout

- `certinext_zabbix/zabbix_push.py` — metric computation and the Zabbix
  trapper send. Its `KEY_*` item keys must stay in sync with
  `templates/template_certinext/7.0/template_certinext.yaml` (the Zabbix
  template import file — server-side source of truth for items,
  triggers, and macros).
- `certinext_zabbix/zabbix_push_cli.py` — the `certinext-zabbix-push`
  typer entry point, thin over `zabbix_push.py`. `--zabbix-server` /
  `ZABBIX_SERVER` has **no default** — an unset value fails fast rather
  than silently targeting the wrong server.
- `certinext_zabbix/_cli_shared.py` — logging setup, the per-environment/
  per-job file lock, and signal/exit scaffolding shared by the CLI.
- `templates/template_certinext/7.0/` — the Zabbix template (community-
  templates layout: `template_<name>/<X.X>/template_<name>.yaml`) plus
  its own per-template README (macros, metrics, triggers).
- Connection flags come from the **public `certinext.cli_options`
  aliases** — do not re-declare `--profile`/`--sandbox`/etc. locally; add
  new shared options upstream in `certinext` if both ecosystems need
  them.
- Logging is `certinext.cli_support.setup_logging()` via
  `_cli_shared.configure_logging()` — do not configure structlog/stdlib
  logging anywhere else.

## Copy-not-canonical

This repo is a sanitized copy of code that also lives in the UMS-internal
`ums-certinext-scripts` repo, which remains the canonical source UMS
deploys from until a future, effort-gated migration. Two sources of
truth exist for the push logic, the template, and the `KEY_*` constants
in the interim — see [CLAUDE.md](CLAUDE.md) for the full explanation.
Don't assume a change here is mirrored there, or vice versa.

## GitLab project path

`sysadmin/certinext-zabbix` on gitlab.its.maine.edu — use for GitLab CI
references, clone URLs, and API calls.

## Remotes and mirror

The git remote is named `gitlab` (no `origin`). GitLab is canonical;
`github.com/tod-uma/certinext-zabbix` is a push mirror — push to
`gitlab`, never directly to GitHub.

## Releasing

PyPI-ready but not published yet — see [README.md](README.md) and
[CLAUDE.md](CLAUDE.md) for what publishing would take.

## Deployment

`certinext-zabbix-push` is designed to run unattended from systemd timers
(recommended), cron, or Windows Task Scheduler. Everything an install/
configuration playbook needs — env vars, unit files, network
requirements, validation commands — is in
[docs/deployment.md](docs/deployment.md). Keep that document (and the
matching files under `examples/systemd/`, `examples/cron/`,
`examples/windows/`) in sync when changing flags, environment variables,
exit codes, locking, or logging behavior in `zabbix_push_cli.py`.
`examples/windows/` is explicitly marked untested — it has not been
verified against a real Windows host by someone who knows Windows
Task Scheduler; don't remove that caveat without an actual review.
