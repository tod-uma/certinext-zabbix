---
status: accepted
date: 2026-07-22
---

# Standalone repo, not folded into the `certinext` library

## Context and problem statement

`certinext-zabbix` extracts a Zabbix template and trapper-pusher CLI out
of `ums-certinext-scripts` — a University of Maine System (UMS) internal
repository, **not publicly accessible** — for public release. The
extraction plan's original wishlist idea (recorded in that internal
repo's `docs/wishlist/IDEA-002-public-zabbix-template.md`) left open
whether this should be its own repo or a subdirectory of the
already-public `certinext` API client library. Where should this code
live?

## Considered options

- Its own standalone repo (`certinext-zabbix`)
- A `zabbix/` subdirectory inside the `certinext` library's repo

## Decision outcome

Chosen: **standalone repo**. The template + pusher are an operational
monitoring concern with a different release cadence and audience (ops
teams importing a Zabbix template and scheduling a pusher) than the
`certinext` API client library (Python developers building on the
CertiNext API). Bundling would force library consumers to carry
Zabbix/`typer`/`filelock` dependencies they don't want, and would couple
two independent release cadences together.

### Consequences

- Good: `certinext` stays a lean API client with no Zabbix-specific
  dependencies.
- Good: this repo can version, release, and (eventually) accept
  community-templates-review feedback independently of `certinext`.
- Neutral: two repos to maintain instead of one, but with genuinely
  different audiences this is the right split rather than added
  overhead.

## More information

- [certinext on PyPI](https://pypi.org/project/certinext/) / [GitHub](https://github.com/tod-uma/certinext) — the library this repo depends on rather than bundles with.
- Fuller planning context lives in `docs/plans/certinext-zabbix-extraction.md`
  in the `sysadmin-ansible` superproject — a UMS-internal repository, not
  publicly accessible.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-21/22.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
