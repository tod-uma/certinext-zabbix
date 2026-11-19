# CertiNext DCV by Zabbix trapper

## Overview

Monitors Domain Control Validation (DCV) health and order health for
domains managed through
[CertiNext](https://www.entrust.com/products/certinext): the domain
inventory, verification state, DCV expiry, and the dns-persist TXT records
that gate certificate issuance for InCommon and other CertiNext-backed CAs.

Link the template to **one Zabbix host per CertiNext environment** (for
example `CertiNext prod` and, optionally, `CertiNext sandbox`) and set
`{$CERTINEXT.ENV}` on each. Most items are trappers, fed by the
`certinext-zabbix-push` CLI ([this repo's root README](../../../README.md)).
The exception is the dns-persist discovery: its item prototypes are
passive Zabbix agent checks (`net.dns.record`) of each apex's
`_validation-persist` TXT record, so each host needs an agent interface
pointing at the machine that runs the pusher.

Why one host per environment:
[ADR 0013](../../../docs/adr/0013-one-zabbix-host-per-certinext-environment.md).
Why dns-persist is checked in DNS rather than by its `validTill`:
[ADR 0014](../../../docs/adr/0014-dns-persist-checked-by-zabbix-agent.md).

Upgrading from 0.1.x (`[prod]`/`[sandbox]` keys)? Every item key changed;
follow the upgrade section of the root repo's
[docs/deployment.md](../../../docs/deployment.md#upgrading-from-01x).

## Requirements

- Zabbix 7.0 server, and Zabbix agent (or agent 2) on the pusher machine
  for the passive dns-persist checks. The `net.dns.record` behaviour this
  template relies on was verified on **agent 2** 7.0.31 only. With the
  classic agent, check it before relying on the alerts. From the Zabbix
  server or proxy, run `zabbix_get -s <pusher machine> -k agent.variant`
  (`1` = agent, `2` = agent 2). Then run
  `zabbix_get -s <pusher machine> -k 'net.dns.record[,_validation-persist.<a name that does not exist>,TXT]'`,
  and confirm that the error starts with `Cannot perform DNS query`. If it
  words that differently, a deleted record raises *lookup failing*
  (AVERAGE) instead of *record missing* (HIGH).
- `certinext-zabbix-push` 0.2.0 or later, on a machine that can reach the
  CertiNext API and this Zabbix server's (or proxy's) trapper port
  (10051 by default).
- From the pusher machine: outbound DNS (port 53) to
  `{$CERTINEXT.DNS.RESOLVER.1}` and `{$CERTINEXT.DNS.RESOLVER.2}`
  (1.1.1.1 and 8.8.8.8 by default), and inbound from the Zabbix server or
  proxy to the agent port (10050 by default).

## Setup

1. **Import** this file via *Data collection → Templates → Import*.
2. **Create one host per environment** you monitor. For each:
   - *Host name*: what that environment's pusher runs send as
     `--zabbix-host` / `ZABBIX_HOSTNAME`, exactly.
   - An **agent interface** with the pusher machine's IP or DNS name
     (passive checks ignore the host name, so both hosts can point at the
     same machine).
   - *Monitored by*: the same server or proxy as the trapper sends go to.
   - Link this template.
3. **Set the host macros** on each host:
   - `{$CERTINEXT.ENV}`: `prod` or `sandbox`. Defaults to `prod`, so a
     sandbox host left at the default raises the misroute trigger on its
     first run.
   - `{$CERTINEXT.SENDER.ALLOWED}`: the pusher machine's source IP(s). The
     committed `127.0.0.1` rejects every real sender.

   There is no macro for the expected `_validation-persist` value: the
   daily run sends it per apex from the CA's API (see
   [dns-persist discovery](#dns-persist-discovery)). Upgrading from
   0.2.0rc2 or earlier, delete any `{$CERTINEXT.PERSIST.EXPECTED}` host
   macro; nothing reads it.
4. **Schedule the pusher** for each environment: a frequent run (domain
   totals; the reference deployment uses every 6 hours), an hourly run
   with `--order-health`, and a daily run with `--expiry-days` (DCV expiry
   and the dns-persist apex list). See the root repo's
   `docs/deployment.md` for systemd, cron and Windows examples.
5. **(Optional) mute sandbox notifications**: every item and trigger is
   tagged `env:{$CERTINEXT.ENV}`. Add a condition to your alerting action
   (*Alerts → Actions*): `Tag value | env | does not equal | sandbox`.
   Sandbox problems still appear in the UI; they just don't page. See
   [ADR 0009](../../../docs/adr/0009-sandbox-suppressed-at-action-not-template.md).
6. **Disable unused `no data from pusher` triggers on the host** if an
   environment (typically sandbox) doesn't run all three schedules.

## Macros

| Macro | Default | Meaning |
| --- | --- | --- |
| `{$CERTINEXT.ENV}` | `prod` | **Set on each host.** The environment whose pusher runs target this host. Used in names and the `env` tag, and compared against the `certinext.env` value each run sends. |
| `{$CERTINEXT.SENDER.ALLOWED}` | `127.0.0.1` | **Must override.** Source IP(s) of the pusher machine. Trapper items and the discovery rule reject any other sender. |
| `{$CERTINEXT.DCV.WARN_DAYS}` | `3` | WARNING when the soonest **token-method** DCV expiry is within this many days. Sized for a ~10-day DCV window; for a CA with long windows (emSign's were about a year) raise `WARN_DAYS`/`AVG_DAYS` (the 0.1.0 defaults were `14`/`7`). DNS-PERSIST domains never reach these triggers. |
| `{$CERTINEXT.DCV.AVG_DAYS}` | `2` | AVERAGE threshold (days), token-method domains. |
| `{$CERTINEXT.DCV.HIGH_DAYS}` | `1` | HIGH threshold (days), token-method domains. |
| `{$CERTINEXT.DCV.DISASTER_DAYS}` | `0` | DISASTER: token-method DCV lapsed. |
| `{$CERTINEXT.DNS.RESOLVER.1}` | `1.1.1.1` | First public resolver for the "visible publicly" check. |
| `{$CERTINEXT.DNS.RESOLVER.2}` | `8.8.8.8` | Second public resolver. |
| `{$CERTINEXT.DNS.INTERVAL.LOCAL}` | `5m` | Lookup interval on the pusher machine's own resolver. "Record missing" needs 3 consecutive misses, so this sets its debounce (≈15 minutes). |
| `{$CERTINEXT.DNS.INTERVAL.EXTERNAL}` | `1h` | Lookup interval on the two public resolvers. |
| `{$CERTINEXT.DNS.NODATA}` | `3h` | `nodata()` window for the dns-persist lookups. Must exceed `DNS.INTERVAL.EXTERNAL`. |
| `{$CERTINEXT.NODATA.FAST}` | `13h` | `nodata()` window for the frequent (domain-list) items: one missed 6-hour run tolerated, two alert. |
| `{$CERTINEXT.NODATA.ORDER_HEALTH}` | `3h` | `nodata()` window for the hourly order-health items: one missed run tolerated, two alert. |
| `{$CERTINEXT.NODATA.DAILY}` | `26h` | `nodata()` window for the daily expiry items: one missed daily run alerts. |
| `{$CERTINEXT.UNVERIFIED.MAX_AGE}` | `7h` | How long a domain may stay unverified before the "stuck" trigger fires; just over one 6-hour cycle. |
| `{$CERTINEXT.ORDER.STUCK_AGE}` | `24h` | How long an order may sit awaiting issuance before its trigger fires. Assumes `--order-health` runs at least hourly ([#8](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/8)). |
| `{$CERTINEXT.ORDER.UNDOWNLOADED_AGE}` | `7d` | How long a generated certificate may sit undownloaded before its trigger fires. |
| `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}` | `4` | WARNING when no certificate has been issued for more than this many days. A plain number (the item is in days), not `4d`. Sized from 3 months of a busy production account (worst normal gap 2.09 days); raise it on a low-volume account such as a sandbox. |

## Metrics (items)

Trapper items, all pushed by `certinext-zabbix-push`:

| Key | Type | Pushed by | Description |
| --- | --- | --- | --- |
| `certinext.env` | Character | every run | The environment the run connected to (`prod` / `sandbox`). Misroute guard. |
| `certinext.domains.total` | Unsigned | frequent | Domain records in the list, after `--validated-by` and `--domain-scope`. |
| `certinext.domains.unverified` | Unsigned | frequent | ACTIVE records not DCV-VERIFIED (PENDING/REJECTED), same filtering. |
| `certinext.dcv.expiring` | Unsigned | daily | Verified **token-method** domains whose DCV expires within `--expiry-days`. |
| `certinext.dcv.token_domains` | Unsigned | daily | Verified token-method domains with a known expiry: the set `min_days_left` is computed over. Gates the DCV-expiry triggers. |
| `certinext.dcv.min_days_left` | Float (days) | daily | Days to the soonest token-method DCV expiry; negative once lapsed. Not sent when `token_domains` is 0. |
| `certinext.dcv.persist_min_days_left` | Float (days) | daily | Days to the soonest DNS-PERSIST `validTill`. A reuse clock, not a deadline: the CA re-checks DNS at the next issuance. Not sent without persist domains. |
| `certinext.dcv.persist_apexes` | Unsigned | daily | Number of apexes in the dns-persist discovery value; sent only with it. |
| `certinext.orders.unissued` | Unsigned | hourly | Orders accepted but not yet generated: still in approval, DCV, CSR, documents or agreement. |
| `certinext.orders.undownloaded` | Unsigned | hourly | Certificates generated but never downloaded: a delivery or automation failure. |
| `certinext.orders.failed_recent` | Unsigned | hourly | Orders in a terminal non-fulfilled state within `--order-failing-lookback-days` (default 30). Cancellations excluded ([ADR 0010](../../../docs/adr/0010-cancelled-orders-are-not-failures.md)). |
| `certinext.orders.expiring` | Unsigned | hourly | Issued certificates whose expiry falls within `--order-cert-expiry-days` (default 30), expired ones included. **No trigger** ([ADR 0008](../../../docs/adr/0008-orders-expiring-ships-data-only.md)). |
| `certinext.orders.days_since_issued` | Float (days) | hourly | Days since the CA last generated a certificate, by `order_date`. |

"Frequent" is a plain run, "daily" is `--expiry-days`, "hourly" is
`--order-health`.

**Token versus persist.** The daily run reads each scoped domain's DCV
method. `DNS-PERSIST` domains feed only the persist items and the
discovery rule; every other method (DNS-TXT, HTTP, email…) is a token
method and feeds `expiring`, `token_domains` and `min_days_left`. If any
method read fails, the run sends none of the daily items, so a partial
apex list never reaches the discovery rule.

**The CA's persist value.** The same method read returns each persist
domain's DNS-PERSIST record value, which the run puts in that apex's
discovery row as `{#PERSIST.EXPECTED}` (no extra API calls). It fails
closed per apex: if the CA returns no value, the apex's records disagree,
or the value can't sit literally in a trigger parameter (over 200
characters, `"`, `\`, `{`, `}`, or anything but printable ASCII), the row
carries
`NO-USABLE-DNS-PERSIST-VALUE-FROM-CA-API-SEE-PUSHER-LOG` instead, so that
apex alerts as "record missing". The run logs
`No usable dns-persist value from the CA — apex will alert` with the apex
and reason, and exits 1. Every other apex and the expiry items still go
out.

Neither order count is named "pending": the vendor's `status` enum uses
`pending-*` for the whole pre-fulfillment space, which spans `unissued`
**and** `undownloaded`. The split keys on whether
`certificate_expiry_date` is populated, a typed field, never the
free-text `certificate_status` display string.

**`--domain-scope`** gates the domain and DCV items, discovery included.
The default, `top`, excludes any domain with a registered ancestor in the
account (no DNS lookups), so discovery holds apexes. `ns-boundary`
re-includes a domain with its own NS records; `all` turns filtering off.

**`--validated-by CA`** picks one CA's domain records (CertiNext keeps one
per CA per name). It gates the domain and DCV items, not order health, and
runs before `--domain-scope`. Don't set it until the account holds records
for that CA, or `domains.total` reads 0 and the empty-list trigger fires.
See [ADR 0011](../../../docs/adr/0011-dcv-metrics-single-ca-by-flag.md).

## dns-persist discovery

Discovery rule `certinext.dcv.persist.discovery` (trapper, same allowed
hosts as the items) receives
`[{"{#APEX}": "example.edu", "{#PERSIST.EXPECTED}": "ssl.com; accounturi=…"}, …]`
from each daily run. `{#PERSIST.EXPECTED}` is the record value the CA's
API says to publish for that apex, unquoted (the triggers add the
quotes). When the CA changes it, the next daily run rewrites the
discovered triggers and every affected apex raises "record missing" until
DNS is republished. Lost apexes' items are deleted after **3 days** and are never
disabled first. Per apex it creates three passive agent items (text,
7 days' history):

| Key | Interval |
| --- | --- |
| `net.dns.record[,_validation-persist.{#APEX},TXT]` (the pusher machine's resolver) | `{$CERTINEXT.DNS.INTERVAL.LOCAL}` |
| `net.dns.record[{$CERTINEXT.DNS.RESOLVER.1},_validation-persist.{#APEX},TXT]` | `{$CERTINEXT.DNS.INTERVAL.EXTERNAL}` |
| `net.dns.record[{$CERTINEXT.DNS.RESOLVER.2},_validation-persist.{#APEX},TXT]` | `{$CERTINEXT.DNS.INTERVAL.EXTERNAL}` |

Each item maps the agent's `Cannot perform DNS query` error (no such
record) to an empty value, so a missing record reads as "no match". Any
other error, such as a resolver timeout, leaves the item unsupported,
which the "lookup failing" trigger catches. The two causes stay apart.
A local resolver that sometimes drops queries therefore shows up as gaps
in the local item, never as "record missing". `{$CERTINEXT.DNS.NODATA}`
(3h) is sized to absorb occasional blips.

## Triggers

16 static triggers per host, plus 3 trigger prototypes per discovered
apex.

| Trigger | Severity | Fires on |
| --- | --- | --- |
| pusher data from the wrong environment | HIGH | `certinext.env` ≠ `{$CERTINEXT.ENV}`: a run sent to the wrong host, or the host macro isn't set. |
| domain list is empty | WARNING | `total` = 0: likely an API regression, not a real empty account. |
| domain(s) unverified too long | AVERAGE | `unverified` > 0 for the whole of `{$CERTINEXT.UNVERIFIED.MAX_AGE}`. |
| no data from pusher (frequent run) | WARNING | `nodata()` on `unverified` for `{$CERTINEXT.NODATA.FAST}`. |
| no data from pusher (daily expiry run) | WARNING | `nodata()` on `expiring` for `{$CERTINEXT.NODATA.DAILY}`. Also covers discovery that stopped arriving (the pusher never sends daily items without it), but not discovery Zabbix failed to apply: check the discovery rule's error for that. |
| DCV lapsed | DISASTER | `min_days_left` ≤ `{$CERTINEXT.DCV.DISASTER_DAYS}` and `token_domains` > 0. |
| DCV expires within `{$CERTINEXT.DCV.HIGH_DAYS}` day(s) | HIGH | Same, ≤ `HIGH_DAYS`; depends on DCV lapsed. |
| DCV expires within `{$CERTINEXT.DCV.AVG_DAYS}` days | AVERAGE | Same, ≤ `AVG_DAYS`; depends on HIGH. |
| DCV expires within `{$CERTINEXT.DCV.WARN_DAYS}` days | WARNING | Same, ≤ `WARN_DAYS`; depends on AVERAGE. |
| dns-persist reuse period lapsed | INFORMATION | `persist_min_days_left` ≤ 0: the next issuance re-checks DNS. Informational; the DNS checks below are the real alert. |
| dns-persist apex count dropped | WARNING, manual close | `persist_apexes` below its maximum over the last 4 days (the rule's 3-day lifetime plus one daily run), so it opens before Zabbix deletes the dropped apexes' checks. A deliberate removal also raises it once. |
| order(s) stuck awaiting issuance | AVERAGE | `unissued` > 0 for the whole of `{$CERTINEXT.ORDER.STUCK_AGE}`. |
| no data from pusher (hourly order-health run) | WARNING | `nodata()` on `unissued` for `{$CERTINEXT.NODATA.ORDER_HEALTH}`. |
| certificate(s) generated but never downloaded | WARNING | `undownloaded` > 0 for the whole of `{$CERTINEXT.ORDER.UNDOWNLOADED_AGE}`. |
| recent order failure(s) | WARNING | `failed_recent` > 0 (already date-filtered by the pusher). |
| no certificate issued for over `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}` days | WARNING | `days_since_issued` > the macro. |

Per discovered apex:

| Trigger prototype | Severity | Fires on |
| --- | --- | --- |
| dns-persist record missing for `{#APEX}` | HIGH | At least 3 local lookups exist and none of the last 3 contains `"{#PERSIST.EXPECTED}"` (quotes included), the CA's value for that apex. |
| dns-persist record for `{#APEX}` not visible publicly | AVERAGE | Either public resolver has at least 2 lookups and its last 2 lack the value. Depends on "record missing", so a record gone everywhere raises only the HIGH. |
| dns-persist TXT lookup failing for `{#APEX}` | AVERAGE | `nodata()` on any of the three lookups for `{$CERTINEXT.DNS.NODATA}`: a resolver or the agent is failing, not the record. |

The four DCV-expiry triggers are dependency-chained, so only the most
severe tier fires. "Record missing" and "lookup failing" can both be open
at once: they have different causes.

> **Before enabling notifications:** both order-stuck triggers compare
> against zero, so they fire immediately against an account carrying a
> backlog of abandoned orders. Cancel those first.

`certinext.orders.expiring` has no trigger, deliberately: production
certificate lifetimes are bimodal (~30-day ACME and ~199-day), so no
threshold separates healthy mid-life certificates from lapsed ones. See
[ADR 0008](../../../docs/adr/0008-orders-expiring-ships-data-only.md); a
trigger becomes possible once the metric is segmented by originator
([IDEA-005](../../../docs/wishlist/IDEA-005-originator-alert-severity-bucketing.md)).

## Author

Tod Detre — [certinext-zabbix](https://github.com/tod-uma/certinext-zabbix)
