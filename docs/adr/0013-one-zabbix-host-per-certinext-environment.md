---
status: accepted
date: 2026-11-18
---

# One Zabbix host per CertiNext environment

## Context and problem statement

Up to 0.1.x, one Zabbix host carried both CertiNext environments. Every
item existed twice, keyed `[prod]` and `[sandbox]`, and the pusher chose
the key parameter from its connection. The template held about 400 lines
of duplicated sandbox items and triggers.

0.2.0 adds a dns-persist check
([ADR 0014](0014-dns-persist-checked-by-zabbix-agent.md)): a low-level
discovery rule, fed by the pusher, whose item prototypes are Zabbix agent
`net.dns.record` lookups, one set per apex. With both environments on one
host, that design runs into two problems:

- **One discovery rule fed by two pusher runs.** Each environment's daily
  run knows only its own apexes. Sending them to a shared rule means each
  run sends a partial list, and LLD's lost-resource handling then deletes
  the other environment's items with no warning. Avoiding that needs a
  coordinator that merges both snapshots before sending.
- **Per-environment keys can't come from a macro.** The Zabbix 7.0 docs
  for [trapper items][trapper] say: *"The Zabbix trapper process does not
  expand macros used in the item key"*. So the `[env]` parameter can't be
  made macro-driven, and keeping it means keeping a hard-coded duplicate
  block for every new item. The same apex can also exist in both
  environments, so agent keys for the two environments would collide on
  one host unless something made them unique.

Tod's requirements (2026-11-18): Zabbix, not the pusher, does the DNS
lookups; sandbox monitoring stays optional; keys stay clear, with no
tricks a reader can't decode.

## Considered options

- **One host per environment**, single-environment template, bare keys.
- **One host, a shared discovery rule plus a snapshot-merge coordinator**
  in the pusher.
- **One host, keys made unique with a trailing dot** on the queried name
  (`_validation-persist.<apex>.`) for one environment.
- **One host, keys made unique with a dummy timeout parameter** on
  `net.dns.record` for one environment.
- **A UserParameter wrapper** on the agent, keyed per environment.
- **DNS lookups in the pusher**, sent as trapper values.

## Decision outcome

Chosen: **one Zabbix host per environment** (Tod, 2026-11-18, after the
design had been through a Gemini review and a Codex review).

- Each environment gets its own logical host, for example
  `CertiNext prod` and `CertiNext sandbox`. Both carry an agent interface
  pointing at the pusher machine (passive checks don't care about the
  host name) and are monitored by the same server or proxy. A prod-only
  install doesn't create the sandbox host.
- One template, linked to each host. Item keys lose the `[env]`
  parameter (`certinext.dcv.min_days_left`, not
  `certinext.dcv.min_days_left[prod]`).
- Host macro `{$CERTINEXT.ENV}` (`prod` / `sandbox`) carries the
  environment into item and trigger names and the `env` tag, where macros
  *are* expanded ([macros by location][macros-loc]). It defaults to
  `prod`, so a prod-only install needs no macro.
- Each pusher run targets its host with the existing `--zabbix-host` /
  `ZABBIX_HOSTNAME`, so no routing code was needed.
- **Misroute guard.** Bare keys lose the rejection a mismatched `[env]`
  key used to cause. In its place, every run sends a text item
  `certinext.env` (`prod` or `sandbox`, from the connection), and a HIGH
  trigger fires on `last(certinext.env)<>"{$CERTINEXT.ENV}"`. A sandbox
  host left at the `prod` default raises it on its first run, which is
  the point.

### Consequences

- Good: each discovery rule only ever receives one environment's complete
  list, so no merge coordinator exists to fail.
- Good: the template lost its duplicated sandbox block; one definition per
  item and trigger.
- Good: a future key parameter becomes additive. IDEA-005 (originator
  bucketing) asked whether a `[class]` parameter was worth changing every
  existing key for; with `[env]` gone, adding `[automated]`/`[manual]`
  breaks nothing that exists.
- Bad: every item key changed, so 0.1.x and 0.2.0 pushers and templates
  don't mix. The pusher and template must be upgraded together, Zabbix
  side first (see the upgrade section of
  [docs/deployment.md](../deployment.md)).
- Bad: a second host to create and configure for anyone monitoring the
  sandbox, including its own agent interface and host macros.
- Bad: the misroute guard detects a mistake after the fact; it doesn't
  prevent one run's values landing on the wrong host.
- Neutral: the template's name, *CertiNext DCV by Zabbix trapper*, is
  unchanged although it now has agent item prototypes too. Renaming it
  means editing every trigger expression; not done.

### Amendment to ADR 0009

[ADR 0009](0009-sandbox-suppressed-at-action-not-template.md)'s decision
stands: sandbox doesn't notify, enforced by an action condition on the
`env` tag. Two details change:

- The `env` tag's value now comes from `{$CERTINEXT.ENV}` on each host,
  not from a literal in the template, so the condition only works if the
  sandbox host's macro is set (the misroute trigger catches a host left
  at the default).
- The three sandbox `nodata()` triggers no longer ship `DISABLED`; there
  are no per-environment trigger copies left to disable. A sandbox host
  that doesn't run all three pusher schedules should disable the
  matching `no data from pusher` triggers on that host.

## Pros and cons of the options

### One host per environment

- Good, because each discovery rule gets one complete list, and keys stay
  plain.
- Bad, because two hosts to set up, and the key change forces a coordinated
  upgrade.

### Shared rule plus merge coordinator

- Good, because it keeps one host.
- Bad, because the coordinator is a new failure point whose failure mode
  is LLD silently deleting the other environment's items.

### Trailing dot, or dummy timeout parameter

- Good, because both produce unique, working keys on one host.
- Bad, because both are tricks a reader can't decode, against the "clear
  keys" requirement, and neither solves the shared discovery rule.

### UserParameter wrapper

- Rejected alongside the key tricks; no separate reason was recorded.

### DNS lookups in the pusher

- Bad, because it breaks the requirement that Zabbix does the lookups.

## More information

- [ADR 0009](0009-sandbox-suppressed-at-action-not-template.md): sandbox
  suppression, amended above.
- [ADR 0011](0011-dcv-metrics-single-ca-by-flag.md): its "monitoring two
  CAs at once would need two hosts or per-CA keys" consequence is the
  same trade-off, settled the other way for environments.
- [ADR 0014](0014-dns-persist-checked-by-zabbix-agent.md): the
  dns-persist check that forced this decision.
- [IDEA-005](../wishlist/IDEA-005-originator-alert-severity-bucketing.md):
  its key-parameter question, now answered by this change.
- Plan: [docs/plans/env-hosts-dns-persist.md](../plans/env-hosts-dns-persist.md).
- [Zabbix 7.0 trapper items][trapper]: macros aren't expanded in trapper
  item keys.
- [Zabbix 7.0 macros supported by location][macros-loc].

[trapper]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/trapper
[macros-loc]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/macros/supported_by_location

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Haiku 5.5,
> `claude-haiku-5-5`) from the env-hosts plan and sessions with Tod Detre
> on 2026-11-18. May contain inaccuracies or hallucinated details; verify
> specifics against current sources before relying on them.
