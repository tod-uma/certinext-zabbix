# Deploying `certinext-zabbix-push` (systemd timer or cron)

How to install and schedule `certinext-zabbix-push` on a Linux server so it
pushes CertiNext DCV health metrics to Zabbix on a recurring schedule.
Written to be sufficient on its own for a human operator **or** a
configuration-management agent (Ansible, etc.) — every path, command,
credential, and network requirement a playbook needs is stated explicitly.
A task checklist for automation is at the
[bottom](#configuration-management-checklist).

## How the script behaves as a scheduled job

`certinext-zabbix-push` pushes CertiNext DCV health metrics to Zabbix as
**trapper item values** — Zabbix polls nothing; a dead pusher is caught by
`nodata()` triggers on the Zabbix side (see
[Nodata windows and cadence](#nodata-windows-and-cadence)).

- **Single-instance lock built in**, per environment and per job: a file
  lock at `$TMPDIR/certinext_zabbix_push_<env>_<job>.lock` (normally
  `/tmp/certinext_zabbix_push_prod_plain.lock` /
  `..._prod_expiry.lock`, and the `sandbox` equivalents if you run
  sandbox monitoring — see [below](#optional-monitoring-the-sandbox-too)).
  If a run is already in progress, the new instance logs one line and
  exits **0**. No external `flock` wrapper is needed — but see the
  `PrivateTmp` warning below.
- **Exit codes:** `0` success (including the lock-skip case), `1` one or
  more errors occurred, `130` interrupted (SIGINT or SIGTERM — the script
  traps SIGTERM, releases the lock, and logs the interruption).
- **Logging:** everything goes to **stderr**; **stdout carries one JSON
  object** with the pushed metrics
  (`{"env": ..., "zabbix_host": ..., "sent": ..., "metrics": {...}}`).
  When stderr is not a TTY (systemd, cron), log output is one `key=value`
  (logfmt) line per event with UTC ISO timestamps and a per-run
  `correlation_id` + `pid` on every line — ready for journald or a log
  aggregator (Splunk and friends auto-extract `key=value` pairs with no
  per-sourcetype configuration, unlike JSON, which only gets that treatment
  if the *entire* line is valid JSON). Pass `--log-format json` to restore
  the old one-JSON-object-per-line format instead.
- **Credentials are fail-fast:** a missing `CERTINEXT_CLIENT_ID`/
  `CERTINEXT_CLIENT_SECRET` or `--zabbix-server`/`ZABBIX_SERVER` raises an
  error and exits 1 immediately.
- **Transport retries:** transient Zabbix trapper failures (connection
  refused/reset, timeout) are retried in-process — 3 tries, 5 s apart —
  before the run fails, so a brief network blip doesn't age into a false
  `nodata()` alert. Server-side rejections (`failed > 0`: wrong host name,
  unlinked template, sender IP not in `{$CERTINEXT.SENDER.ALLOWED}`) are
  configuration errors and are **not** retried.

## Prerequisites

1. **CertiNext OAuth API credentials**: account number (client ID) + client
   secret.
2. **A Zabbix 7.0 server** reachable on its trapper port (10051 by
   default) from the host that will run the pusher.
3. **Network access** per the [table below](#network-requirements).
4. `uv` on the installing host (the playbook can install it; see below).
   Python ≥ 3.11 is required but `uv` can download a managed interpreter
   itself — the OS Python version does not matter.

## Install

Recommended layout: a dedicated virtualenv at a fixed path, so unit files
reference stable absolute paths and no user-level `PATH` setup is
involved.

```bash
# 1. uv (skip if already present; also available as a distro/EPEL package)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Dedicated venv with a uv-managed Python
uv venv /opt/certinext-zabbix --python 3.12

# 3. Install the package (public PyPI once published — see CLAUDE.md;
#    until then, install directly from the GitHub mirror).
uv pip install --python /opt/certinext-zabbix/bin/python \
  "git+https://github.com/tod-uma/certinext-zabbix"
```

This yields `/opt/certinext-zabbix/bin/certinext-zabbix-push`.

**Upgrades:** re-run step 3 pinned to the new version (once published to
PyPI, `certinext-zabbix==X.Y.Z`). No service restart is needed
(`Type=oneshot` — the next timer firing uses the new code).

## Service user

A dedicated unprivileged system user; no shell, no special group
memberships. The service writes nothing except its `/tmp` lock file.

```bash
useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent --no-create-home certinextzbx
```

## Configuration

All runtime configuration is environment variables (plus optional CLI
flags in the unit file). Put secrets in an environment file readable by
root only — systemd reads `EnvironmentFile=` as root before dropping to
`User=`:

```bash
# /etc/certinext-zabbix/certinext-zabbix.env   (mode 0600, owner root:root)
CERTINEXT_CLIENT_ID=<account number>
CERTINEXT_CLIENT_SECRET=<client secret>
ZABBIX_SERVER=<your zabbix server address>
ZABBIX_HOSTNAME=<host name exactly as registered in Zabbix>

# Only if this host lacks direct HTTPS egress to the CertiNext API:
#HTTPS_PROXY=http://proxy.example.org:3128
#NO_PROXY=127.0.0.1,localhost
```

### Environment variable reference

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `CERTINEXT_CLIENT_ID` | yes | — | CertiNext account number / OAuth2 client_id |
| `CERTINEXT_CLIENT_SECRET` | yes | — | CertiNext OAuth2 client secret |
| `ZABBIX_SERVER` | yes | — | Zabbix server (trapper) address. No built-in default — an unset value fails fast rather than silently targeting the wrong server. |
| `ZABBIX_PORT` | no | `10051` | Zabbix trapper port |
| `ZABBIX_HOSTNAME` | no | this machine's FQDN | Host name exactly as registered in Zabbix — **set explicitly in production**; the FQDN fallback depends on `/etc/hosts`/reverse DNS and logs a warning when it looks unusable |
| `ZABBIX_TIMEOUT` | no | `10` | Socket timeout (seconds) for the trapper send |
| `HTTPS_PROXY` / `NO_PROXY` | no | — | Standard proxy vars, honored for the CertiNext HTTPS calls (httpx) |

### CLI flag reference

| Flag | Meaning |
|---|---|
| `--dry-run` | Compute and print the metrics without sending anything to Zabbix. Use for validation. |
| `--expiry-days DAYS` | Also push the DCV-expiry metrics (one extra API call per verified domain — schedule this on a daily run, not every 15 minutes). Disabled by default. |
| `--zabbix-server` / `--zabbix-port` / `--zabbix-host` / `--zabbix-timeout` | Override the matching environment variables above. |
| `--sandbox` | Use the CertiNext **sandbox** API — see [Optional: monitoring the sandbox too](#optional-monitoring-the-sandbox-too). |
| `-v` / `-vvv` / `-vvvv` | Verbosity: config details / script debug / third-party debug. Not needed in production; logs are complete at default verbosity. |
| `--log-format json` | Emit one JSON object per line instead of the default logfmt (`key=value`) lines. |

Full flag list: `/opt/certinext-zabbix/bin/certinext-zabbix-push --help`.

## One-time Zabbix server setup (not a playbook task)

Preconditions on the Zabbix server, done once by an operator in the UI —
the playbook only deploys the sending side. See the
[per-template README](../templates/template_certinext/7.0/README.md) for
the full macro/metric/trigger reference; summarized here for the
deployment sequence:

1. Import
   [templates/template_certinext/7.0/template_certinext.yaml](../templates/template_certinext/7.0/template_certinext.yaml)
   via *Data collection → Templates → Import*. It creates **CertiNext DCV
   by Zabbix trapper**: eight trapper items (each metric twice, key-
   parameterized `[prod]` / `[sandbox]`), sixteen triggers (including a
   per-environment DCV-expiry severity gradient — WARNING → AVERAGE →
   HIGH → DISASTER, dependency-chained so only the deepest tier alerts),
   and the macros. The two sandbox `nodata()` triggers ship **disabled**
   — enable them only once sandbox runs are scheduled.
2. **Link** the template to the sending host's Zabbix host entry.
3. The host entry's **"Host name" field must exactly match** what the
   pusher sends — by default the machine's FQDN; override with
   `--zabbix-host`/`ZABBIX_HOSTNAME` if they differ.
4. **Set `{$CERTINEXT.SENDER.ALLOWED}`** (host- or template-level macro
   override) to the source IP(s) of the host(s) running
   `certinext-zabbix-push`. The committed value (`127.0.0.1`) rejects
   every real sender until you do this.
5. **Mute sandbox notifications**: every sandbox item/trigger is tagged
   `env:sandbox`. In the trigger action(s) that send notifications
   (*Alerts → Actions → Trigger actions*), add the condition *Tag value*
   | `env` | *does not equal* | `sandbox`. Sandbox problems then still
   appear in the UI but never page anyone.

## Two schedules

| Run | Cadence | Command | Pushes |
|---|---|---|---|
| frequent | every 15 min | `certinext-zabbix-push` | `certinext.domains.total`, `certinext.domains.unverified` (one cheap list call) |
| daily | once a day | `certinext-zabbix-push --expiry-days 14` | additionally `certinext.dcv.expiring`, `certinext.dcv.min_days_left` (one detail call **per verified domain** — never schedule this frequently) |

Pick an `--expiry-days` lead time that sits comfortably inside your own
DCV-renewal automation's lead time, so this monitor firing means renewal
has already been silently failing for a while — a real alert, not noise.

## systemd units (recommended over cron)

Two timer/service pairs: a frequent plain run, and a daily run that also
checks DCV expiry. If they ever overlap, the built-in per-job lock makes
the loser exit 0 — harmless. Example unit files are in
[examples/systemd/](../examples/systemd/); copy and adjust the `User=`,
`EnvironmentFile=`, and `ExecStart=` paths to match your install.

```ini
# /etc/systemd/system/certinext-zabbix-push.service
[Unit]
Description=Push CertiNext DCV health metrics to Zabbix (frequent)
Documentation=https://github.com/tod-uma/certinext-zabbix
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=certinextzbx
Group=certinextzbx
EnvironmentFile=/etc/certinext-zabbix/certinext-zabbix.env
ExecStart=/opt/certinext-zabbix/bin/certinext-zabbix-push
# SIGTERM mid-run exits 130 after cleanup; treat an operator stop as success.
SuccessExitStatus=130
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/tmp
# WARNING: do NOT set PrivateTmp=yes. The single-instance lock lives in
# /tmp and must be visible to BOTH service units below; PrivateTmp would
# give each unit its own /tmp and defeat the lock.
```

```ini
# /etc/systemd/system/certinext-zabbix-push.timer
[Unit]
Description=Run certinext-zabbix-push every 15 minutes

[Timer]
OnCalendar=*:00/15
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/certinext-zabbix-push-expiry.service
# Identical to certinext-zabbix-push.service except:
#   Description=Push CertiNext DCV expiry metrics to Zabbix (daily)
#   ExecStart=/opt/certinext-zabbix/bin/certinext-zabbix-push --expiry-days 14
```

```ini
# /etc/systemd/system/certinext-zabbix-push-expiry.timer
[Unit]
Description=Daily certinext-zabbix-push run with DCV expiry metrics

[Timer]
OnCalendar=03:12
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
systemctl daemon-reload
systemctl enable --now certinext-zabbix-push.timer certinext-zabbix-push-expiry.timer
```

Logs land in the journal as logfmt (`key=value`) lines; every line of one run shares a
`correlation_id`:

```bash
journalctl -u certinext-zabbix-push.service --since -1h
```

## cron alternative

`/etc/cron.d` files accept environment assignments and may be root-only
(mode 0600) — crond reads them as root and runs the job as the named
user. See [examples/cron/certinext-zabbix-push.cron](../examples/cron/certinext-zabbix-push.cron)
for a ready-to-adapt file:

```bash
# /etc/cron.d/certinext-zabbix-push   (mode 0600, owner root:root)
CERTINEXT_CLIENT_ID=<account number>
CERTINEXT_CLIENT_SECRET=<client secret>
ZABBIX_SERVER=<your zabbix server address>
ZABBIX_HOSTNAME=<host name exactly as registered in Zabbix>

*/15 * * * *  certinextzbx  /opt/certinext-zabbix/bin/certinext-zabbix-push 2>>/var/log/certinext-zabbix/push.log
12 3 * * *    certinextzbx  /opt/certinext-zabbix/bin/certinext-zabbix-push --expiry-days 14 2>>/var/log/certinext-zabbix/push.log
```

With cron you own the log plumbing: create `/var/log/certinext-zabbix/`
owned by `certinextzbx` and add a logrotate policy, or pipe stderr to
`logger -t certinext-zabbix-push` instead of a file. No `flock` wrapper is
needed (built-in lock). Prefer systemd where available — journald
capture, `Persistent=true` catch-up, and randomized delay come free.

## Windows

[examples/windows/](../examples/windows/) has a Task Scheduler +
PowerShell equivalent (dedicated venv, service account, an env-file
wrapper script, and a task-registration script). **It is untested** —
written by analogy with the systemd/cron setup above, not run against a
real Windows host. Review it, and have it checked by someone who knows
your Windows environment, before using it in production.

## Nodata windows and cadence

The `nodata()` trigger windows in the template assume roughly these
cadences (frequent: alert after 1h of silence via
`{$CERTINEXT.NODATA.FAST}`; daily: after 26h via
`{$CERTINEXT.NODATA.DAILY}`). If you change a timer's cadence materially,
adjust the matching macro on the Zabbix side. The DCV-expiry severity
tiers are likewise macros (`{$CERTINEXT.DCV.WARN_DAYS}`,
`AVG_DAYS`, `HIGH_DAYS`, `DISASTER_DAYS`) — tune them in Zabbix, no
re-import needed; they are independent of the pusher's `--expiry-days`
flag, which only controls the expiring-count metric. See the
[per-template README](../templates/template_certinext/7.0/README.md) for
the full macro table.

## Optional: monitoring the sandbox too

Sandbox monitoring is off by default — enable it only when there is a
concrete need (e.g. watching sandbox state during a test campaign).
Because the item keys are environment-parameterized, it needs **no new
Zabbix host and no template change**:

1. A separate env file with the **sandbox** credentials, e.g.
   `/etc/certinext-zabbix/certinext-zabbix-sandbox.env` (mode 0600,
   root-owned) with `CERTINEXT_CLIENT_ID`/`CERTINEXT_CLIENT_SECRET` — the
   sandbox account's values.
2. Timer/service pairs mirroring the prod ones, with
   `EnvironmentFile=/etc/certinext-zabbix/certinext-zabbix-sandbox.env`
   and `ExecStart=/opt/certinext-zabbix/bin/certinext-zabbix-push --sandbox`
   (add `--expiry-days 14` on the daily one). Offset the timers from the
   prod ones; the locks are per environment, so overlap is harmless but
   staggering avoids hitting CertiNext simultaneously.
3. Enable the two disabled `CertiNext sandbox: no data from pusher`
   triggers on the template.

Sandbox runs push into the `[sandbox]` items automatically (the pusher
derives the key parameter from its connection). With the `env:sandbox`
action condition from the one-time setup, sandbox problems stay visible
in the UI but never notify.

## Validation

```bash
# 1. Metrics computed correctly, nothing sent
sudo -u certinextzbx bash -c 'set -a; . /etc/certinext-zabbix/certinext-zabbix.env; set +a; \
  exec /opt/certinext-zabbix/bin/certinext-zabbix-push --dry-run --expiry-days 14 -v'

# 2. One real frequent-run push, then check the values arrived
systemctl start certinext-zabbix-push.service
journalctl -u certinext-zabbix-push.service -o cat --since -10m
```

In Zabbix, *Monitoring → Latest data* for the host should show fresh
values for `certinext.domains.total[prod]` and
`certinext.domains.unverified[prod]`.
A `"Zabbix rejected item values"` error means the host name, template
link, or the `{$CERTINEXT.SENDER.ALLOWED}` macro doesn't match.

## Network requirements

| Destination | Port | Direction | Purpose |
|---|---|---|---|
| `us-api.certinext.io` | 443 | outbound | CertiNext API + OAuth token endpoint (proxy-able via `HTTPS_PROXY`) |
| `sandbox-us-api.certinext.io` | 443 | outbound | Only when running with `--sandbox` |
| Your Zabbix server | 10051 | outbound | `certinext-zabbix-push` trapper sends (same port an active-mode Zabbix agent uses) |

## Configuration-management checklist

The complete task list for a playbook/role (Ansible module hints in
parentheses):

1. Install `uv` (get_url + command, or a distro package). Idempotence:
   skip when `/usr/local/bin/uv` (or packaged path) exists.
2. Create the `certinextzbx` system user (`user`, `system: true`,
   `shell: /usr/sbin/nologin`, `create_home: false`).
3. Create the venv and install the pinned version (`command`, creates:
   `/opt/certinext-zabbix/bin/certinext-zabbix-push`; re-run on version
   change).
4. Template `/etc/certinext-zabbix/certinext-zabbix.env` (`template`,
   `mode: "0600"`, `owner: root`); secrets from vault.
5. Install the four unit files (`copy`/`template` into
   `/etc/systemd/system/`): the `certinext-zabbix-push` and
   `certinext-zabbix-push-expiry` timer/service pairs. Then
   `systemd: daemon_reload: true`.
6. Enable + start the two **timers** — not the services (`systemd`,
   `name: certinext-zabbix-push.timer`, `enabled: true`,
   `state: started`; same for `certinext-zabbix-push-expiry.timer`).
7. Optional verification task: the `--dry-run` command from
   [Validation](#validation) as a `command` task with
   `changed_when: false`.
8. No handlers needed on upgrades: `Type=oneshot` services pick up the
   new binary at the next timer firing. Env-file or unit changes only
   need `daemon_reload` (units) — nothing to restart.

Facts a playbook must parameterize: the version pin, the two credential
values, any `ZABBIX_*` overrides, and the two timer schedules.

Out of playbook scope (one-time, done in the Zabbix UI): importing
`templates/template_certinext/7.0/template_certinext.yaml` and linking
**CertiNext DCV by Zabbix trapper** to the host — see
[One-time Zabbix server setup](#one-time-zabbix-server-setup-not-a-playbook-task).
