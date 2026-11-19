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

- A match is `find(…,"like","\"{#PERSIST.EXPECTED}\"")`: the exact
  value, inside its quotes, as agent2 returns it
  (`<name> TXT "<value>"`, verified on agent2 7.0.31). Each `find()` is
  gated on `count(…,#N)>=N`, so a newly discovered apex needs a full
  window (3 local or 2 public lookups) before it can alert;
  `find(…,#3)` alone evaluates on however many values exist.
- **The expected value comes from the CA, never from a person**
  (amended 2026-11-19, certinext-zabbix#12; see below). Up to 0.2.0rc2 it
  was a hand-set host macro, `{$CERTINEXT.PERSIST.EXPECTED}`. 0.2.0rc1
  used the context form `{$CERTINEXT.PERSIST.EXPECTED:{#APEX}}`, but an
  unquoted context ends at the first `}`, so Zabbix 7.0 parsed it as
  context `{#APEX` plus a stray `}`. LLD never substituted it, and every
  apex alerted on its first lookup (session 4, live). rc2 fell back to the
  plain host macro, one value per host.
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

**The pusher supplies the expected value per apex** (amended
2026-11-19, certinext-zabbix#12; Tod: nobody hand-sets it). The
`get_dcv()` call that reads each domain's method also returns its
DNS-PERSIST option's record value, so each discovery row carries it as
`{#PERSIST.EXPECTED}`, unquoted:
`[{"{#APEX}": "example.edu", "{#PERSIST.EXPECTED}": "ssl.com; …"}]`. No
extra API calls. `{$CERTINEXT.PERSIST.EXPECTED}` is gone, with no test
override.

- A CA-side change, such as a new account URI, now reaches Zabbix with
  the next daily run. LLD rewrites the discovered triggers, the stored
  TXT values stop matching, and "record missing" fires on every affected
  apex within a lookup or two. That is the right alert: DNS must be
  republished before the next issuance. A separate drift trigger would
  only repeat it.
- It is not the circular check this ADR rules out: the value comes from
  the CA's API, the record from DNS.
- **Per apex, fail closed.** Each apex uses its own value; nothing assumes
  the value is account-wide. When the CA gives an apex no value, its
  domain records disagree, or the value can't sit literally in the quoted
  `find()` parameter, the apex still gets its row, so its checks aren't
  deleted, but with the
  placeholder `NO-USABLE-DNS-PERSIST-VALUE-FROM-CA-API-SEE-PUSHER-LOG`.
  No record matches it, so the apex alerts as "record missing", and the
  trigger description names the placeholder. The pusher logs
  `No usable dns-persist value from the CA — apex will alert` per apex
  and exits 1. A method-read failure still withholds discovery entirely,
  as above.
- **What "can't sit literally" means** (Codex R3 review, 2026-11-19):
  over 200 characters (Zabbix keeps each function's parameters in a
  255-character column, and the quoting adds 16); `"` or `\` (would have
  to survive LLD escaping); `{` or `}` (Zabbix could read the value as a
  `{$MACRO}` and expand it at evaluation, matching something other than
  the CA's text); anything but printable ASCII (untested in Zabbix). Real
  values are 60-90 printable ASCII characters with none of these, so the
  guard costs nothing today; it exists so that an odd API answer alerts
  instead of silently checking the wrong thing.
- The value read never moves a domain to `failed`, so it can't hold back
  discovery or the expiry items. One pair of surrounding quotes is
  stripped; the API returns the value bare today.
- Considered and rejected (2026-11-19): the pusher setting the host macro
  through the Zabbix API (a new write-capable Zabbix credential on the
  pusher host); comparing two items inside `find()` (its pattern must be
  a constant or macro, not a function result); and the 2026-11-18 build
  of #12, which kept the hand-set macro and added a drift trigger against
  a `certinext.dcv.persist_expected` trapper. That build was never
  released and is replaced by this one.

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
  decides which alerts apply to it, and the CA's API decides the expected
  record value (#12).
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
- Confirmed live (2026-11-18, 0.2.0rc2; Ansible/tower-projects#4 note
  22186): all 82 rc1 false problems (41 "record missing", 41 "not visible
  publicly") closed, and none returned. Negative test: a wrong host-level
  `EXPECTED` on the sandbox host raised HIGH and AVERAGE on all 20
  apexes **within about a minute**, and restoring it closed all 40.
- The timing depends on the cause. A changed `EXPECTED` re-evaluates the
  values already stored, so every value in the window misses at once
  and the trigger fires on the next lookup. A record that really
  disappears still has older matching values in the window, so it
  alerts only after 3 consecutive misses, about 15 minutes. The
  `count()` gate holds back only newly discovered apexes. It is not
  what debounces a real miss.
- Still to run, live: the misroute test, and a dead `RESOLVER.1` that
  raises "lookup failing" and not "record missing".
- certinext-zabbix#12 (the pusher-supplied value above):
  `tests/test_template.py` checks that every persist `find()` pattern is
  exactly `"\"{#PERSIST.EXPECTED}\""` and that no
  `{$CERTINEXT.PERSIST.EXPECTED}` remains; the pusher tests cover the
  per-apex value, the placeholder for a missing, conflicting or unsafe
  value, and that every apex keeps its row. **Not yet proven live:** that
  Zabbix 7.0 substitutes an LLD macro inside a quoted `find()` parameter.
  rc1's break was a different construct (an LLD macro as an unquoted
  user-macro context), so it isn't disproven, only untested. Before
  release: import, one daily push, then read the discovered triggers back
  through the API (expression shows the CA's value; problems close). The
  negative test becomes a real record change in sandbox, since the macro
  knob is gone.

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
