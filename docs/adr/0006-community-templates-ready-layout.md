---
status: accepted
date: 2026-07-22
---

# Structure for Zabbix community-templates submission-readiness, without submitting yet

## Context and problem statement

This repo's primary deliverable is the pip-installable
`certinext-zabbix-push` pusher — a normal Python-project layout. The
official [Zabbix community-templates repository](https://www.zabbix.com/documentation/guidelines/en/thosts/community_templates),
by contrast, treats a template and its helper scripts as one self-
contained folder unit with its own required layout. These two shapes are
in tension. Should the template just live wherever is convenient for the
Python package, or should it be laid out to match the community
conventions even before any submission is planned?

## Considered options

- Plain Python-package layout: template YAML lives wherever is
  convenient for the pusher package (e.g. alongside the CLI code),
  ignoring community-templates folder conventions.
- Fully submission-ready **and** submit now to `zabbix/community-templates`.
- Submission-ready layout, but do not submit yet (chosen).

## Decision outcome

Chosen: **lay the template subtree out to match community-templates
conventions now, without submitting**. This reconciles the two channels:
the pusher stays a normal Python package, while
`templates/template_certinext/7.0/template_certinext.yaml` follows the
community repo's required `template_<name>/<X.X>/` layout (folder/file
regex `^template_[.a-zA-Z0-9()_-]+$`), a single uncompressed YAML file,
and a per-template `README.md` (setup, required macros, metrics,
triggers, author) — the community repo's recommended contents. That way
a future PR to `zabbix/community-templates` is a copy, not a
restructure.

Also corrects an assumption from this idea's originating discussion
(recorded in `ums-certinext-scripts` — a UMS-internal repository, not
publicly accessible): the community-templates page does **not** require
`class`/`target`/`component`/`subject` tags; the real submission gate
was the license (see ADR 0005), now resolved.

### Consequences

- Good: the template subtree is submission-ready today, at zero ongoing
  cost — no separate maintenance branch or later restructuring needed if
  a submission is ever made.
- Good: the template must already import clean on a fresh Zabbix
  install with no external template linkage (a submission requirement
  anyway), which is also just good template hygiene.
- Neutral: no submission has been made or committed to — see the
  wishlist idea tracking that decision (`docs/wishlist/IDEA-002-community-templates-submission.md`).

## More information

- [Zabbix community-templates guidelines](https://www.zabbix.com/documentation/guidelines/en/thosts/community_templates) — the layout, per-template README, and license requirements this repo conforms to.
- [Zabbix template import/export (7.0)](https://www.zabbix.com/documentation/7.0/en/manual/xml_export_import/templates)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-21/22.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
