---
status: accepted
date: 2026-07-22
---

# GitLab-canonical with a GitHub push mirror

## Context and problem statement

`certinext-zabbix` needs to be visible to external InCommon/higher-ed
CertiNext users, but this org's CI and issue workflow live on
`gitlab.its.maine.edu` — the University of Maine System's internal
GitLab instance, reachable only from inside UMS's network/VPN and **not
publicly accessible**, even though the project on it may be marked
"internal" (visible to any UMS GitLab user) rather than fully private.
Where should the repo actually live, and how does it become publicly
visible to people outside UMS?

## Considered options

- GitLab-canonical (an internal-visibility project on UMS's own,
  not-publicly-reachable GitLab instance; CI runs there) with a GitLab
  **push mirror** to a public GitHub repo.
- Make the `gitlab.its.maine.edu` project itself public — still requires
  someone to reach UMS's GitLab instance rather than a well-known public
  host, so this was not considered sufficient on its own.

## Decision outcome

Chosen: **GitLab-canonical + GitHub push mirror**, matching the existing
setup for the `certinext` library: `gitlab.its.maine.edu` (UMS-internal,
not publicly reachable) is the canonical repo where CI runs;
`github.com/tod-uma/certinext-zabbix` — a genuinely public, no-login-
required host — is the public face InCommon members see and clone from.
A GitLab push mirror works from an internal/private source project — the
GitLab repo need not itself be public for the GitHub mirror to be
public. The user configures the mirror once the repo's content is ready
(Phase 5 of the extraction plan).

### Consequences

- Good: consistent pattern with `certinext` — anyone maintaining one
  repo already knows how the other is set up.
- Good: CI, secrets, and issue triage stay on the org's existing GitLab
  instance; nothing GitHub-side needs org credentials.
- Neutral: pushes to `github.com` directly are never done — always push
  to the `gitlab` remote and let the mirror propagate. A commit made
  only on GitHub (e.g. via the GitHub web UI) would be silently
  overwritten by the next mirror sync.

## More information

- [GitLab repository push mirroring](https://docs.gitlab.com/ee/user/project/repository/mirror/push.html)
- [`certinext` on GitHub](https://github.com/tod-uma/certinext) — the mirror pattern being replicated.

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre on 2026-07-21/22.
> May contain inaccuracies or hallucinated details; verify specifics
> against current sources before relying on them.
