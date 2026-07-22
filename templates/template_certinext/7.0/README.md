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
   metrics). See the root repo's `docs/deployment.md` for systemd/cron
   examples.
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
| `{$CERTINEXT.NODATA.DAILY}` | `26h` | `nodata()` window for the daily expiry items — one missed daily run alerts. |
| `{$CERTINEXT.UNVERIFIED.MAX_AGE}` | `1h` | How long a domain may stay unverified before the "stuck" trigger fires. |

## Metrics (items)

Each exists twice — `[prod]` and `[sandbox]` — via the Zabbix key parameter.

| Key | Type | Description |
|---|---|---|
| `certinext.domains.total[<env>]` | Unsigned | Total domains returned by the CertiNext domain list. |
| `certinext.domains.unverified[<env>]` | Unsigned | Domains that are ACTIVE but not DCV-VERIFIED (PENDING/REJECTED). |
| `certinext.dcv.expiring[<env>]` | Unsigned | Verified domains whose DCV expires within the pusher's `--expiry-days` lead time. |
| `certinext.dcv.min_days_left[<env>]` | Float (days) | Days until the soonest DCV expiry across all verified domains; negative once lapsed. |

The domain-list metrics (`total`, `unverified`) are pushed every frequent
run; the expiry metrics (`expiring`, `min_days_left`) are pushed only by the
daily run (the one invoked with `--expiry-days`).

## Triggers

16 total (8 per environment):

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

The four DCV-expiry triggers are dependency-chained (most severe tier
suppresses the less severe ones below it), so exactly one fires per domain
state.

## Author

Tod Detre — [certinext-zabbix](https://github.com/tod-uma/certinext-zabbix)
