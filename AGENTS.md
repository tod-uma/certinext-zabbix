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

## GitLab project path

`sysadmin/certinext-zabbix` on `gitlab.its.maine.edu` — UMS's internal
GitLab instance, not reachable from outside UMS's network/VPN. Use it
for GitLab CI references, clone URLs, and API calls. External
contributors should work against the public GitHub mirror instead (see
below).

## Remotes and mirror

The git remote is named `gitlab` (no `origin`). GitLab is canonical;
`github.com/tod-uma/certinext-zabbix` is a push mirror — push to
`gitlab`, never directly to GitHub. The mirror also runs its own CI
(`.github/workflows/ci.yml`, copied from `certinext`'s pattern) so
external contributors' PRs get checks GitLab's pipelines never see them.

## ADRs and wishlist

Architecture decisions: `docs/adr/` (MADR format, sequential
`NNNN-slug.md`). Deferred ideas: `docs/wishlist/` (`IDEA-NNN-slug.md`,
indexed in `docs/wishlist/README.md`). ADRs 0001–0006 already cover this
repo's foundational decisions (standalone repo, copy-now/canonical-later,
PyPI-not-published-yet, GitHub push mirror, MIT license,
community-templates-ready layout) — check there before re-litigating any
of them.

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
