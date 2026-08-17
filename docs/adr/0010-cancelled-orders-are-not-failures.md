---
status: accepted
date: 2026-08-17
---

# Cancelled orders are excluded from `failed_recent`, which keeps its trigger

## Context and problem statement

`certinext.orders.failed_recent` counts orders in a terminal non-fulfilled
state whose `order_date` falls inside `--order-failing-lookback-days`
(default 30), and carries a `last(...)>0` WARNING trigger on both
`[prod]` and `[sandbox]`.

`bucket_orders()` fills that bucket from a catch-all: any `order_status`
that isn't recognized is treated as a failure. The catch-all exists so a
vendor status nobody has seen surfaces instead of vanishing — the
rejected / expired / revoked orders the metric was designed for have
never been observed, and their `order_status` strings are undocumented.

The concern raised when the metric shipped was that cancelling an order
is routine administrative work, not an incident, so a `>0` trigger over a
rolling 30-day window might be alerting on normal activity. That was
never analysed. [ADR 0008](0008-orders-expiring-ships-data-only.md) had
just found exactly that shape of error in `certinext.orders.expiring`, so
the question needed measuring rather than assuming.

## Decision drivers

- The production account was measured for `expiring` and the assumption
  there turned out to be wrong in the opposite direction from what was
  expected. Any decision here should rest on the same kind of evidence.
- Order-health notifications are not enabled yet, so this is cheap to fix
  now and expensive to fix after people start ignoring the alert.
- `certinext` IDEA-011 proposes an order-cleanup CLI that will
  deliberately cancel orders in bulk.

### The measurement

Read-only production `--dry-run`, 2026-08-17, whole order history:

- **All 9 rows in the `failed` bucket are `Order Cancelled`.** Zero
  rejected, expired, or revoked orders have ever appeared. The statuses
  the metric was built for have never occurred.
- Cancellations fall between 2026-05-29 and 2026-07-09, on 6 distinct
  days, 1–3 per day.
- Replaying the rolling 30-day window across the period where it sits
  entirely inside available history (60 days), the trigger would have
  been in PROBLEM **50 of 60 days — 83%**, as a single continuous 50-day
  episode. It reads 0 today only because the last cancellation was 39
  days ago.

So the trigger's observed behaviour is approximately 100% routine
administrative activity and 0% incident, at a duty cycle comparable to
the one ADR 0008 rejected.

The order history spans only 2026-05-20 to 2026-08-17. Tod confirmed this
is **account age** — the CertiNext account was opened 2026-05-21 — not
vendor retention. It is still a single observation period rather than a
long baseline.

## Considered options

- **Exclude cancellations from the failed bucket, keep the trigger.**
  Recognize `Order Cancelled` as terminal-but-routine. `failed_recent`
  then counts only genuinely unrecognized terminal statuses.
- **The same, plus a data-only `cancelled_recent` metric**, so
  cancellations stay visible in Zabbix.
- **Ship `failed_recent` trigger-less**, mirroring ADR 0008 exactly:
  remove both triggers, keep the item as data.
- **Keep `last(...)>0` unchanged** and accept the 83% duty cycle,
  treating cancellations as worth a look.

## Decision outcome

Chosen: **exclude cancellations from the failed bucket and keep the
trigger.**

This treats the problem as a classification error rather than a threshold
problem — the same conclusion ADR 0008 reached, reached the same way. The
catch-all's actual purpose was to surface a status nobody has seen;
`Order Cancelled` is a status we *have* seen and understand, so leaving it
in the catch-all was the defect. Once removed, `failed_recent > 0` means
"a terminal status occurred that this code has never encountered", which
is worth waking someone for and would have fired zero times across the
account's life.

Keeping the trigger is what distinguishes this from ADR 0008: `expiring`
had no salvageable signal at any threshold, whereas here the signal is
intact and was merely buried in noise.

The `>0` comparison, the 30-day lookback, and the trigger expressions in
the template are all unchanged — this is a bucketing change, so the
template needs only a description correction.

Cancellations go to a separate `OrderBuckets.cancelled` field that feeds
no metric, and their count is logged each run. The **data-only metric was
declined** — it costs two more template items for something nobody has
asked to see yet — but bucketing them separately rather than discarding
them keeps the count auditable in the run log and means the metric can be
added later without re-plumbing the classification.

### Consequences

- Good: the trigger stops firing on routine work. In production the
  `failed` bucket is now empty, so it is quiet until something genuinely
  novel happens.
- Good: IDEA-011's bulk cancellations will not spike the metric.
- Good: the alert's meaning becomes precise and explainable — "an
  unrecognized terminal status appeared" — instead of "some order ended
  badly, possibly on purpose".
- Bad: a *wrongly* cancelled order — someone cancels an order that was
  still needed — now raises no alert. Accepted: nothing detected that
  before either, because the trigger was in PROBLEM most of the time
  regardless. The cancellation count in the run log is the mitigation,
  and the declined data-only metric is the upgrade path if this bites.
- Bad: the decision rests on ~90 days of history from a young account. If
  the vendor's rejected/expired/revoked statuses turn out to be common,
  the picture changes — but that would make the trigger *more* useful,
  not less, which is the safe direction to be wrong in.
- Neutral: `--order-failing-lookback-days` keeps its 30-day default. It
  now bounds only unrecognized statuses.

### Confirmation

Verified against production read-only on 2026-08-17 after the change: the
run logs `Cancelled orders excluded from the failed bucket count=9`, no
unrecognized-status log line appears at all, and `failed_recent` is 0.
Unit tests cover a cancellation reaching the `cancelled` bucket, staying
out of `failed`, and not raising the metric even when dated yesterday.

## More information

- [ADR 0008](0008-orders-expiring-ships-data-only.md) — the sibling
  decision on `certinext.orders.expiring`, which reached the opposite
  conclusion about the trigger for a metric whose signal was
  unrecoverable.
- Internal GitLab issue
  [#6](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/6)
  raised this question. `gitlab.its.maine.edu` is the University of Maine
  System's internal instance and is not publicly reachable.
- [`docs/plans/order-health-followups.md`](../plans/order-health-followups.md)
  — step 3 is what this ADR closes.
- [Zabbix 7.0 trigger expressions](https://www.zabbix.com/documentation/7.0/en/manual/config/triggers/expression)
  — the `last()` function used by the trigger.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Opus 5,
> `claude-opus-5`) from a conversation with Tod Detre on 2026-08-17.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
