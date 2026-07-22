# IDEA-002: Submit the template to the official Zabbix community-templates repository

- **Status:** Proposed
- **Created:** 2026-07-22
- **Updated:** 2026-07-22
- **Tracking issue:** sysadmin/certinext-zabbix#2

## Context

`templates/template_certinext/7.0/` was deliberately laid out to match
the official [Zabbix community-templates repository](https://www.zabbix.com/documentation/guidelines/en/thosts/community_templates)'s
conventions — the `template_<name>/<X.X>/template_<name>.yaml` folder
structure, a per-template `README.md`, and the MIT license that repo
requires (see ADR 0006 and ADR 0005) — specifically so a future
submission there would be a copy, not a restructure. That decision
explicitly deferred the submission itself; it only made submission
*possible* later, cheaply.

## The idea

Open a pull request to `zabbix/community-templates` adding
`template_certinext` alongside this repo's own distribution. Being
listed there would put the template in front of the wider Zabbix
community (not just InCommon/CertiNext users who already know to look at
`github.com/tod-uma/certinext-zabbix`), and would put it through that
project's own review process.

## Why not now

- No submission has ever been drafted or discussed with the
  `zabbix/community-templates` maintainers — the layout work only kept
  the door open, it didn't commit to walking through it.
- Community-templates review may ask for changes (naming, tagging,
  additional documentation, or template behavior) that haven't been
  scoped yet — submitting is not just "open a PR," it's an ongoing
  review relationship with an upstream project this org doesn't
  control.
- This repo's own public release (README, CI, GitHub mirror) hasn't
  shipped yet as of this writing — submitting upstream before this
  repo itself is stable and has real users would be premature.

**What would change this:** this repo ships (Phase 5 of the extraction
plan) and proves stable for a while, and/or an external user asks why
the template isn't in the official community-templates listing.

## Pros

- Reaches Zabbix users who browse the official community-templates
  index but wouldn't otherwise discover a small, independent GitHub
  repo.
- The layout work (ADR 0006) is already done — the incremental cost of
  actually submitting is comparatively small.

## Cons / costs

- An ongoing relationship with an upstream project's review/maintenance
  expectations, on top of maintaining this repo directly.
- Possible community-templates-specific requirements not yet
  encountered (their review may ask for things this repo doesn't
  currently have).
- Two places where an interested user might now expect updates
  (`community-templates` and this repo) if the submission is accepted.

## Effort

Small-to-medium: the template itself needs no rework (that was the
point of ADR 0006), but drafting the PR, responding to review feedback,
and deciding how to keep a submitted copy in sync with this repo's own
copy going forward are new, unscoped work.

## Open questions & caveats

- How to keep a submitted template in sync with this repo's own copy if
  accepted — treat `zabbix/community-templates` as another
  copy-not-canonical target (see ADR 0002 for the pattern this repo
  already accepted once), or make the community-templates copy
  authoritative instead?
- Whether community-templates review requires anything not already
  reflected in this repo's `templates/template_certinext/7.0/` (find out
  by reading their contribution process before drafting a PR, not by
  guessing).

## Next steps

- Tracked in sysadmin/certinext-zabbix#2.
- Revisit once this repo has shipped (Phase 5 of the extraction plan)
  and had some real-world use. At that point: read
  `zabbix/community-templates`'s own contribution guide in full, then
  draft the PR.

## References

- [Zabbix community-templates guidelines](https://www.zabbix.com/documentation/guidelines/en/thosts/community_templates)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre. May contain
> inaccuracies or hallucinated details; verify specifics against current
> sources before relying on them.
