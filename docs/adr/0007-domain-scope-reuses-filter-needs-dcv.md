---
status: accepted
date: 2026-07-23
---

# `--domain-scope` reuses `certinext.filter_needs_dcv`, not a new PSL-based filter

## Context and problem statement

A 2026-07-23 incident showed `certinext-zabbix-push`'s all-or-nothing
expiry check (deliberately "skip rather than undercount") can black out
prod DCV monitoring from a single flaky domain's detail refresh — and
separately, monitoring roughly 111 individual hostnames per account is
both the wrong signal (top-level domain health, not every hostname) and
more surface area for exactly this failure mode. A `--domain-scope`
option (`top` / `ns-boundary` / `all`) addresses both: fewer domains
scoped in means fewer per-domain API calls, and it's the actual
monitoring signal wanted. How should the three scope modes decide which
domains to keep?

## Considered options

- Reuse `certinext.filter_needs_dcv` / `Domain.dcv_covering_parent`
  (already-shipped account-hierarchy filtering in the `certinext` SDK)
- A new domain-parsing implementation using `tldextract` or a public
  suffix list (PSL), to derive "top-level" from real public-suffix rules

## Decision outcome

Chosen: **reuse `certinext.filter_needs_dcv`**. It already implements
exactly the two DNS-adjacent semantics needed:

- `check_ns=False` excludes a domain if any registered ancestor exists in
  the account's domain list (pure string-suffix match against
  `all_domain_names`, no DNS) — this is the `top` mode (default).
- `check_ns=True` does the same but re-includes a domain that has its own
  NS records (a real DNS zone cut) even when a registered ancestor
  exists — this is `ns-boundary`, identical logic to what
  `certinext-top-domains`/`dcv-update` (in `ums-certinext-scripts`) already
  rely on.
- `all` mode skips the filter entirely — today's unfiltered behavior.

This is account-hierarchy filtering (does a *registered* ancestor cover
this domain?), not real public-suffix parsing (is `example.co.uk` a
second-level domain?) — a PSL dependency would answer a different
question than the one being asked here.

### Consequences

- Good: no new dependency for PSL/`tldextract` parsing; `dnspython` is
  the only addition (already required for `check_ns=True`'s NS lookups,
  and promoted to a **hard** dependency here — not optional — so
  `ns-boundary` can't silently degrade into `top` when the extra is
  missing).
- Good: one account-hierarchy filter shared with
  `certinext-top-domains`/`dcv-update`, instead of a second
  implementation that could drift out of sync with it.
- Neutral: `--domain-scope` applies to all four pushed metrics (total,
  unverified, expiring, min-days-left), not just the expiry pair —
  domains are filtered once, immediately after `sess.domain.get_list()`,
  before any metric collection. Switching the default from `all` to
  `top` causes a one-time step-change drop in `certinext.domains.total`
  the first time it ships — an expected graph change, not a monitoring
  bug (documented in the template item descriptions and per-template
  README).
- Bad: `top`'s semantics depend on the account actually having a
  registered apex domain (e.g. the org's primary domain) in
  `sess.domain.get_list()` — unverified before this ADR was written
  against a live account; must be confirmed (`certinext-top-domains -v`
  or a `--domain-scope top --dry-run` run against real prod credentials)
  before relying on `top` in production.

## More information

- [`certinext` on PyPI](https://pypi.org/project/certinext/) —
  `filter_needs_dcv`/`Domain.dcv_covering_parent` live here
- [`dnspython` on PyPI](https://pypi.org/project/dnspython/) — required
  for `check_ns=True` NS lookups
- `docs/plans/certinext-zabbix-retry-and-domain-scope.md` in the
  `sysadmin-ansible` superproject — a UMS-internal repository, not
  publicly accessible — carries the fuller incident/planning context
  this ADR was extracted from.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-23.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
