# certinext-zabbix

## GitLab project path

`sysadmin/certinext-zabbix` on `gitlab.its.maine.edu` — the University of
Maine System's internal GitLab instance, not reachable from outside
UMS's network/VPN. Use it for GitLab CI references, clone URLs, and API
calls. This is the canonical repo; merges, issues, and the source of
truth for content all happen here. External contributors work against
the public GitHub mirror instead (see below), which also runs its own
CI (see PyPI section) since GitLab's pipelines are invisible to them.

## Remotes and the GitHub mirror

The git remote is named `gitlab` (no `origin`, per this org's convention).
`gitlab.its.maine.edu` (UMS-internal) is canonical;
`github.com/tod-uma/certinext-zabbix` is a GitLab **push mirror** — the
public, no-login-required face for InCommon/higher-ed users, configured
in GitLab under *Settings → Repository → Mirroring repositories*. Push
to `gitlab`; never push directly to GitHub.

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

PyPI-ready but not published (see the root README). The GitHub mirror
carries a `.github/workflows/ci.yml` with `publish-pypi`/`release` jobs,
copied from `certinext`'s pattern: gated to fire only on `v*` tag pushes,
publishing via **OIDC trusted publishing** (`pypa/gh-action-pypi-publish`,
`permissions: id-token: write`) — no PyPI token stored on GitHub *or*
GitLab. Flipping PyPI on is pure pypi.org-side config: register
`certinext-zabbix` as a project and add a trusted publisher pointing at
this repo's `ci.yml` workflow + the `pypi` environment. No workflow or
`.gitlab-ci.yml` changes needed at that point — `.gitlab-ci.yml` has a
comment explaining this instead of a stub job (GitLab CI publishes
nothing to PyPI; that's GitHub Actions' job here, matching `certinext`).

## License

MIT (chosen for Zabbix community-templates submission-readiness — that
repo accepts MIT only). No per-file license headers; the root `LICENSE`
governs.

## ADRs and wishlist

Architecture decisions live in `docs/adr/` (MADR format, numbered
sequentially, e.g. `0001-standalone-repo.md`); deferred "not now" ideas
live in `docs/wishlist/` (`IDEA-NNN-slug.md`, indexed in
`docs/wishlist/README.md`). Several of this repo's foundational decisions
(standalone repo, copy-now/canonical-later, PyPI-not-published-yet,
GitHub push mirror, MIT license, community-templates-ready layout) are
recorded as ADRs 0001–0006 — read them before revisiting any of those
choices. When a durable decision gets settled in conversation, record it
as a new ADR rather than leaving the rationale only in a commit message
or chat history.

## AGENTS.md

`AGENTS.md` at the repo root carries these same operational facts for
non-Claude tooling — keep the two in sync when either changes.
