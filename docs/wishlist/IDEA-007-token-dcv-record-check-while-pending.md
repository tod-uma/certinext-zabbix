# IDEA-007: Check a token-method DCV record in DNS while its validation is pending

- **Status:** Proposed
- **Created:** 2026-11-18
- **Updated:** 2026-11-18

## Context

0.2.0 has the Zabbix agent check each dns-persist apex's
`_validation-persist` TXT record, discovered from the pusher
([ADR 0014](../adr/0014-dns-persist-checked-by-zabbix-agent.md)). Token
methods (DNS-TXT and similar) are covered only by `validTill` and by
`certinext.domains.unverified`: nothing looks at whether the token record a
pending validation needs is actually in DNS.

The idea was listed in the env-hosts plan
([docs/plans/env-hosts-dns-persist.md](../plans/env-hosts-dns-persist.md))
as a wishlist item to write in session 3. Nothing beyond its one-line
description was discussed, so the sections below record only that, and
what 0.2.0 already provides.

## The idea

While a token-method domain's DCV is pending, have Zabbix look up the
token record it needs, the same way the persist check does.

## Why not now

Out of scope for 0.2.0, whose job was the dns-persist check and the
one-host-per-environment change. No further reason was recorded.

## Effort

Not estimated. The discovery machinery 0.2.0 built (pusher-sent discovery,
lost-resource handling, never sending a partial list, the LLD-macro test)
would carry over.

## Open questions & caveats

- Where the token record's name and expected value come from (the DCV
  detail the pusher already reads per domain is the obvious candidate;
  unverified).
- Whether a check that exists only while a validation is pending is worth
  the item churn, given LLD deletes items after its lost-resource window.

## References

- [Zabbix 7.0 agent item keys (`net.dns.record`)](https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/zabbix_agent)
- [Zabbix 7.0 low-level discovery](https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Haiku 5.5,
> `claude-haiku-5-5`) from the env-hosts plan, 2026-11-18. May contain
> inaccuracies or hallucinated details; verify specifics against current
> sources before relying on them.
