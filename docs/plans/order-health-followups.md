---
status: planned
implements-adr: [0008, 0009]
---

# Order-health follow-ups

## Goal and context

The `--order-health` flag on `certinext-zabbix-push` adds five order metrics from
the CertiNext Orders Report. The metrics themselves are built and working on branch
`feat/order-health-metrics` (MR !14). What was unresolved was **trigger strategy** —
which of them should alert, and at what threshold.

That question was settled on 2026-08-07 by measuring the production account for the
first time. The decisions are recorded in
[ADR 0008](../adr/0008-orders-expiring-ships-data-only.md) and
[ADR 0009](../adr/0009-sandbox-suppressed-at-action-not-template.md). **This plan is
the implementation tail** — the template and code changes those ADRs imply, plus one
question they deliberately left open.

Read the two ADRs before starting. This document does not repeat their reasoning.

### The measurement that drove it

Read-only prod dry-run, 2026-08-07, full 163-row report:

```text
unissued=24 undownloaded=13 failed_recent=1 expiring=68 days_since_issued=0.12
```

Two facts matter for the work below:

- Prod certificate lifetimes are **bimodal** — 61 of 130 cert-bearing orders are
  ~30-day (overwhelmingly ACME), 62 are ~199-day. Nothing expires between 30 and 90
  days out, so `expiring` reads **68 at a 30-day threshold and 68 at 90 days**.
- All four order triggers would fire on import in prod today, not only the two
  already documented as doing so.

<details>
<summary>Why the prod measurement changed the plan</summary>

The prior hand-off assumed sandbox's saturated `expiring` metric was an artifact of
sandbox being a CI/CD playground, and that prod certificates were not short-lived.
Both assumptions were wrong. Prod has the same structure, which is what turned
"tune the threshold" into "no threshold can work here."

`lv-o-swdist02.its.maine.edu` is the clinching case: it carries two orders, the newer
placed 2026-08-05. The metric counted that **renewal two days after it succeeded**,
because a 30-day certificate is inside a 30-day window from the moment it is issued.
</details>

## Prerequisite: cut stable releases first

Tod's standing instruction (2026-08-07): promote these repos from alpha/rc to **real
stable releases before starting the next round of major changes.** Everything is
currently pre-release — `certinext-zabbix` 0.1.0rc8, `certinext` 1.2.0a7,
`ums-certinext-scripts` 0.4.0rc2, `nm` 1.2.0a1. `certinext` has never had a stable
tag; the `first-stable-release` skill covers that case.

Do this **before** step 1 below. Any `pyproject.toml` version bump needs `uv lock` in
the same commit, or `--locked` CI jobs go red immediately.

## Step 1 — Remove the `orders.expiring` triggers (implements ADR 0008)

Delete both `certinext.orders.expiring` triggers — `[prod]` and `[sandbox]` — from
`templates/template_certinext/7.0/template_certinext.yaml`. The **items stay**; only
the triggers go. ADR 0008 keeps the metric as a graph/capacity signal so the baseline
accumulates from now rather than from whenever IDEA-005 starts.

Also update:

- `docs/deployment.md` — it states the template creates "twenty-six triggers." That
  becomes twenty-four. Check the surrounding item/trigger inventory prose too.
- The item description in the template, so it says outright that this metric is not
  alerted on and why, pointing at ADR 0008. Someone will otherwise "fix" the missing
  trigger.

**Verification:** re-import the template into Zabbix and confirm it parses and that
no `orders.expiring` trigger appears. Grep the repo for tests asserting trigger or
item counts — `KEY_* constants ↔ template sync is manual, with no automated test and
silent failure at push time`, so counts asserted in prose or tests must be updated by
hand.

## Step 2 — Fix `Order In-Progress` misbucketing (bug)

In `certinext_zabbix/zabbix_push.py`, `bucket_orders()` classifies every unrecognized
non-empty `order_status` as **failed** via its `elif status:` catch-all.
`Order In-Progress` was observed in prod on 2026-08-07 and is caught by it.

