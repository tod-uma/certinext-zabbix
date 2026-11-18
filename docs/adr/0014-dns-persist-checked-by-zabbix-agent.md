---
status: accepted
date: 2026-11-18
---

# dns-persist records checked by the Zabbix agent; DCV alerts follow the DCV method

Supersedes [ADR 0012](0012-dcv-expiry-thresholds-sized-for-sslcom.md).

## Context and problem statement

ADR 0012 sized the DCV-expiry triggers (3/2/1/0 days on
`certinext.dcv.min_days_left`) on the assumption that SSL.com's roughly
10-day `validTill` was a deadline that would roll forward before it
lapsed. That assumption was wrong for dns-persist.

Under [draft-ietf-acme-dns-persist][dns-persist], the CA looks up the
`_validation-persist.<apex>` TXT record **at validation and issuance
time**. CertiNext's `validTill` is only a reuse clock: how long a past
check may be reused without looking again. So:

- ADR 0012's tiers fire on every ~10-day cycle as the clock runs down.
  Prod and sandbox WARNINGs had been open since 2026-11-18 11:30Z.
- The real dependency, the TXT record in public DNS, wasn't monitored at
  all. A deleted or edited record would surface only as a failed issuance.

Meanwhile some domains still use token methods (DNS-TXT, HTTP, email),
where `validTill` *is* a deadline and the gradient is the right alert.

Tod's requirements (2026-11-18): Zabbix does the DNS lookups, not the
pusher; each domain is alerted only for the DCV method it uses; keys stay
clear.

## Considered options

- **Keep ADR 0012's gradient for every domain.** Fires every cycle on
  dns-persist domains.
- **DNS lookups in the pusher**, sent as trapper values.
- **Zabbix agent lookups of each persist apex's TXT record**, discovered
  by LLD from the pusher, with alerts split by DCV method.

## Decision outcome

Chosen: **agent lookups through LLD, alerts by method** (Tod,
2026-11-18). Structural details are in
[ADR 0013](0013-one-zabbix-host-per-certinext-environment.md) (one host per
environment, which this design needed).

**The pusher splits domains by DCV method.** On the daily `--expiry-days`
run it reads each scoped domain's method with `Domain.get_dcv()`.
`DNS-PERSIST` domains go one way; everything else is a token domain.

- Token domains feed `certinext.dcv.expiring` and
  `certinext.dcv.min_days_left`, as before, plus a new
  `certinext.dcv.token_domains`: the count of verified token domains with
  a known expiry, the exact set `min_days_left` is computed over.
- The four gradient triggers keep 0012's 3/2/1/0 macros but add
  `and last(certinext.dcv.token_domains)>0`. With no token domains they
  can't fire, and a stale `min_days_left` (not sent when there are none)
  can't hold one open.
- Persist domains feed `certinext.dcv.persist_min_days_left`, with one
  **INFORMATION** trigger at `<=0`: "reuse period lapsed — next issuance
  re-checks DNS". Lapse-only, at INFORMATION severity, was Tod's choice.

**The agent checks the record.** The pusher sends
`certinext.dcv.persist.discovery` (`[{"{#APEX}": …}]`) and the template's
discovery rule creates, per apex, three passive agent items
([`net.dns.record`][agent-keys]) on `_validation-persist.{#APEX}` TXT:

| Lookup | Interval macro (default) | Trigger | Severity |
| --- | --- | --- | --- |
| pusher machine's resolver | `DNS.INTERVAL.LOCAL` (5m) | record missing: no match in the last 3 values | HIGH |
| `{$CERTINEXT.DNS.RESOLVER.1}` (1.1.1.1), `RESOLVER.2` (8.8.8.8) | `DNS.INTERVAL.EXTERNAL` (1h) | not visible publicly: no match in the last 2 values on either; depends on "record missing" | AVERAGE |
| all three | `DNS.NODATA` (3h) | lookup failing: `nodata()` on any of them | AVERAGE |

- A match is `find(…,"like","\"{$CERTINEXT.PERSIST.EXPECTED}\"")`:
  the exact value, inside its quotes, as agent2 returns it
  (`<name> TXT "<value>"`, verified on agent2 7.0.31). Each `find()` is
  gated on `count(…,#N)>=N`, so a newly discovered apex needs a full
  window (3 local or 2 public lookups) before it can alert;
  `find(…,#3)` alone evaluates on however many values exist.
- `{$CERTINEXT.PERSIST.EXPECTED}` defaults to `CONFIGURE-ME` and so
  **fails closed**: every apex alerts until the host macro holds the real
  value. The value lives only in host macros, because this repo mirrors
  publicly. It is **one value per host, with no per-apex override**.
  0.2.0rc1 used the context form `{$CERTINEXT.PERSIST.EXPECTED:{#APEX}}`,
  but an unquoted context ends at the first `}`, so Zabbix 7.0 parsed it
  as context `{#APEX` plus a stray `}`. LLD never substituted it, and
  every apex alerted on its first lookup (session 4, live). The value is
  per CertiNext account, the same for every apex in an environment, so
  the override was only ever wanted for the negative test.
- Preprocessing maps only `^Cannot perform DNS query` (agent2's error for a
  missing name) to an empty value, so "record missing" fires. Other errors,
  such as a resolver timeout, stay unsupported, so they surface as "lookup
  failing" instead. The two causes stay distinguishable.

