# Wishlist

Ideas that came up during development but aren't being acted on yet — the
"not now" pile. Each one records why it was deferred and what would have to
change to make it worth picking up, so the reasoning isn't lost.

An idea graduates when someone commits to doing it: it becomes, or links to,
an ADR (see [../adr/](../adr/)).

## Status vocabulary

- **Proposed** — written down, not yet explored further.
- **Exploring** — actively being investigated.
- **Accepted → ADR NNNN** — committed to; see the linked ADR.
- **Rejected** — considered and turned down.
- **Superseded by IDEA-NNN** — replaced by a later idea.

## Ideas

| ID | Title | Status |
|----|-------|--------|
| [IDEA-001](IDEA-001-credential-file-input.md) | Read secrets from systemd `$CREDENTIALS_DIRECTORY` / `_FILE`-suffixed input | Proposed |
| [IDEA-002](IDEA-002-community-templates-submission.md) | Submit the template to the official Zabbix community-templates repository | Proposed |
| [IDEA-003](IDEA-003-days-since-issued-alert-threshold.md) | Add a severity trigger for days-since-last-issued once there's a baseline cadence | Proposed |
| [IDEA-004](IDEA-004-stuck-order-domain-visibility-lld.md) | Per-domain visibility into stuck orders via Zabbix LLD | Proposed |
| [IDEA-005](IDEA-005-originator-alert-severity-bucketing.md) | Bucket order-health alert severity by `originator` (ACME vs. manual) | Proposed |
