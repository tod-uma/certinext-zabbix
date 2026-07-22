# Windows deployment (Task Scheduler)

> **UNTESTED.** This directory is a starting reference for running
> `certinext-zabbix-push` on Windows via Task Scheduler, written by
> analogy with the systemd/cron examples — it has **not** been run
> against a real Windows host or reviewed by a Windows admin. Treat every
> default (service account choice, ACLs, task settings) as a draft to
> verify, and test in a non-production environment before relying on it.

## Contents

- [`certinext-zabbix.env.example`](certinext-zabbix.env.example) — the
  same `KEY=VALUE` credentials/config as the systemd/cron examples.
- [`Invoke-CertinextZabbixPush.ps1`](Invoke-CertinextZabbixPush.ps1) — a
  wrapper that loads the env file into the process environment, then
  calls `certinext-zabbix-push.exe`. Needed because Task Scheduler has no
  `EnvironmentFile=`-equivalent action type.
- [`Register-CertinextZabbixPushTasks.ps1`](Register-CertinextZabbixPushTasks.ps1) —
  registers the two Scheduled Tasks (frequent + daily), the Windows
  analogue of the systemd timer pairs.

## Install

```powershell
# 1. uv (see https://docs.astral.sh/uv/getting-started/installation/)
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Dedicated venv with a uv-managed Python
uv venv C:\certinext-zabbix\venv --python 3.12

# 3. Install the package
uv pip install --python C:\certinext-zabbix\venv\Scripts\python.exe `
  "git+https://github.com/tod-uma/certinext-zabbix"
```

This yields `C:\certinext-zabbix\venv\Scripts\certinext-zabbix-push.exe`.

## Service account

Run both tasks as the **same** dedicated, least-privileged account — a
group-managed service account (gMSA) if your domain supports one (no
password to manage), otherwise a regular service account with "Log on as
a batch job" rights. Same-account is not optional: the pusher's
single-instance lock lives under that account's `%TEMP%`
(`certinext_zabbix_push_<env>_<job>.lock`), and two different accounts
would each get their own temp directory, silently defeating the lock —
the Windows equivalent of the systemd-side `PrivateTmp=yes` warning.

## Secrets

Put credentials in an env file (see
[`certinext-zabbix.env.example`](certinext-zabbix.env.example)) at, e.g.,
`C:\ProgramData\certinext-zabbix\certinext-zabbix.env`, then lock its ACL
down to the service account and `SYSTEM` only:

```powershell
icacls "C:\ProgramData\certinext-zabbix\certinext-zabbix.env" `
  /inheritance:r /grant:r "SYSTEM:(R)" "DOMAIN\svc-certinext-zabbix:(R)"
```

This is a plaintext-file-with-ACL model, not a secrets vault — if your
environment has one (Credential Manager, a CI-integrated vault, DPAPI-
protected storage), prefer it and adapt
[`Invoke-CertinextZabbixPush.ps1`](Invoke-CertinextZabbixPush.ps1)
accordingly.

## Register the Scheduled Tasks

Run elevated (Administrator):

```powershell
.\Register-CertinextZabbixPushTasks.ps1 -ServiceAccount 'DOMAIN\svc-certinext-zabbix'
# or, for a gMSA:
.\Register-CertinextZabbixPushTasks.ps1 -ServiceAccount 'DOMAIN\svc-certinext-zabbix$' -AsGmsa
```

Creates:

- **CertiNext Zabbix Push** — every 15 minutes, indefinitely.
- **CertiNext Zabbix Push - Expiry** — daily at 03:12, with
  `--expiry-days 14`.

Re-run the script to update an existing registration; it unregisters
before re-registering.

## Caveat: no graceful stop

Unlike the systemd units (which send SIGTERM and let the script clean up,
exiting 130), Task Scheduler's "stop the task if it runs longer than…"
setting forcefully terminates the process — no signal handler runs, and
the file lock is left behind until the next run's zero-timeout acquire
fails once and self-clears. Set `ExecutionTimeLimit` generously rather
than relying on graceful shutdown, and validate this behavior in your own
environment.

## Validation

```powershell
# 1. Metrics computed correctly, nothing sent
& C:\certinext-zabbix\venv\Scripts\certinext-zabbix-push.exe --dry-run --expiry-days 14 -v

# 2. Trigger a real run and check Task Scheduler's history
Start-ScheduledTask -TaskName "CertiNext Zabbix Push"
Get-ScheduledTaskInfo -TaskName "CertiNext Zabbix Push"
```

In Zabbix, *Monitoring → Latest data* for the host should show fresh
values for `certinext.domains.total[prod]` and
`certinext.domains.unverified[prod]` — see the root
[`docs/deployment.md`](../../docs/deployment.md) for the one-time
Zabbix-side setup shared by every platform.
