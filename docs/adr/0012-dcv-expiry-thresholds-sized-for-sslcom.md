---
status: superseded by 0014
date: 2026-11-10
---

> **Superseded by [ADR 0014](0014-dns-persist-checked-by-zabbix-agent.md)
> (2026-11-18).** For dns-persist, `validTill` turned out to be a reuse
> clock, not a deadline. The 3/2/1/0 defaults remain, now for token-method
> domains only.

# DCV-expiry thresholds default to 3/2/1/0 days, sized for SSL.com

## Context and problem statement

The DCV-expiry triggers on `certinext.dcv.min_days_left` escalate through
four macros: `{$CERTINEXT.DCV.WARN_DAYS}`, `AVG_DAYS`, `HIGH_DAYS` and
`DISASTER_DAYS`, which defaulted to 14/7/1/0. Those values were chosen
for emSign DCV, which stayed valid for roughly a year.

SSL.com dns-persist DCV behaves differently. A record's `validTill` is
about **10 days** after verification, measured in the sandbox and in
production (a production apex re-created 2026-11-10 17:11Z showed
`dcv_expires` 2026-11-20 17:11Z). It is expected to renew itself while
the static persist TXT stays published. With a 10-day window,
`min_days_left` is always under 14 and usually under 7, so the WARNING
and AVERAGE tiers would sit in PROBLEM permanently. The sandbox showed
exactly that: both tiers were open on 2026-11-10 with 5.4 days left.

The requirement (Tod, 2026-11-10): no alerts the moment SSL.com
monitoring is switched on (`--validated-by SSLCOM`, ADR 0011).

## Considered options

- **Keep 14/7/1/0.** No change. WARNING and AVERAGE fire continuously
  for every SSL.com record.
- **Wait for the first observed SSL.com auto-renewal** (sandbox, after
  2026-11-15T20:43Z) to learn *when* in the window renewal happens, then
  size the thresholds from that.
- **3/2/1/0 now**, as template defaults and as host-level overrides on
  the production host.

## Decision outcome

Chosen: **3/2/1/0 now** (Tod, 2026-11-10). Waiting would have left the
production host alerting from its next daily expiry run, because the
deployed pusher already counted the SSL.com records, and that's exactly
what the requirement ruled out. The tiers now fire only when a record
gets within 3 days of lapsing, which shouldn't happen if renewal works.
So an alert means renewal *didn't* roll the record forward, and that's
the failure worth catching, since SSL.com DCV is published on demand,
with no timer re-verifying it.

The values live in two places on purpose. The production host carries
them as host macros, because the problem was immediate. The template
defaults carry the same values, so a fresh import or another deployment
monitoring SSL.com doesn't inherit thresholds that can never be quiet.

### Consequences

- Good: no standing WARNING/AVERAGE noise on SSL.com records. A DCV
  problem now means "close to lapsing without renewing".
- Bad: the thresholds were set before any renewal had been observed.
  If SSL.com renews late in the window, say within the last day, HIGH
  could fire on every cycle. The first sandbox renewal (after
  2026-11-15T20:43Z) checks this. If renewal timing contradicts the
  thresholds, revisit them with a new ADR.
- Bad: only 3 days' warning before a lapse. Accepted, because the record
  should renew long before that, and re-publishing the DCV record is
  quick.
- Neutral: a deployment monitoring a CA with long DCV windows (emSign's
  were about a year) should raise `WARN_DAYS`/`AVG_DAYS` back toward
  14/7. The template README says so.
- Neutral: macro values have no UUIDs, so the template still re-imports
  cleanly. Host-level macros override template values, so the production
  host keeps 3/2/1/0 even if the template defaults change later.

### Confirmation

- The production host's macros read 3/2/1/0 via the Zabbix API
  (2026-11-10), with `{$CERTINEXT.SENDER.ALLOWED}` unchanged.
- Production dry-run with `--validated-by sslcom` (2026-11-10 20:40Z):
  `min_days_left` 9.71, above the 3-day WARNING, so switching on
  SSL.com monitoring opens no problem.

## More information

- [ADR 0011](0011-dcv-metrics-single-ca-by-flag.md): `--validated-by`,
  which makes the DCV metrics measure SSL.com only.
- [Zabbix 7.0 user macros](https://www.zabbix.com/documentation/7.0/en/manual/config/macros/user_macros):
  host-level macros override template-level ones.
- [draft-ietf-acme-dns-persist](https://datatracker.ietf.org/doc/draft-ietf-acme-dns-persist/):
  the persistent DNS TXT validation method SSL.com's DCV uses.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5.5,
> `claude-sonnet-5-5`) from a conversation with Tod Detre on 2026-11-10.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
