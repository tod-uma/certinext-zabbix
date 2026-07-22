---
status: accepted
date: 2026-07-22
---

# MIT license

## Context and problem statement

`certinext-zabbix` is a new public release, so its license had to be
chosen fresh. The files it's built from (originally written inside
`ums-certinext-scripts` — a UMS-internal repository, not publicly
accessible) carry no license header, and that source repo has no
`LICENSE` file or `license` field in its `pyproject.toml`, so there is no
relicensing hurdle: these files are being licensed for the first time
here. Apache-2.0 was the initial assumption, since it's the precedent
used elsewhere at UMS (e.g. the internal repo's release tooling carries
an Apache-2.0 header). What license should this repo actually use?

## Considered options

- Apache-2.0 (the UMS-internal precedent).
- MIT.

## Decision outcome

Chosen: **MIT**, for Zabbix community-templates submission-readiness —
the official [Zabbix community-templates repository](https://www.zabbix.com/documentation/guidelines/en/thosts/community_templates)
accepts **MIT only**: "Third-party licenses, even the compatible ones
(for example, GPL v2/v3) are not accepted." Both MIT and Apache-2.0 are
permissive; MIT is the one that keeps a future community-templates
submission open (see ADR 0006). Deciding this up front kept the whole
repo consistent from its first commit.

### Consequences

- Good: nothing blocks a future submission to `zabbix/community-templates`
  on licensing grounds.
- Good: a root `LICENSE` file plus `license = "MIT"` in `pyproject.toml`
  is the entire mechanism — no per-file header churn, since the copied
  files had none to begin with and none are added.
- Neutral: differs from the Apache-2.0 UMS-internal precedent, but that
  precedent governs UMS-internal tooling, not this public repo.

## More information

- [Zabbix community-templates guidelines](https://www.zabbix.com/documentation/guidelines/en/thosts/community_templates) — states the MIT-only requirement.
- [MIT License](https://opensource.org/license/mit)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-21/22.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
