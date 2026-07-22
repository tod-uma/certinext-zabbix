# IDEA-001: Read secrets from systemd `$CREDENTIALS_DIRECTORY` / `_FILE`-suffixed input

- **Status:** Proposed
- **Created:** 2026-07-22
- **Updated:** 2026-07-22
- **Tracking issue:** sysadmin/certinext-zabbix#1
- **Companion:** originated as a broader idea (also covering `dcv-update`
  and NM credentials) in `ums-certinext-scripts`'s
  `docs/wishlist/IDEA-001-credential-file-input.md` — a University of
  Maine System (UMS) internal repository, **not publicly accessible**.
  This entry is the certinext-zabbix-specific slice of that idea:
  `certinext-zabbix-push` has no NM dependency, so only the CertiNext
  credentials apply here.

## Context

`certinext-zabbix-push` currently reads its two secrets
(`CERTINEXT_CLIENT_ID`, `CERTINEXT_CLIENT_SECRET`) only from plain
environment variables — set via a systemd `EnvironmentFile=`, a
`/etc/cron.d` file, or (on Windows) a wrapper script that loads a
`KEY=VALUE` file into the process environment (see
`examples/windows/Invoke-CertinextZabbixPush.ps1`). None of these
prevent the values from appearing in `/proc/PID/environ` (Linux) or
being inherited by any child process, and the Windows wrapper's approach
is a plaintext-file-with-ACL model rather than anything OS-credential-
store-backed.

systemd's Credentials mechanism (`LoadCredential=` /
`LoadCredentialEncrypted=`) exists specifically to close this gap on
Linux — but only if the CLI itself knows how to read a secret from a
file, not just from a plain env var.

## The idea

Add file-based secret input to `certinext-zabbix-push` so
`CERTINEXT_CLIENT_ID` and `CERTINEXT_CLIENT_SECRET` can each be supplied
either as the current plain env var, or as a `<NAME>_FILE` variable
pointing at a file to read the value from — the common Docker/Kubernetes
secrets convention. That would let a systemd deployment switch to
`LoadCredential=` (pointing `<NAME>_FILE` at
`$CREDENTIALS_DIRECTORY/<name>`), and could give the Windows deployment
path a way to source secrets from something other than a flat env file
too (e.g. a file whose ACL is the only thing protecting it today).

## Why not now

- No known deployment host currently has a TPM, so
  `LoadCredentialEncrypted=`'s strongest guarantee has no urgency yet.
- Plain `EnvironmentFile=` at `0600` root-only (or the equivalent ACL-
  restricted file on Windows) already keeps the service account itself
  from being the weak point; the marginal benefit today is narrow — this
  codebase makes no subprocess/`Popen` calls, so there's no child
  process to leak the environment to.
- Adding a second input path per secret is real surface area (docs,
  precedence rules, tests) for a benefit that isn't needed until someone
  actually deploys onto a host that wants `LoadCredentialEncrypted=`, or
  otherwise asks for it.

**What would change this:** a deployment host gets a TPM and wants
`LoadCredentialEncrypted=`, or a user asks for a proper secrets-store
integration on any platform.

## Pros

- `_FILE`-suffix convention is a well-known pattern (Docker/Kubernetes
  secrets), not a bespoke one.
- Unlocks stronger secret handling on systemd deployments without
  breaking existing plain-env-var deployments.
- Could reduce the Windows deployment's reliance on a flat, ACL-only
  secrets file.

## Cons / costs

- New input path per secret to implement, document, and test.
- Need a clear precedence rule when both the plain var and the `_FILE`
  variant are set.
- Must update `docs/deployment.md` (the contract examples/systemd,
  examples/cron, and examples/windows are all generated from) for the
  two affected variables.

## Effort

Small: only two secrets are involved (`CERTINEXT_CLIENT_ID`,
`CERTINEXT_CLIENT_SECRET`), and the read-a-secret logic is likely a
handful of lines in `certinext_zabbix/_cli_shared.py` or
`zabbix_push_cli.py`, plus `docs/deployment.md` and tests.

## Open questions & caveats

- Precedence when both `CERTINEXT_CLIENT_SECRET` and
  `CERTINEXT_CLIENT_SECRET_FILE` are set — error, or one wins silently?
- Whether this belongs here or upstream in `certinext.cli_options` /
  `certinext.cli_support`, since those aliases are the public surface
  this CLI's connection flags already come from (see AGENTS.md) — adding
  it there would benefit every `certinext`-based CLI, not just this one.

## Next steps

- Tracked in sysadmin/certinext-zabbix#1.
- Consider raising it with the `certinext` library first (see the open
  question above) before implementing it locally.

## References

- [systemd.io — Credentials](https://systemd.io/CREDENTIALS/)
- [systemd.exec(5) — LoadCredential=, LoadCredentialEncrypted=](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials)

---
> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Sonnet 5,
> `claude-sonnet-5`) from a conversation with Tod Detre. May contain
> inaccuracies or hallucinated details; verify specifics against current
> sources before relying on them.
