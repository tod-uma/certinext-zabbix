# CertiNext DCV by Zabbix trapper

## Overview

Monitors Domain Control Validation (DCV) health for domains managed through
[CertiNext](https://www.entrust.com/products/certinext) — the domain
inventory, verification state, and DCV expiry that gate certificate
issuance/renewal for InCommon and other CertiNext-backed CAs.

This template is trapper-only: it does not poll anything itself. All items
are populated by the `certinext-zabbix-push` CLI ([this repo's root
README](../../../README.md)), run on a schedule against the CertiNext API.
Items are duplicated per environment (`[prod]` / `[sandbox]` key parameters)
so a single Zabbix host can monitor both without collision.

## Requirements

- Zabbix 7.0 server.
- `certinext-zabbix-push` running on a host that can reach both the
  CertiNext API and this Zabbix server's trapper port (10051 by default).

## Setup

1. **Import** this file via *Data collection → Templates → Import*.
2. **Link** the template to the host that will receive the pusher's data
   (create one if you don't already have one for this purpose — commonly
   the same host running `certinext-zabbix-push`).
3. **Match the Host name** in Zabbix to what the pusher is sending to —
   `certinext-zabbix-push` targets a host by name via `--zabbix-host` /
   `ZABBIX_HOSTNAME`; it must equal the Zabbix host's configured *Host name*
   exactly.
4. **Set `{$CERTINEXT.SENDER.ALLOWED}`** (host- or template-level macro
   override) to the source IP(s) of the host(s) running
   `certinext-zabbix-push`. The committed value (`127.0.0.1`) rejects every
   real sender until you do this — see the macro table below.
5. **Schedule the pusher**: a frequent run (domain-list metrics; every
   15 minutes is typical) and a daily run with `--expiry-days` (DCV expiry
   metrics) and/or `--order-health` (order-health metrics). See the root
   repo's `docs/deployment.md` for systemd/cron examples. By default the
   pusher only monitors top-level domains (`--domain-scope top`) — see the
   note under [Metrics](#metrics-items) below.
6. **(Optional) mute sandbox notifications** — every item/trigger is tagged
   `env:prod` or `env:sandbox`. Add a condition to your alerting action
   (*Alerts → Actions*): `Tag value | env | does not equal | sandbox`.
   Sandbox problems still appear in the Zabbix UI; they just won't page.

## Required macros

| Macro | Default | Meaning |
|---|---|---|
| `{$CERTINEXT.SENDER.ALLOWED}` | `127.0.0.1` | **Must override.** Source IP(s) of the host(s) running `certinext-zabbix-push`. Trapper items reject any other sender. |
| `{$CERTINEXT.DCV.WARN_DAYS}` | `14` | WARNING severity when the soonest DCV expiry is within this many days. |
| `{$CERTINEXT.DCV.AVG_DAYS}` | `7` | AVERAGE severity threshold (days). |
| `{$CERTINEXT.DCV.HIGH_DAYS}` | `1` | HIGH severity threshold (days). |
| `{$CERTINEXT.DCV.DISASTER_DAYS}` | `0` | DISASTER severity — DCV already lapsed. |
| `{$CERTINEXT.NODATA.FAST}` | `1h` | `nodata()` window for the frequent (domain-list) items — four missed 15-minute runs alerts. |
| `{$CERTINEXT.NODATA.DAILY}` | `26h` | `nodata()` window for the daily expiry/order-health items — one missed daily run alerts. |
| `{$CERTINEXT.UNVERIFIED.MAX_AGE}` | `1h` | How long a domain may stay unverified before the "stuck" trigger fires. |
| `{$CERTINEXT.ORDER.STUCK_AGE}` | `24h` | How long an order may stay in a pending certificate status before the "stuck" trigger fires. |

## Metrics (items)

Each exists twice — `[prod]` and `[sandbox]` — via the Zabbix key parameter.

| Key | Type | Description |
|---|---|---|
| `certinext.domains.total[<env>]` | Unsigned | Total domains returned by the CertiNext domain list, after `--domain-scope` filtering. |
| `certinext.domains.unverified[<env>]` | Unsigned | Domains that are ACTIVE but not DCV-VERIFIED (PENDING/REJECTED), after `--domain-scope` filtering. |
| `certinext.dcv.expiring[<env>]` | Unsigned | Verified domains in scope whose DCV expires within the pusher's `--expiry-days` lead time. |
| `certinext.dcv.min_days_left[<env>]` | Float (days) | Days until the soonest DCV expiry across all verified domains in scope; negative once lapsed. |
| `certinext.orders.pending[<env>]` | Unsigned | Orders currently in a pending certificate status (pending-dcv/pending-organization-verification/pending-csr/pending-documents/pending-agreement/pending-approval). |
| `certinext.orders.failed_recent[<env>]` | Unsigned | Orders rejected, cancelled, expired, or revoked within the pusher's `--order-failing-lookback-days` window (default 30 days). |
| `certinext.orders.days_since_issued[<env>]` | Float (days) | Days since the most recent order with an "issued" certificate status, by `order_date`. No trigger yet — see [Triggers](#triggers). |

The domain-list metrics (`total`, `unverified`) are pushed every frequent
run; the expiry metrics (`expiring`, `min_days_left`) are pushed only by the
daily run invoked with `--expiry-days`; the order-health metrics (`pending`,
`failed_recent`, `days_since_issued`) are pushed only by the daily run
invoked with `--order-health`.

Order-health metrics deliberately avoid the vendor's free-text
`certificate_status` field for classification — only the server-side
status filter and `order_status` (the one field confirmed reliable against
live CertiNext data) decide what counts as pending/failed. See
`certinext_zabbix/zabbix_push.py`'s module docstring in the root repo for
the full rationale.

**`--domain-scope` gates all four metrics above**, not just the expiry
pair. The default, `top`, excludes any domain with a registered ancestor in
the account (e.g. skips `dept.example.edu` when `example.edu` is also
registered) — no DNS lookups. `ns-boundary` does the same but re-includes a
domain that has its own NS records (a real DNS zone cut). `all` restores
the pre-`--domain-scope` unfiltered behavior. Switching away from `all`
causes a one-time drop in `certinext.domains.total` the first time it
ships — expected, not a monitoring fault.

## Triggers

22 total (11 per environment):

| Trigger | Severity | Fires on |
|---|---|---|
| domain list is empty | WARNING | `total` = 0 — likely an API-side regression, not a real empty account. |
| domain(s) unverified too long | AVERAGE | `unverified` > 0 for the whole of `{$CERTINEXT.UNVERIFIED.MAX_AGE}`. |
| no data from pusher (frequent run) | WARNING | `nodata()` on `unverified` for `{$CERTINEXT.NODATA.FAST}` — prod only; ships DISABLED for sandbox until a sandbox frequent schedule exists. |
| no data from pusher (daily expiry run) | WARNING | `nodata()` on `expiring` for `{$CERTINEXT.NODATA.DAILY}` — prod only; ships DISABLED for sandbox until a sandbox daily schedule exists. |
| DCV lapsed | DISASTER | `min_days_left` ≤ `{$CERTINEXT.DCV.DISASTER_DAYS}`. |
| DCV expires within `{$CERTINEXT.DCV.HIGH_DAYS}` day(s) | HIGH | `min_days_left` ≤ `{$CERTINEXT.DCV.HIGH_DAYS}`; dependency-chained under DCV lapsed. |
| DCV expires within `{$CERTINEXT.DCV.AVG_DAYS}` days | AVERAGE | `min_days_left` ≤ `{$CERTINEXT.DCV.AVG_DAYS}`; dependency-chained under HIGH. |
| DCV expires within `{$CERTINEXT.DCV.WARN_DAYS}` days | WARNING | `min_days_left` ≤ `{$CERTINEXT.DCV.WARN_DAYS}`; dependency-chained under AVERAGE. |
| order(s) stuck too long | AVERAGE | `pending` > 0 for the whole of `{$CERTINEXT.ORDER.STUCK_AGE}`. |
| no data from pusher (daily order-health run) | WARNING | `nodata()` on `pending` for `{$CERTINEXT.NODATA.DAILY}` — prod only; ships DISABLED for sandbox until a sandbox daily schedule exists. |
| recent order failure(s) | WARNING | `failed_recent` > 0 (already date-filtered by the pusher, so no Zabbix-side window needed). |

The four DCV-expiry triggers are dependency-chained (most severe tier
suppresses the less severe ones below it), so exactly one fires per domain
state. `days_since_issued` has no trigger yet — see the note under
[Metrics](#metrics-items).

## Author

Tod Detre — [certinext-zabbix](https://github.com/tod-uma/certinext-zabbix)
