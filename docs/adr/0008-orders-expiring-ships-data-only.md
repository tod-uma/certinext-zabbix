---
status: accepted
date: 2026-08-07
---

# `certinext.orders.expiring` ships data-only, with no trigger

## Context and problem statement

`certinext.orders.expiring` counts cert-bearing orders whose
`certificate_expiry_date` falls within `--order-cert-expiry-days`
(default 30), already-expired ones included. It shipped with a
`last(...)>0` WARNING trigger on both `[prod]` and `[sandbox]`.

The metric was known to saturate in sandbox, which was assumed to be a
sandbox artifact — a CI/CD playground full of short-lived certs — with
prod expected to look different. Prod had never been measured. A
read-only dry-run against the production account on 2026-08-07 measured
it for the first time, and the assumption did not hold.

Prod certificate lifetimes are bimodal: of 130 cert-bearing orders, 61
are ~30-day and 62 are ~199-day. Because nothing at all expires between
30 and 90 days out, the count is **identical at a 30-day and a 90-day
threshold — 68 either way**. Threshold tuning on this metric is not
merely unvalidated; it provably cannot change the answer.

Correlating orders by common name showed what the 68 are made of:

| slice | count | interpretation |
| --- | --- | --- |
| future-dated, superseded by a later cert on the same CN | 24 | residue of a *successful* renewal |
| future-dated, newest cert for its CN | 31 | mostly healthy ACME certs mid-life |
| already expired, superseded by a later cert | 10 | residue of a successful renewal |
| already expired, newest cert for its CN | 3 | genuinely lapsed — the real signal |

The clearest single case is `lv-o-swdist02.its.maine.edu`. It carries two
orders: one placed 2026-07-16 (expiring 2026-08-15) and one placed
2026-08-05 — two days before measurement — expiring 2026-09-04. The
metric counts the **brand-new renewal**, because a 30-day certificate
sits inside a 30-day window from the moment it is issued. A trigger on
this metric fires on a certificate that renewed successfully two days
earlier.

## Considered options

- **Keep `last(...)>0`** — accept that it fires permanently in prod.
- **Add a lower bound** to the metric (`now < expiry <= cutoff`), so
  already-expired certs stop counting.
- **Make the threshold macro-driven** (`>{$CERTINEXT.ORDER.EXPIRING.MAX}`)
  and raise the macro during an observation period.
- **Ship the item with no trigger at all.**

## Decision outcome

Chosen: **ship the item data-only, with no trigger.** The count is a
graph/capacity metric. Nothing alerts on it.

This is explicitly a **deferral, not a permanent verdict.** The signal
actually wanted is narrower than this metric expresses: a certificate
someone obtained manually and then forgot to renew. ACME-originated
certificates are already monitored for expiry by other means, so they
are noise here — and they dominate the count. Segmenting orders by
`originator` (ACME vs. REST vs. portal) is the prerequisite for a
meaningful trigger, and is tracked as
[IDEA-005](../wishlist/IDEA-005-originator-alert-severity-bucketing.md).
The evidence supports that framing directly: all 3 genuinely-lapsed
certificates are `CERTInext API` originator, and none are ACME.

The lower-bound option was **rejected on evidence**: of the 68 counted
rows it would remove 13, of which 10 are renewal residue and 3 are the
only genuinely actionable rows in the entire metric. It deletes the
signal and keeps the noise. The metric's docstring already describes
including already-expired certs as deliberate, and that remains correct.

An observation period was rejected as circular — there is no threshold
for observation to discover, since 30 days and 90 days yield the same
number.

### Consequences

- Good: no trigger that fires permanently in prod. A template whose
  triggers are known-noisy trains people to ignore all of it, including
  the DCV triggers that do work.
- Good: the item still records history, so the baseline IDEA-005 needs
  accumulates from the day this ships rather than from the day the
  segmentation work starts.
- Bad: a genuinely lapsed certificate raises no alert from *this* metric
  until IDEA-005 lands. Partially mitigated today — `cm-unet1-ms.its.maine.edu`,
  the one non-test lapsed cert found, is `Order Accepted` with a
  certificate, so it is already surfaced by
  `certinext.orders.undownloaded`. That overlap is incidental, not a
  designed backstop, and does not cover a lapsed cert that *was*
  downloaded.
- Neutral: `--order-cert-expiry-days` keeps its 30-day default. With no
  trigger the value only shapes the graph, so there is no reason to
  change it before IDEA-005 revisits the metric's shape.

## Confirmation

Measured read-only against the production account on 2026-08-07 via
`certinext-zabbix-push --dry-run --order-health`, plus throwaway
correlation scripts over the same `/reports/orders` data. Prod read
`unissued=24 undownloaded=13 failed_recent=1 expiring=68
days_since_issued=0.12` at the time of the decision.

## More information

- [IDEA-005](../wishlist/IDEA-005-originator-alert-severity-bucketing.md)
  — originator segmentation, the prerequisite for triggering this metric
- [IDEA-004](../wishlist/IDEA-004-stuck-order-domain-visibility-lld.md)
  — per-domain LLD; the per-CN correlation used as evidence here is a
  manual version of what that idea automates
- [Zabbix trigger expression reference](https://www.zabbix.com/documentation/7.0/en/manual/config/triggers/expression)
- [`certinext` on PyPI](https://pypi.org/project/certinext/) — supplies
  `OrderRecord.certificate_expiry_date` and `originator`

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-08-07.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
