# IDEA-005: Bucket order-health alert severity by `originator`

- **Status:** Proposed
- **Created:** 2026-08-08
- **Updated:** 2026-08-07

> **This idea is now a prerequisite, not just an enhancement.**
> [ADR 0008](../adr/0008-orders-expiring-ships-data-only.md) ships
> `certinext.orders.expiring` with no trigger and defers the trigger
> decision to this idea. See "Prod evidence (2026-08-07)" below.

## Context

The order-health metrics
(`certinext.orders.unissued`, `certinext.orders.undownloaded`,
`certinext.orders.failed_recent`) are single counts spanning every order in
the account, regardless of how it got there. But the orders come from
channels with very different expectations:

| `originator` | prod share (100-row sample, 2026-08-07) | expectation |
| --- | --- | --- |
| `ACME` | 57 | fully automated — an order should go from submitted to downloaded within seconds. Anything stuck is a broken client. |
| `CERTInext` | 39 | a human in the portal — can legitimately sit for hours or days awaiting an approver. |
| `CERTInext API` | 4 | scripted, but not necessarily unattended. |

(The sandbox account's dominant originator is `emSign-Hub API`, 54 of 58
rows, with 4 `ACME` — so the value set is account-specific and must not be
hardcoded to the prod three.)

A single `{$CERTINEXT.ORDER.STUCK_AGE}` has to be set loose enough not to
page on a normal manual approval wait, which makes it far too loose to
catch a broken ACME client promptly — the case where fast detection
actually matters, because nobody is watching it.

`OrderRecord.originator` was added to `certinext` on 2026-08-07 and is
already available at the version this repo pins. It was confirmed populated
on every row of a fresh 462-row prod sample; a prod fixture predating that
date showed it as always `None`, and `certinext`'s
`test_probe_r16_orders_originator_populated` guards against a regression
back to that state.

## Prod evidence (2026-08-07)

A read-only measurement of the production account added a second, stronger
driver than the stuck-age tuning conflict described above: **expiry
monitoring is meaningless without this split.**

- Prod certificate lifetimes are bimodal — 61 of 130 cert-bearing orders
  are ~30-day, 62 are ~199-day. The ~30-day population is overwhelmingly
  ACME.
- A 30-day certificate is inside a 30-day expiry window from the moment
  it is issued, so `certinext.orders.expiring` counts healthy,
  freshly-renewed ACME certs. `lv-o-swdist02.its.maine.edu` was counted
  two days after a successful renewal.
- Correlating by common name, **all 3 genuinely-lapsed certificates
  (newest cert for their CN, already expired) are `CERTInext API`
  originator. None are ACME.**

The operational reason is that ACME certificate expiry is **already
monitored by other means**. The uncovered risk is a certificate obtained
manually — through the portal or a one-off API call — that nobody renewed.
That is precisely an originator distinction, which makes ACME-vs-REST-vs-portal
the axis this metric needs before any trigger on it can mean anything.

This suggests the class split wanted here is at least three-way
(ACME / API / portal), not the two-way automated-vs-manual split sketched
below — and that the split should apply to expiry metrics, not only the
stuck-order counts.

## The idea

Split each order-health count by originator class rather than pushing one
aggregate, so each class can carry its own threshold:

- Push per-class item keys (e.g.
  `certinext.orders.unissued[prod,automated]` vs
  `certinext.orders.unissued[prod,manual]`), with a separate stuck-age
  macro per class — tight for automated, loose for manual.
- Classify with a configurable mapping rather than a hardcoded list, since
  the observed values differ between prod and sandbox and are vendor
  strings that can change.

Keep the existing aggregate items as-is so nothing that already alerts on
them breaks.

## Why not now

The prerequisite is a clean baseline, which does not exist yet. Both
order-stuck triggers currently fire immediately against the production
account's backlog of abandoned orders from earlier experimentation (see
[../deployment.md](../deployment.md) — that backlog has to be cancelled
before order-health notifications get enabled at all). Splitting a metric
that is saturated at "always firing" adds template complexity without
adding signal.

Beyond that, an alert-threshold decision wants evidence about *normal*
per-class behavior, which needs the aggregate metrics running against a
cleaned-up account for a while first — the same reason
[IDEA-003](IDEA-003-days-since-issued-alert-threshold.md) is deferred.

**What would change this:** the abandoned-order backlog is cleared, the
aggregate order-health metrics have run against the cleaned account long
enough to show a steady state, and the single stuck-age threshold has
demonstrably had to be compromised — either it pages on normal manual
approvals, or an ACME client broke and the alert took too long to fire.

## Pros

- Catches a broken ACME client in minutes instead of whatever loose
  threshold the manual-approval case forces.
- Removes the tuning conflict between two workflows with genuinely
  different tolerances.
- No new API calls: `originator` is already on every fetched record.

## Cons

- Doubles (or triples) the order-health item and trigger count, on a
  template that already carries 18 items and 26 triggers.
- Depends on vendor free-text-ish channel names. `originator` is a typed
  string field rather than a display string, but its value set is
  undocumented and observably account-specific, so the classification
  mapping is config, not a constant — and a new unmapped value has to fall
  back to something safe rather than silently vanishing (the same
  catch-all rule `bucket_orders()` already applies to unknown
  `order_status` values).

## Open questions & caveats

- Key parameter (`[prod,automated]`) vs. separate key names
  (`certinext.orders.unissued_automated[prod]`)? The former composes with
  the existing `[env]` parameter but changes every existing order-health
  key; the latter avoids touching them.
- Is two classes (automated/manual) enough, or does `CERTInext API` want
  its own tier?
- Where does the mapping live — a CLI flag, an env var, or a config file?
  This repo currently has no config-file mechanism at all.
- Interaction with [IDEA-004](IDEA-004-stuck-order-domain-visibility-lld.md)
  (per-domain LLD): if LLD lands first, `originator` becomes an item
  *property* on the discovered order rather than a bucketing dimension,
  and this idea may be redundant.
