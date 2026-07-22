---
status: accepted
date: 2026-07-22
---

# Copy now; make canonical later (phased, effort-gated)

## Context and problem statement

The pusher and template being extracted into this repo still run in
production out of `ums-certinext-scripts` — a University of Maine System
(UMS) internal repository, **not publicly accessible** (mentioned here
only for provenance; outside readers cannot reach it). Once
`certinext-zabbix` exists, should it immediately become the single
source of truth — with `ums-certinext-scripts` deleting its copy and
depending on this package — or should the two coexist for a while?

## Considered options

- Immediate cutover: extract, then have `ums-certinext-scripts` depend on
  `certinext-zabbix` right away and delete its own copy.
- Phased: `certinext-zabbix` starts as a sanitized copy;
  `ums-certinext-scripts` keeps running its own copy unchanged; a
  later, separately gated phase does the cutover.

## Decision outcome

Chosen: **phased**. Tod: "plan for making the new repo canonical, but
probably not right away — it depends on how much effort it will take."
Phases 1–5 of the extraction are a sanitized copy: `ums-certinext-scripts`
is left completely untouched and keeps running production unchanged. A
future Phase 6 — separately gated on an effort estimate produced at the
end of Phase 5, and a UMS decision — does the canonical migration:
`certinext-zabbix` publishes to PyPI, `ums-certinext-scripts` deletes its
copy and depends on the package, and both ship a coordinated release.

### Consequences

- Good: zero risk to UMS production during the extraction — nothing in
  `ums-certinext-scripts` changes until Phase 6 is explicitly greenlit.
- Bad: **two sources of truth** for the push logic, the Zabbix template,
  and the `KEY_*` item-key constants exist in the interim — a fix or
  change in one repo does not propagate to the other. This is the
  explicit cost accepted by this decision, mitigated only by cross-
  pointers left in each repo (see the extraction plan, Phase 1 step 7)
  and by keeping this ADR/the plan visible.
- Neutral: Phase 6 has no committed date — it happens only when someone
  actively decides the effort is worth it.

### Confirmation

`ums-certinext-scripts/zabbix_push.py` and
`zabbix/template_certinext.yaml` still exist and are still what UMS
deploys from, as long as this ADR's status is `accepted` and no Phase 6
ADR supersedes it.

## More information

- Fuller planning context (including the Phase 6 outline for the eventual
  cutover) lives in `docs/plans/certinext-zabbix-extraction.md` in the
  `sysadmin-ansible` superproject — a UMS-internal repository, not
  publicly accessible.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-21/22.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
