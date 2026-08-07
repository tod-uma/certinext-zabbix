# IDEA-004: Per-domain visibility into stuck orders via Zabbix LLD

- **Status:** Proposed
- **Created:** 2026-08-07
- **Updated:** 2026-08-08

## Context

The order-health metrics (`certinext.orders.unissued`,
`certinext.orders.undownloaded`, `certinext.orders.failed_recent`) only
report **counts** — e.g. "3 orders awaiting issuance."
`OrderRecord.common_name` (the order's primary domain) is already present
on every fetched record (`fetch_orders()` / `bucket_orders()` in
`certinext_zabbix/zabbix_push.py`), but isn't surfaced anywhere: an admin
seeing one of those alerts fire still has to log into the CertiNext portal
to find out *which* domain's order is stuck.

## The idea

Use Zabbix Low-Level Discovery (LLD) to turn each unissued/undownloaded/
failed order's
`common_name` into its own discovered item, instead of (or alongside) the
aggregate counts. A discovery rule would push one JSON entry per stuck order
(keyed on domain/order number), and an item prototype would turn that into a
per-domain "stuck since" or "status" item — auto-created while the order is
stuck, auto-retired once it resolves, matching how the existing template's
non-LLD items are hand-defined per environment.

## Why not now

This is materially more scope than the count-based metrics already shipped:
it needs a discovery rule, an item prototype (and possibly a trigger
prototype), and a decision about how discovered items expire cleanly when an
order resolves (Zabbix's "keep lost resources for N days" LLD setting) so
resolved orders don't linger as stale items. The count-based metrics answer
"is there a problem" cheaply; this answers "which domain" at real
template-authoring cost. Worth doing once the count-based alerts have proven
themselves useful in practice and someone's actually had to go log into the
portal to find the stuck domain more than once.

**What would change this:** the count-based order-health metrics
(unissued, undownloaded, failed-recent) are live and have fired at least once in practice, and the
"which domain" lookup step is enough friction that automating it is clearly
worth the added template complexity.

## Pros

- Turns a count-based alert into an actionable one — the domain is right
  there in Zabbix, no portal login needed to triage.
- `common_name` is already in hand on every fetched order record; no new API
  calls needed, same principle as the cert-expiry-from-orders metric.

## Cons / costs

- Needs a discovery rule + item (and maybe trigger) prototype, not just a
  new trapper item — more template surface area to maintain.
- Needs a decision on stale-item expiry (Zabbix's LLD "keep lost resources"
  setting) so resolved orders' discovered items don't linger indefinitely.
- `common_name` can be `None` when the orders report response doesn't
  include a domain field for a given order (see its docstring in
  `certinext/models/orders.py`) — discovery data needs a fallback key
  (e.g. `order_number`) for those rows so they aren't silently dropped from
  discovery.

## Effort

Medium: a new discovery-rule-shaped push (JSON payload via the trapper
protocol, per the
[Zabbix LLD documentation](https://www.zabbix.com/documentation/current/en/manual/discovery/low_level_discovery)),
an item prototype per discovered `{#DOMAIN}` (or `{#ORDER}`), and a
decision on prototype triggers vs. relying on the existing aggregate
triggers. No new CertiNext API calls — same data already fetched by
`fetch_orders()`.

## Open questions & caveats

- One discovery rule per bucket (unissued, undownloaded, failed) or a single rule with a
  status field per entry? A single rule is less template surface but makes
  per-bucket trigger severity harder to express.
- Key discovery entries on `common_name` or `order_number`? `order_number`
  is always present and stable; `common_name` is what an admin actually
  wants to see but can be `None` (see Cons).
- Does this replace the aggregate count items, or run alongside them? Likely
  alongside — the counts are what the existing triggers key on, and LLD adds
  the "which one" layer, not a replacement for "is there a problem."

## Next steps

- Ship and observe the count-based pending/failed-recent metrics in
  production for a while.
- Revisit once there's a concrete "had to log into the portal to find the
  stuck domain" incident, or the count alone is proving insufficient for
  triage.

## References

- [Zabbix Low-Level Discovery documentation](https://www.zabbix.com/documentation/current/en/manual/discovery/low_level_discovery)
- `OrderRecord.common_name` — `certinext/models/orders.py` (the field this
  idea would surface)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-08-07. May
> contain inaccuracies or hallucinated details; verify specifics against
> current sources before relying on them.
