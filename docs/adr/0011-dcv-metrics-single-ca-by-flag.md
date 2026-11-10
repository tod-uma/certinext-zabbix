---
status: accepted
date: 2026-11-10
---

# DCV metrics measure one CA, selected by `--validated-by`

## Context and problem statement

CertiNext now holds one domain record **per CA per name**: since the
SSL.com migration began, an organization can have both an emSign record
and an SSL.com record for `example.edu`. The pusher lists every record, so
`certinext.domains.total`, `.unverified` and the DCV-expiry pair silently
mix two CAs.

That mix gets actively harmful after the emSign distrust (Chrome/Firefox
for certs issued from 2026-11-16, Apple 2026-11-30): emSign records will
lapse on purpose, which would fire the DCV DISASTER trigger. Meanwhile a
genuine SSL.com gap could be masked by a still-valid emSign record for
the same name. What should the DCV metrics measure while two CAs coexist?

## Decision drivers

- The metric that matters after 2026-11-16 is "can we still get SSL.com
  certificates", and nothing may hide a gap in it.
- emSign is going away within weeks, so anything built for it is
  short-lived.
- Keep the template stable: no new items, no new UUIDs, no re-import.

## Considered options

- **A `--validated-by` flag** that filters the domain list to one CA;
  item keys unchanged.
- **Per-CA item keys** (e.g. a CA dimension on every DCV item).
- **Deduplicate by name** across CAs.

## Decision outcome

Chosen: **a `--validated-by CA` flag (env `CERTINEXT_VALIDATED_BY`)**,
decided by Tod on 2026-11-10 (decision D2 in ums-certinext-scripts'
`docs/plans/sslcom-dcv-dns-persist.md`). Production runs with `SSLCOM`;
emSign DCV stops being monitored once the flag is set.

The flag is passed to `DomainAccessor.get_list(validated_by=...)` and
applied **before** `--domain-scope`. That ordering matters:
`Domain.dcv_covering_parent()` (used by `scope_domains` through
`filter_needs_dcv`) is org-scoped but not CA-aware, so on a mixed list an
SSL.com child would be dropped as "covered" by an emSign apex. Filtering
to one CA first leaves only that CA's names to cover each other.

### Consequences

- Good: no template change — item keys, triggers and UUIDs stay as they
  are.
- Good: the CA-blindness of `filter_needs_dcv` becomes harmless.
- Bad: one push measures one CA. Monitoring two CAs at once would need two
  hosts or per-CA keys. Accepted, because emSign is on its way out.
- Bad: switching CA changes what the existing `[env]` items mean, so their
  history shows a step change at the switch, the same kind ADR 0007
  accepted for `--domain-scope`.
- Neutral: default unset = every CA, the pre-flag behaviour, so existing
  deployments are unaffected until they opt in. An empty value is treated
  as unset, because filtering on `""` would drop every record and trip the
  empty-list trigger.
- Neutral: order-health metrics are not filtered; orders aren't segmented
  by CA in this tool.
- Neutral: locks are unchanged (per env and per job), so an unfiltered and
  a filtered push of the same job on one host would contend for one lock.
  Production runs one configuration, so this doesn't arise.
- Rollout constraint: set `SSLCOM` in prod only once prod actually holds
  SSL.com records, or `domains.total[prod]` reads 0 and the empty-list
  guard fires.

### Confirmation

Unit tests pin the flag, the env var, upper-casing, empty-means-unset and
the no-kwarg call when unset. They also pin that order health is not
filtered, and that the CA is bound into the log context (`validated_by=`
on every line, `ALL` when unset). `scope_domains` tests show the mixed-CA
hazard and that a CA-filtered list scopes correctly.

## Pros and cons of the options

### `--validated-by` flag

- Good, because it needs no template change and drops cleanly when emSign
  is gone.
- Bad, because it covers only one CA per push.

### Per-CA item keys

- Good, because both CAs would be visible at once.
- Bad, because it adds a dimension to the template: duplicated items or
  LLD, new UUIDs and a re-import, all for a CA that disappears within
  weeks.

### Deduplicate by name

- Bad, because a still-valid emSign record would hide an SSL.com gap, which
  is exactly the failure that matters after 2026-11-16.

## More information

- [ADR 0007](0007-domain-scope-reuses-filter-needs-dcv.md): `--domain-scope`,
  whose `filter_needs_dcv` coverage check this flag must run ahead of.
- [certinext on GitHub](https://github.com/tod-uma/certinext):
  `DomainAccessor.get_list(validated_by=)`, first released in certinext
  1.4.0rc5 (this repo's dependency floor).
- [Zabbix 7.0 trapper items](https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/trapper)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Haiku 5.5,
> `claude-haiku-5-5`) from a conversation with Tod Detre on 2026-11-10.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
