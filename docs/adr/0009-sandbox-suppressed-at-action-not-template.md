---
status: accepted
date: 2026-08-07
---

# Sandbox does not notify — suppressed by Zabbix action filter, not by removing triggers

## Context and problem statement

The template defines every metric twice, keyed `[prod]` and `[sandbox]`,
with the same triggers and the same severities on both. That symmetry was
a consequence of templating both environments identically, not a
decision anyone made about sandbox.

The result is hard to defend. The sandbox account carries a
**DISASTER**-severity trigger (`CertiNext sandbox: DCV lapsed`) plus
HIGH/AVERAGE/WARNING on `certinext.dcv.min_days_left[sandbox]` — the
same severity ladder as production, for an account that is a CI/CD
playground where stuck orders and lapsed DCV are ordinary test states.
The sandbox trigger descriptions already concede this, saying "Often a
deliberate test state."

Sandbox measurements on 2026-08-07 confirmed the environment behaves as
expected for a playground, and prod measurements the same day showed
prod shares much of the same structure — so this is a question about
*notification policy*, not about which environment is well-behaved.

**This ADR ratifies an approach the repo already prescribed.**
[../deployment.md](../deployment.md) step 5 ("Mute sandbox
notifications") already instructs the operator to add a *Tag value* |
`env` | *does not equal* | `sandbox` condition to the notifying trigger
actions. What was missing was the *decision* behind it — whether that
blanket mute was intended to cover the DCV severity ladder including the
DISASTER trigger, or only the routine metrics. This ADR settles that it
is blanket, and records why the alternatives were rejected.

## Considered options

- **Delete the `[sandbox]` triggers** from the template YAML.
- **Import with triggers, then disable them per-host** in Zabbix.
- **Lower sandbox severities** (e.g. DISASTER → WARNING) so sandbox
  still notifies, just more quietly.
- **Keep the triggers and filter sandbox out at the action level**, using
  the `env` tag every trigger already carries.

## Decision outcome

Chosen: **keep the triggers; exclude sandbox at the action level.**

Every item and trigger in the template already carries an `env` tag
valued `prod` or `sandbox`. A single Zabbix action condition excluding
`env = sandbox` therefore suppresses notification for *all* sandbox
metrics — order-health and DCV alike, including the DISASTER trigger —
with **no template change at all**.

This applies as a blanket principle: no sandbox trigger notifies anyone.
It is not decided metric by metric.

Deleting triggers was rejected because it churns UUIDs on re-add and
removes the problem from the Zabbix problem view entirely, not just from
notifications. Per-host disabling was rejected as the same outcome
reached by more manual steps. Lowering severities was rejected because
it keeps sandbox in the notification path at all, which is the thing
being removed — and a "quiet" page is still a page.

### Consequences

- Good: zero template surgery, no UUID churn, and one condition covers
  every current and future sandbox metric — including ones not yet
  written.
- Good: sandbox problems stay visible in the Zabbix problem view and on
  dashboards. Suppressing notification is not the same as discarding the
  signal, and someone debugging sandbox still sees it.
- Good: reversible in exactly one place.
- Bad: **the enforcement lives in Zabbix server configuration, outside
  this repository.** It is invisible to anyone reading the template,
  absent from version control, and lost in a server rebuild or restore.
  [../deployment.md](../deployment.md) step 5 and this ADR are the only
  in-repo record — compensating controls for a real gap, not a fix for
  it. Nothing detects the condition having been dropped except a sandbox
  problem paging someone.
- Bad: the sandbox DCV trigger still *displays* as DISASTER severity on
  dashboards even though it pages nobody, which reads as more alarming
  than it is.

## Confirmation

Verify after the template is imported and the action condition is
created: a sandbox trigger entering PROBLEM state must appear in the
Zabbix problem view while generating no notification. The template's
existing `env` tags can be confirmed in
`templates/template_certinext/7.0/template_certinext.yaml` — every item
and trigger definition carries one.

## More information

- [Zabbix action conditions](https://www.zabbix.com/documentation/7.0/en/manual/config/notifications/action/conditions)
  — event tag / tag value conditions are what implement this
- [Zabbix trigger tags](https://www.zabbix.com/documentation/7.0/en/manual/config/tagging)
- [ADR 0008](0008-orders-expiring-ships-data-only.md) — the other
  trigger-shape decision settled in the same session

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-08-07.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
