# IDEA-003: Add a severity trigger for days-since-last-issued once there's a baseline cadence

- **Status:** Proposed
- **Created:** 2026-08-07
- **Updated:** 2026-08-07

## Context

`certinext.orders.days_since_issued[<env>]` (added alongside the other
order-health metrics) is pushed by the daily `--order-health` run but
ships with **no trigger** — see the item's description in
`templates/template_certinext/7.0/template_certinext.yaml`. It reports
days since the most recent order with an "issued" certificate status, by
`order_date`.

## The idea

Once a few weeks or months of real data has accumulated (*Monitoring →
Latest data* / a graph on this item), pick a threshold from the observed
normal maximum gap between issuances for this account, then add a trigger
— a single `last(item)>{$MACRO}` at WARNING, or a full severity gradient
mirroring the DCV-expiry triggers already on this template — via a new
macro (e.g. `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}`).

## Why not now

There is no baseline yet for what a *normal* gap between certificate
issuances looks like for this account — it could be daily for a large,
constantly-rotating fleet, or monthly/quarterly for a small one. Guessing
a number now risks either alert fatigue (too tight — pages during a
perfectly normal quiet period) or a useless trigger (too loose — never
fires, or fires so late it's not actually an early warning). This is the
same caution already applied to the CertiNext DCV-renew plan's
"window not API-readable, so keep it a tunable `_days` var" decision —
don't fix a number until there's real data to anchor it on.

**What would change this:** enough history in Zabbix (a few weeks to a
few months, depending on this account's actual issuance frequency) to
identify the longest *normal* gap between issuances, so a threshold set
above that gap means a real problem, not noise.

## Pros

- Turns `days_since_issued` into a real early-warning check instead of
  just a dashboard number.
- A severity gradient would match the DCV-expiry triggers' style, keeping
  the template internally consistent.

## Cons / costs

- Needs real historical data before choosing a number — can't be done at
  template-authoring time.
- A wrong threshold either creates noise or is silently useless; whoever
  sets it needs to actually look at the accumulated graph first, not just
  pick a round number.

## Effort

Small once there's data to act on: one new macro plus a `last()`-based
trigger (or a gradient of them, copying the DCV-expiry pattern) in the
template. No pusher code changes needed — the metric already exists and
is already being pushed.

## Open questions & caveats

- Single WARNING threshold, or a full multi-tier gradient like DCV expiry
  (WARNING → AVERAGE → HIGH → DISASTER)? The latter only makes sense if
  there's a meaningful escalation story (e.g. "mildly overdue" vs.
  "definitely broken"), which isn't obvious for this metric the way it is
  for DCV expiry counting down to a hard cutoff.
- Should the threshold be a fixed number of days, or something computed
  from the observed average/variance? A fixed macro is far simpler to
  express in a Zabbix trigger expression; a computed threshold would need
  a calculated item and is probably not worth the complexity unless the
  fixed number proves too noisy in practice.

## Next steps

- Watch `certinext.orders.days_since_issued[prod]` in Zabbix for a few
  weeks/months once `--order-health` is deployed.
- Revisit this idea once there's a real "worst normal gap" to anchor a
  threshold on.

## References

- This template's existing DCV-expiry severity gradient
  (`{$CERTINEXT.DCV.WARN_DAYS}`/`AVG_DAYS`/`HIGH_DAYS`/`DISASTER_DAYS}`)
  is the pattern to mirror if a gradient is chosen over a single
  threshold.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-08-07. May
> contain inaccuracies or hallucinated details; verify specifics against
> current sources before relying on them.
