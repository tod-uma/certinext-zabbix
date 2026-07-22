---
status: accepted
date: 2026-07-22
---

# PyPI-ready, but not published from the start

## Context and problem statement

All of `certinext-zabbix`'s runtime dependencies (`certinext`,
`zabbix-utils`, `filelock`, `structlog`, `typer`) are already on public
PyPI, so nothing technically blocks publishing this package there too.
Should the first release go straight to PyPI, or should the repo be
built PyPI-ready without actually publishing yet?

## Considered options

- Publish to PyPI from the first release.
- Build the package to be PyPI-ready (correct metadata, a CI `build`
  job validating the wheel/sdist) but only distribute via
  `pip install git+https://...` for now.

## Decision outcome

Chosen: **PyPI-ready but not published**. Tod: "I don't want to do PyPI
from the start, but build it so we could do it in the future if members
request it." CI therefore includes a `build` job but no publish job.
Flipping to PyPI later is: register the project, add a PyPI API token to
CI variables, and add a tag-triggered `twine upload` job — the plan
leaves a commented stub for this in `.gitlab-ci.yml`.

### Consequences

- Good: no PyPI account/token/release-process overhead until there's
  actual external demand.
- Good: the install story (`pip install git+https://github.com/tod-uma/certinext-zabbix`)
  works today with zero extra setup.
- Bad: a git-install is a slightly rougher onboarding experience than
  `pip install certinext-zabbix` — acceptable while the user base is
  unknown.
- Neutral: nothing about the package's structure needs to change when
  PyPI publishing is later turned on; only CI and PyPI-side registration
  are added.

## More information

- [Publishing package distribution releases using GitHub Actions / PyPI trusted publishing concepts apply generally](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/) — general reference for the eventual publish step.
- [`certinext` on PyPI](https://pypi.org/project/certinext/) — the dependency + mirror pattern this repo replicates once it does publish.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-21/22.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
