# certinext-zabbix

## GitLab project path

`sysadmin/certinext-zabbix` on gitlab.its.maine.edu — use for GitLab CI
references, clone URLs, and API calls. This is the canonical repo; CI
runs here.

## Remotes and the GitHub mirror

The git remote is named `gitlab` (no `origin`, per this org's convention).
gitlab.its.maine.edu is canonical; `github.com/tod-uma/certinext-zabbix`
is a GitLab **push mirror** — the public face for InCommon/higher-ed
users, configured in GitLab under *Settings → Repository → Mirroring
repositories*. Push to `gitlab`; never push directly to GitHub.

## Copy-not-canonical (until Phase 6)

This repo is a **sanitized copy** of the pusher + template that still
live in `ums-certinext-scripts` (`sysadmin/ums-certinext-scripts`, UMS-
internal, not publicly accessible). `ums-certinext-scripts` runs
production unchanged and remains the source UMS actually deploys from.
Until a future, effort-gated Phase 6 makes this repo canonical (see
`docs/plans/certinext-zabbix-extraction.md` in the `sysadmin-ansible`
superproject where this repo originated), **two sources of truth exist**
for the push logic, the template, and the `KEY_*` item-key constants —
changes made in one place do not automatically propagate to the other.
Do not assume a fix here is also live in `ums-certinext-scripts`, or vice
versa.

## `KEY_*` ↔ template sync

The `KEY_*` constants in `certinext_zabbix/zabbix_push.py` (e.g.
`KEY_TOTAL`, `KEY_UNVERIFIED`) must stay in sync with the trapper items
defined in `templates/template_certinext/7.0/template_certinext.yaml` —
that YAML is the Zabbix-side source of truth for items, triggers, and
macros. A key added/renamed in one place without the matching change in
the other fails silently at push time (`"Zabbix rejected item values"`),
not as a type/schema error.

## Testing

All tests are offline (mocked CertiNext sessions, typer `CliRunner`) — no
credentials needed. `tests/conftest.py` must keep setting
`_TYPER_FORCE_DISABLE_TERMINAL=1` in `pytest_configure`: CI setting
`PY_COLORS='1'` otherwise forces ANSI into captured `--help` output and
breaks the CLI tests in `tests/test_zabbix_push_cli_mocked.py`.

## Shared CLI surface

Connection flags come from the public `certinext.cli_options` aliases;
logging from `certinext.cli_support.setup_logging()` (wrapped in
`_cli_shared.py`) — don't re-declare flags or configure logging locally.
New shared options belong upstream in `certinext`, not duplicated here.

## Deployment doc

`docs/deployment.md` is the contract for install/configure automation
(systemd/cron/Windows Task Scheduler, env vars, network, Ansible
checklist). Keep it (and `examples/systemd/`, `examples/cron/`,
`examples/windows/`) in sync when changing `zabbix_push_cli.py` flags,
env vars, exit codes, locking, or logging. `examples/windows/` is
untested by a real Windows admin — don't remove that caveat without
someone actually verifying it.

## PyPI

PyPI-ready but not published (see the root README). Flipping to PyPI
means: register the `certinext-zabbix` project, add a PyPI API token as a
CI variable, and add a tag-triggered `twine upload` job — `.gitlab-ci.yml`
has a commented stub for this.

## License

MIT (chosen for Zabbix community-templates submission-readiness — that
repo accepts MIT only). No per-file license headers; the root `LICENSE`
governs.

## AGENTS.md

`AGENTS.md` at the repo root carries these same operational facts for
non-Claude tooling — keep the two in sync when either changes.