That is wrong in two directions: it is an in-flight state, so it inflates
`failed_recent`, and it keeps a genuinely stuck order out of `unissued` where the
stuck-age trigger would find it.

The catch-all itself is deliberate and should stay — it exists so an unknown terminal
status surfaces rather than vanishing. Only this now-observed value needs promoting to
a recognized status, classified the same way `ORDER_STATUS_ACCEPTED` is: split on
whether `certificate_expiry_date` is populated, into `undownloaded` or `unissued`.

**Impact today is nil** — the single prod row falls outside the 30-day
`failed_recent` lookback, so current numbers are undistorted. Fix it before that
stops being true.

**Verification:** unit tests covering an `Order In-Progress` record both with and
without a certificate expiry date. Then a prod `--dry-run --order-health` and confirm
`failed_recent` drops to 0 and `unissued` rises by 1.

## Step 3 — Decide the `failed_recent` trigger shape (OPEN — not decided)

`certinext.orders.failed_recent` carries `last(...)>0` WARNING. This was **not**
settled on 2026-08-07 and needs a decision before order-health notifications are
enabled.

The concern: the prod report holds 8 `Order Cancelled` rows, and cancelling an order
is routine administrative work, not an incident. A `>0` trigger on a 30-day rolling
window of routine cancellations looks like the same wrong-shape problem ADR 0008
found in `expiring`, though it has not been analysed to the same depth.

It interacts with two other things, so decide it with them in view:

- Step 2 changes what lands in this bucket.
- `certinext` IDEA-011 (order cleanup CLI) will *deliberately* cancel orders in bulk.
  Cancelling old junk will not spike this metric — the count filters on `order_date`,
  not cancellation date — but cancelling recent stuck orders would.

Whatever is decided, record it: an ADR if a shape is chosen, a wishlist idea if it is
deferred again.

## Step 4 — Verify the sandbox action condition (implements ADR 0009)

No repository change. `docs/deployment.md` step 5 already prescribes the Zabbix action
condition *Tag value* | `env` | *does not equal* | `sandbox`, and ADR 0009 ratifies it
as a blanket rule covering the DCV severity ladder including the DISASTER trigger.

**Verification:** after the template is imported and the condition exists, confirm a
sandbox trigger entering PROBLEM state appears in the Zabbix problem view while
generating no notification. Note the ADR's recorded weakness — this enforcement lives
in Zabbix server config, outside version control, and nothing detects it being dropped
except a sandbox problem paging someone.

## Documentation expectations

- `docs/deployment.md` — trigger count, the order-health section, and the backlog
  prerequisite figures (already refreshed to 24 unissued / 13 undownloaded on
  2026-08-07).
- Template item descriptions — `orders.expiring` must say it is deliberately not
  alerted on.
- Docstrings in `zabbix_push.py` for any bucketing change in step 2.
- Annotated tag message for whatever release carries this, per house convention.

## Still-open dependencies

- **IDEA-005** (originator segmentation) is now a **prerequisite**, not an
  enhancement — ADR 0008 defers `expiring`'s trigger to it. Prod evidence suggests a
  three-way split (ACME / API / portal) rather than the two-way automated/manual
  sketch in the idea, because ACME expiry is already monitored elsewhere and the real
  uncovered risk is a manually-obtained certificate nobody renewed.
- **The prod abandoned-order backlog** (24 unissued, 13 undownloaded) still blocks
  enabling order-health notifications at all — see `docs/deployment.md`.
  `certinext` IDEA-011 proposes the cleanup CLI that would clear it repeatably.

## References

- [Zabbix 7.0 trigger expressions](https://www.zabbix.com/documentation/7.0/en/manual/config/triggers/expression)
- [Zabbix 7.0 action conditions](https://www.zabbix.com/documentation/7.0/en/manual/config/notifications/action/conditions)
- [Zabbix 7.0 template export/import format](https://www.zabbix.com/documentation/7.0/en/manual/xml_export_import/templates)
- [`certinext` on PyPI](https://pypi.org/project/certinext/) — supplies `OrderRecord`
- [`uv` documentation](https://docs.astral.sh/uv/) — `uv lock` on version bumps

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-08-07.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