**Discovery is never sent partial.** If any `get_dcv()` call fails, the
run withholds discovery and every expiry metric and exits 1 (`nodata()`
on `dcv.expiring` flags a failure that persists). The rule deletes lost
apexes' items after **3 days** and never disables them first, so a single
bad run costs nothing.

**A shrinking apex list is alerted.** Every run that sends discovery also
sends `certinext.dcv.persist_apexes`, the number of discovery rows. A
WARNING, closed manually, fires when it drops below its maximum over the
last **4 days** (`last()<max(…,4d)`): the rule's 3-day lifetime plus one
daily run, so the alert opens before Zabbix deletes the dropped apexes'
checks. This came out of the R1 external review (Codex), which noted that
discovery is only as complete as the CertiNext domain list. That list
raises on HTTP errors but would end quietly on a short page.

### Why LLD here, when ADR 0011 rejected it for the CA dimension

The set of persist apexes is data: it grows as institutions move to
dns-persist, and each apex needs its own *agent* check. Creating items
from data is what LLD exists for. ADR 0011's CA dimension was a small
fixed set on its way down to one, so static keys suited it. LLD's main
risk here, items deleted after a partial list, is handled by the
"never partial" rule, the 3-day lifetime and the apex-count alert.

### Consequences

- Good: the alert now tracks the real dependency. A persist record that
  is deleted, edited or unpublished pages within about 15 minutes, before
  an issuance fails.
- Good: the standing every-cycle WARNINGs on dns-persist domains stop,
  without losing the gradient for token-method domains.
- Good: no per-institution configuration. The method each domain uses
  decides which alerts apply to it.
- Bad: each host needs an agent interface on the pusher machine, and that
  machine needs outbound DNS (port 53) to the two public resolvers.
- Bad: the first discovery needs one daily run, and the dropped-apex
  alert needs history: it doesn't catch one apex removed and another added
  in the same run (the count stays the same).
- Bad: a deliberate removal of a persist apex also raises the
  dropped-count WARNING once; close it by hand.
- Neutral: `certinext.domains.unverified` isn't method-aware. Whether a
  persist domain's `dcvStatus` leaves VERIFIED after its reuse clock lapses
  is still to be observed (prod, after 2026-11-20 ~13:41Z); if it does,
  that metric needs the same split.
- Neutral: 0012's 3/2/1/0 defaults remain, now applying only to token
  domains. For a token CA with long DCV windows, raise `WARN_DAYS` /
  `AVG_DAYS` as 0012 already advised.

### Confirmation

- `tests/test_template.py` checks that every `KEY_*` the pusher sends
  exists in the template, that every `{#…}` macro under the rule is a
  column the pusher sends, and that every macro used is declared. It
  also rejects an LLD macro inside an unquoted user-macro context and
  any persist `find()` without its `count()` gate.
- Confirmed live (session 4, 0.2.0rc1): the import, the preprocessing
  export constants, and item and trigger counts after the first runs.
- Still to run, live (0.2.0rc2): that the expected value now matches
  (no "record missing" on any apex after a full window), and the
  negative tests: a wrong host-level `EXPECTED` on a host in
  maintenance raises HIGH on every apex after about 15 minutes; a dead
  `RESOLVER.1` raises "lookup failing" and not "record missing".

## Pros and cons of the options

### Keep ADR 0012's gradient for every domain

- Good, because nothing changes.
- Bad, because it fires every cycle on dns-persist domains and still
  watches nothing that can actually break issuance.

### DNS lookups in the pusher

- Bad, because it breaks the requirement that Zabbix does the lookups.

### Agent lookups through LLD, alerts by method

- Good, because it watches the record the CA actually checks, both from
  the local resolver and from two public ones.
- Bad, because of the agent interface, the DNS egress and the LLD
  lifecycle to reason about.

## More information

- [ADR 0011](0011-dcv-metrics-single-ca-by-flag.md): `--validated-by`,
  and the earlier LLD rejection discussed above.
- [ADR 0012](0012-dcv-expiry-thresholds-sized-for-sslcom.md): superseded.
- [ADR 0013](0013-one-zabbix-host-per-certinext-environment.md): one host
  per environment.
- Plan, including the R1 review triage:
  [docs/plans/env-hosts-dns-persist.md](../plans/env-hosts-dns-persist.md).
- [draft-ietf-acme-dns-persist][dns-persist].
- [Zabbix 7.0 agent item keys (`net.dns.record`)][agent-keys].
- [Zabbix 7.0 low-level discovery](https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery).
- [Zabbix 7.0 item value preprocessing](https://www.zabbix.com/documentation/7.0/en/manual/config/items/preprocessing)
  and the [item object API](https://www.zabbix.com/documentation/7.0/en/manual/api/reference/item/object)
  (check-for-not-supported parameters).
- [Zabbix 7.0 string functions (`find`)](https://www.zabbix.com/documentation/7.0/en/manual/appendix/functions/string)
  and [history functions (`nodata`)](https://www.zabbix.com/documentation/7.0/en/manual/appendix/functions/history).

[dns-persist]: https://datatracker.ietf.org/doc/draft-ietf-acme-dns-persist/
[agent-keys]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/zabbix_agent

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Haiku 5.5,
> `claude-haiku-5-5`) from the env-hosts plan and sessions with Tod Detre
> on 2026-11-18. May contain inaccuracies or hallucinated details; verify
> specifics against current sources before relying on them.
