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
  the old one-JSON-object-per-line format instead. Under systemd,
  `--log-mode` (default `auto`) additionally drops the redundant
  `timestamp`/`pid` fields, since journald already stamps both on every
  forwarded line.
- **Unattended-run tracebacks:** a run that fails outright logs a truncated
  traceback on its own journal line, so the usual "what blew up?" question is
  answerable from the journal alone. Per-domain failures stay concise — one
  bad run would otherwise dump a stack per domain. For the full untruncated
  stack, and for everything that happened *before* the failure, set
  `--debug-log-path` (env `CERTINEXT_ZABBIX_DEBUG_LOG`) to append every event
  to a host-local file, independent of `-v`, so it's safe to leave on
  permanently. See
  [Recovering a traceback from an unattended run](#recovering-a-traceback-from-an-unattended-run).
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
# The Zabbix host for THIS environment (one host per environment):
ZABBIX_HOSTNAME=<prod host name exactly as registered in Zabbix>
# Only if the account also holds other CAs' records (see --validated-by):
#CERTINEXT_VALIDATED_BY=SSLCOM

# Only if this host lacks direct HTTPS egress to the CertiNext API:
#HTTPS_PROXY=http://proxy.example.org:3128
#NO_PROXY=127.0.0.1,localhost
```

### Environment variable reference

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `CERTINEXT_CLIENT_ID` | yes | — | CertiNext account number / OAuth2 client_id |
| `CERTINEXT_CLIENT_SECRET` | yes | — | CertiNext OAuth2 client secret |
| `ZABBIX_SERVER` | yes | — | Zabbix trapper address. No built-in default — an unset value fails fast rather than silently targeting the wrong server. **If the Zabbix host is monitored by a proxy, point this at the proxy, not the server** — see [Proxies and proxy groups](#proxies-and-proxy-groups). |
| `ZABBIX_PORT` | no | `10051` | Zabbix trapper port |
| `ZABBIX_HOSTNAME` | no | this machine's FQDN | Host name exactly as registered in Zabbix — **set explicitly in production**; the FQDN fallback depends on `/etc/hosts`/reverse DNS and logs a warning when it looks unusable. Each CertiNext environment has its own Zabbix host ([ADR 0013](adr/0013-one-zabbix-host-per-certinext-environment.md)), so sandbox runs need a different value from prod runs — set it in each environment's env file |
| `ZABBIX_TIMEOUT` | no | `10` | Socket timeout (seconds) for the trapper send |
| `CERTINEXT_DOMAIN_SCOPE` | no | `top` | Which domains to monitor: `top`, `ns-boundary`, or `all` — see the CLI flag reference below |
| `CERTINEXT_VALIDATED_BY` | no | unset (every CA) | Only monitor domain records validated by this CA, e.g. `SSLCOM` — see the CLI flag reference below. Empty means unset |
| `CERTINEXT_ORDER_HEALTH` | no | off | Enable the order-health metrics — see the CLI flag reference below |
| `CERTINEXT_ORDER_FAILING_LOOKBACK_DAYS` | no | `30` | Only count failed orders from this many days back as "failed recently" |
| `CERTINEXT_ORDER_HISTORY_DAYS` | no | `1095` | Bound the orders-report fetch to this many days back — see the CLI flag reference below |
| `CERTINEXT_ZABBIX_DEBUG_LOG` | no | off | Path for the always-on human-readable DEBUG traceback log — see [Recovering a traceback from an unattended run](#recovering-a-traceback-from-an-unattended-run) |
| `HTTPS_PROXY` / `NO_PROXY` | no | — | Standard proxy vars, honored for the CertiNext HTTPS calls (httpx) |

### CLI flag reference

| Flag | Meaning |
|---|---|
| `--dry-run` | Compute and print the metrics without sending anything to Zabbix. Use for validation. |
| `--domain-scope {top,ns-boundary,all}` | Which domains to monitor — applies to all four metrics, not just expiry. `top` (default) excludes any domain with a registered ancestor in the account (no DNS lookups). `ns-boundary` does the same but re-includes a domain with its own NS records (a real DNS zone cut). `all` restores the pre-`--domain-scope` unfiltered behavior. Switching away from `all` causes a one-time drop in `certinext.domains.total` — expected, not a fault. |
| `--validated-by CA` | Only monitor domain records validated by this CA (`SSLCOM`, `EMSIGN`; case-insensitive). CertiNext keeps one record per CA per name, so without it every CA's records are counted together. Applies to the domain and DCV-expiry metrics, not order health, and runs before `--domain-scope`. Item keys are unchanged, so switching CA is a step change in their history. Set it on **every** timer (frequent, daily-expiry and hourly) so they all measure the same CA. Each environment's env file sets it independently, according to the CAs that account holds; prod and sandbox can differ. Don't set it before the account holds records for that CA, or `domains.total` reads 0 and the empty-list trigger fires. See [ADR 0011](adr/0011-dcv-metrics-single-ca-by-flag.md). |
| `--expiry-days DAYS` | Also push the DCV-expiry metrics (one extra API call **per verified domain** — a real prod run of this loop took 3m25s; schedule this on its own daily run, not more often). Disabled by default. |
| `--order-health` | Also push the order-health metrics: orders awaiting issuance, certificates generated but never downloaded, orders that recently failed, and days since the last certificate was issued (one Orders Report fetch, paginated — 2 pages at current order volume, cheap). Schedule this **hourly**, on its own timer, separate from `--expiry-days` — the `unissued`/`undownloaded` "stuck" triggers use a `min()` window that needs multiple samples inside it to actually guard against a single noisy snapshot; at a daily cadence the window only ever held one sample and silently degraded to a plain threshold check (see [sysadmin/certinext-zabbix#8](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/8)). Disabled by default. |
| `--order-failing-lookback-days DAYS` | Only count failed orders from the last `DAYS` days toward the failed-recent metric. Default `30`. |
| `--order-history-days DAYS` | Bound the orders-report fetch to the last `DAYS` days so it does not grow without limit as order history accumulates. Must stay comfortably longer than the longest certificate lifetime in the account — an issued cert older than this window drops out of `certinext.orders.expiring`. Public TLS certificates cap at 398 days, so the `1095` (3-year) default has ample margin. |
| `--zabbix-server` / `--zabbix-port` / `--zabbix-host` / `--zabbix-timeout` | Override the matching environment variables above. |
| `--sandbox` | Use the CertiNext **sandbox** API — see [Optional: monitoring the sandbox too](#optional-monitoring-the-sandbox-too). |
| `-v` / `-vvv` / `-vvvv` | Verbosity: config details / script debug / third-party debug. Not needed in production; logs are complete at default verbosity. |
| `--log-format json` | Emit one JSON object per line instead of the default logfmt (`key=value`) lines. |
| `--log-mode {auto,syslog,verbose}` | Non-interactive field verbosity: `auto` (default) drops the redundant `timestamp`/`pid` fields when systemd is detected, `syslog` always drops them, `verbose` always keeps them. |
| `--debug-log-path PATH` | Append a human-readable DEBUG-level log (full tracebacks) to `PATH`, independent of `-v` — see [Recovering a traceback from an unattended run](#recovering-a-traceback-from-an-unattended-run). |

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
   by Zabbix trapper**: thirteen trapper items with bare keys
   (`certinext.domains.total`, …), sixteen triggers (including a DCV-expiry
   severity gradient for token-method domains — WARNING → AVERAGE → HIGH
   → DISASTER, dependency-chained so only the deepest tier alerts), a
   dns-persist discovery rule whose prototypes are passive agent checks of
   each apex's `_validation-persist` TXT record, and the macros.
   `certinext.orders.expiring` ships deliberately trigger-less (per
   [ADR 0008](adr/0008-orders-expiring-ships-data-only.md)). Upgrading
   from 0.1.x? See [Upgrading from 0.1.x](#upgrading-from-01x) instead.
2. **Create one Zabbix host per CertiNext environment** you monitor
   ([ADR 0013](adr/0013-one-zabbix-host-per-certinext-environment.md)),
   e.g. `CertiNext prod` (and `CertiNext sandbox` only if you
   [monitor the sandbox](#optional-monitoring-the-sandbox-too)). Each one:
   - **Host name** exactly matches what that environment's runs send as
     `--zabbix-host`/`ZABBIX_HOSTNAME`. The prod host can simply be the
     pusher machine's existing host entry, if it already has one.
   - An **agent interface** pointing at the machine that runs the pusher
     (the dns-persist checks are passive agent items; both hosts can point
     at the same machine). That machine needs a Zabbix agent or agent 2.
   - **Monitored by** the same server or proxy the trapper sends go to.
   - **Linked** to the template.
3. **Set the host macros** on each host:
   - `{$CERTINEXT.ENV}` — `prod` or `sandbox`. Defaults to `prod`; a sandbox
     host left at the default raises the HIGH *pusher data from the wrong
     environment* trigger on its first run.
   - `{$CERTINEXT.SENDER.ALLOWED}` — the source IP(s) of the machine running
     `certinext-zabbix-push`. The committed value (`127.0.0.1`) rejects
     every real sender until you do this.
4. **Mute sandbox notifications**: every item/trigger is tagged
   `env:{$CERTINEXT.ENV}`. In the trigger action(s) that send notifications
   (*Alerts → Actions → Trigger actions*), add the condition *Tag value*
   | `env` | *does not equal* | `sandbox`. Sandbox problems then still
   appear in the UI but never page anyone. This relies on the sandbox
   host's `{$CERTINEXT.ENV}` being set.
5. **Clear the abandoned-order backlog before enabling order-health
   notifications** — see below.

### Clear the abandoned-order backlog first

Both order-stuck triggers compare against zero:

- *order(s) stuck awaiting issuance* — `certinext.orders.unissued` > 0
  for the whole of `{$CERTINEXT.ORDER.STUCK_AGE}`
- *certificate(s) generated but never downloaded* —
  `certinext.orders.undownloaded` > 0 for the whole of
  `{$CERTINEXT.ORDER.UNDOWNLOADED_AGE}`

An account that accumulated abandoned orders while people were still
learning the CertiNext workflow will hold both counts permanently above
zero, so both triggers fire on the first `--order-health` run and never
clear. A
full-report production measurement on 2026-08-07 (all 163 rows, not a
sample) showed **24** orders awaiting issuance — median age 63 days, 18
of them older than 30 days — and **13** certificates generated but never
downloaded.

Before enabling notifications for these two triggers, go through the
account and cancel the orders that are genuinely dead, and download or
cancel the generated-but-uncollected certificates. Run
`certinext-zabbix-push --order-health --dry-run -v` to see the current
counts without sending anything; the goal is a steady-state of zero (or a
small number that clears on its own within the macro window).

## Three schedules

| Run | Cadence | Command | Pushes |
|---|---|---|---|
| frequent | every 6 hours | `certinext-zabbix-push` | `certinext.domains.total`, `certinext.domains.unverified` (one cheap list call) |
| order-health | hourly | `certinext-zabbix-push --order-health` | `certinext.orders.unissued`, `certinext.orders.undownloaded`, `certinext.orders.failed_recent`, `certinext.orders.expiring`, `certinext.orders.days_since_issued` (one paginated Orders Report fetch, 2 pages at current order volume) |
| daily | once a day | `certinext-zabbix-push --expiry-days 14` | `certinext.dcv.expiring`, `certinext.dcv.token_domains`, `certinext.dcv.min_days_left`, `certinext.dcv.persist_min_days_left`, `certinext.dcv.persist_apexes` and the dns-persist discovery value (each apex with the CA's expected record value) (a DCV-method read per domain plus a detail call **per verified domain** — the expensive one; a real prod run took 3m25s before the method read was added) |

Every run also sends `certinext.env`, the environment it connected to,
which the template compares against the host's `{$CERTINEXT.ENV}`.

Pick an `--expiry-days` lead time that sits comfortably inside your own
DCV-renewal automation's lead time, so this monitor firing means renewal
has already been silently failing for a while — a real alert, not noise.

`--expiry-days` and `--order-health` are independent flags, and — unlike
before — **deliberately scheduled on separate timers at different
cadences**, not combined on one invocation. `--expiry-days` stays daily
because it is genuinely expensive (a per-domain call). `--order-health`
moved to hourly because it is cheap (a couple of paginated calls) *and*
because the `unissued`/`undownloaded` "stuck" triggers wrap their metric
in a `min()` window that only works as a real sustain guard when the
window holds more than one sample; at the old shared daily cadence it
held exactly one, and silently behaved like a plain threshold check
instead — see [sysadmin/certinext-zabbix#8](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/8).
If you still run them together for some reason, they share the same lock
tier, so a collision just makes the loser exit 0 rather than skipping a
whole cycle — but there's no longer a reason to combine them.

## systemd units (recommended over cron)

Three timer/service pairs: a frequent plain run, an hourly order-health
run, and a daily expiry run. Each has its own lock tier, so any overlap
between *different* pairs just makes the loser exit 0 — harmless; see
[sysadmin/certinext-zabbix#8](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/8)
for why order-health needed its own tier rather than continuing to share
one with expiry. Example unit files are in
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
# /tmp and must be visible to every certinext-zabbix-push* unit;
# PrivateTmp would give each unit its own /tmp and defeat the lock.
```

```ini
# /etc/systemd/system/certinext-zabbix-push.timer
[Unit]
Description=Run certinext-zabbix-push every 6 hours

[Timer]
OnCalendar=00/6:00:00
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/certinext-zabbix-push-order-health.service
# Identical to certinext-zabbix-push.service except:
#   Description=Push CertiNext order-health metrics to Zabbix (hourly)
#   ExecStart=/opt/certinext-zabbix/bin/certinext-zabbix-push --order-health
```

```ini
# /etc/systemd/system/certinext-zabbix-push-order-health.timer
[Unit]
Description=Hourly certinext-zabbix-push run with order-health metrics

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
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
systemctl enable --now certinext-zabbix-push.timer certinext-zabbix-push-order-health.timer certinext-zabbix-push-expiry.timer
```

Logs land in the journal as logfmt (`key=value`) lines; every line of one run shares a
`correlation_id`:

```bash
journalctl -u certinext-zabbix-push.service --since -1h
```

### Recovering a traceback from an unattended run

Two places carry a traceback, and they answer different questions.

**The journal** carries one when the *whole run* fails — the outermost
handler attaches the stack to its `level=error` line as a quoted
`exception="Traceback...\n..."` field, truncated to the innermost frames and
escaped onto a single line so journald and rsyslog can't split it into
fragments. Because the line is logfmt, `exception` is an extracted field in
Splunk with no per-sourcetype configuration, joinable to the rest of the run
on `correlation_id`. Per-domain failures deliberately do **not** carry one:
they log concisely with a "re-run with -vvv" hint, because a systemic failure
would otherwise emit one stack per domain.

**The debug-log file** carries the full untruncated stack, plus every DEBUG
event leading up to the failure — often more diagnostic than the traceback
itself. Setting `--debug-log-path` (env `CERTINEXT_ZABBIX_DEBUG_LOG`) opens
it, independent of `-v`/`-vvv`, in the same human-readable format you get on
a terminal. It is **host-local by design** and deliberately not ingested into
Splunk, so reading it means SSH-ing to the host:

```bash
grep -A30 '\[error' /var/log/certinext-zabbix/debug.log
```

Two consequences of that being host-local: log rotation is the only thing
bounding how far back you can look, so size the retention window against how
long a broken timer might go unnoticed; and the file is lost if the host is
rebuilt.

```bash
# /etc/certinext-zabbix/certinext-zabbix.env
CERTINEXT_ZABBIX_DEBUG_LOG=/var/log/certinext-zabbix/debug.log
```

Create the directory first (owned by the service user) and add it to the
unit's `ReadWritePaths=` — already done in the example units in
[examples/systemd/](../examples/systemd/). This repo does not rotate the
file itself; add a `logrotate` policy for `/var/log/certinext-zabbix/` (see
the [configuration-management checklist](#configuration-management-checklist)).

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

0 */6 * * *   certinextzbx  /opt/certinext-zabbix/bin/certinext-zabbix-push 2>>/var/log/certinext-zabbix/push.log
7 * * * *     certinextzbx  /opt/certinext-zabbix/bin/certinext-zabbix-push --order-health 2>>/var/log/certinext-zabbix/push.log
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
cadences: frequent (domain-list) — alert after one missed 6-hour run is
tolerated, a second alerts, via `{$CERTINEXT.NODATA.FAST}` (13h);
order-health — alert after one missed hourly run is tolerated, a second
alerts, via `{$CERTINEXT.NODATA.ORDER_HEALTH}` (3h); daily (expiry) —
one missed daily run alerts, via `{$CERTINEXT.NODATA.DAILY}` (26h). If
you change a timer's cadence materially, adjust the matching macro on the
Zabbix side. The daily window also covers *sending* the dns-persist
discovery: the pusher never sends the daily items without it, so a
discovery list that stopped arriving shows up as *no data from pusher
(daily expiry run)*. It does not prove Zabbix *applied* the list: if LLD
fails to create or update a discovered trigger, the push still succeeds
and the old trigger can keep checking the old value. That error shows on
the discovery rule itself (*Data collection → Hosts → Discovery*, the
rule's *Info* column), so check it after a template import or a change in
the CA's value.

The dns-persist lookups are agent checks, not pusher data, with their own
cadence: `{$CERTINEXT.DNS.INTERVAL.LOCAL}` (5m) and
`{$CERTINEXT.DNS.INTERVAL.EXTERNAL}` (1h), with `{$CERTINEXT.DNS.NODATA}`
(3h) as their *lookup failing* window. Keep `DNS.NODATA` above
`DNS.INTERVAL.EXTERNAL`.

`{$CERTINEXT.ORDER.STUCK_AGE}`'s `min()` trigger additionally assumes
`--order-health` pushes **more often than once per STUCK_AGE window**
(hourly, by default) — the window needs several samples inside it to
actually require sustained badness rather than degrading to a single
snapshot check. See
[sysadmin/certinext-zabbix#8](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/8)
for the incident this was found from, and don't move `--order-health`
back onto a daily timer without either widening `STUCK_AGE` to match or
accepting that the guard is a no-op again.

The DCV-expiry severity tiers are likewise macros
(`{$CERTINEXT.DCV.WARN_DAYS}`, `AVG_DAYS`, `HIGH_DAYS`, `DISASTER_DAYS`)
— tune them in Zabbix, no re-import needed; they are independent of the
pusher's `--expiry-days` flag, which only controls the expiring-count
metric. Likewise `{$CERTINEXT.ORDER.STUCK_AGE}` and
`{$CERTINEXT.ORDER.UNDOWNLOADED_AGE}` are independent of
`--order-failing-lookback-days`, which only controls the failed-recent
metric, and of `--order-history-days`, which only bounds how far back the
report fetch reaches. See the
[per-template README](../templates/template_certinext/7.0/README.md) for
the full macro table.

## Optional: monitoring the sandbox too

Sandbox monitoring is off by default — enable it only when there is a
concrete need (e.g. watching sandbox state during a test campaign).
The sandbox gets **its own Zabbix host** linked to the same template
([ADR 0013](adr/0013-one-zabbix-host-per-certinext-environment.md)); no
template change:

1. A sandbox host, set up as in
   [One-time Zabbix server setup](#one-time-zabbix-server-setup-not-a-playbook-task)
   step 2, with `{$CERTINEXT.ENV}` = `sandbox` and
   `{$CERTINEXT.SENDER.ALLOWED}`. A low-volume sandbox account will also want a higher
   `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}`.
2. A separate env file with the **sandbox** credentials and host name, e.g.
   `/etc/certinext-zabbix/certinext-zabbix-sandbox.env` (mode 0600,
   root-owned) with `CERTINEXT_CLIENT_ID`/`CERTINEXT_CLIENT_SECRET` — the
   sandbox account's values — and `ZABBIX_HOSTNAME` set to the sandbox
   host's name. Without it, sandbox runs land on the prod host and raise
   its misroute trigger.
3. Timer/service pairs mirroring the prod ones (frequent, hourly
   order-health, daily expiry — same three-way split), with
   `EnvironmentFile=/etc/certinext-zabbix/certinext-zabbix-sandbox.env`
   and `ExecStart=/opt/certinext-zabbix/bin/certinext-zabbix-push --sandbox`
   (add `--order-health` on the hourly one, `--expiry-days 14` on the
   daily one — kept apart for the same reason as prod, see
   [#8](https://gitlab.its.maine.edu/sysadmin/certinext-zabbix/-/issues/8)).
   Offset the timers from the prod ones; the locks are per environment, so
   overlap is harmless but staggering avoids hitting CertiNext
   simultaneously.
4. If you skip any of the three sandbox schedules, disable the matching
   *no data from pusher* trigger on the sandbox host (not the template).

With the `env:sandbox` action condition from the one-time setup, sandbox
problems stay visible in the UI but never notify.

## Upgrading from 0.1.x

0.2.0 changes **every item key**: `certinext.domains.total[prod]` becomes
`certinext.domains.total`, and each environment moves to its own Zabbix
host ([ADR 0013](adr/0013-one-zabbix-host-per-certinext-environment.md)).
A 0.1.x template rejects a 0.2.0 pusher's values and vice versa, so
upgrade both together, **Zabbix side first**, with the pusher's timers
stopped in between. A run that lands mid-upgrade fails with
`Zabbix rejected item values` and exits 1. The hourly order-health timer
makes that likely, unless the timers are stopped.

**Pick a path before you start.** Both share the preparation, the timer
stop and the in-place re-import, so either way you learn whether history
carried over. They differ in which Zabbix hosts receive 0.2.0's data:

- **Path A, keep the existing host** as the prod host. Fits when that host
  exists only for CertiNext monitoring.
- **Path B, fresh hosts** (`CertiNext prod`, `CertiNext sandbox`). Fits
  when the existing host is the pusher machine's own entry, carrying its
  OS or other monitoring. The CertiNext data then gets hosts of its own,
  and the old host is cleared at the end.

### Before the change window

1. **Prepare the pusher side completely.** If configuration management
   deploys the pusher, have the role and pin change released, merged and
   ready to run; it can't be produced inside the window. Each
   environment's env file needs its own `ZABBIX_HOSTNAME` (see
   [Configuration-management checklist](#configuration-management-checklist)).
2. **No dns-persist value to prepare.** From 0.2.0rc3 the daily run sends
   each apex's expected `_validation-persist` value from the CA's API. A
   host coming from 0.2.0rc1/rc2 has a `{$CERTINEXT.PERSIST.EXPECTED}`
   host macro: delete it after the upgrade, since nothing reads it. To see
   the value the pusher will send, run
   `certinext-domains [--profile sandbox] get-dcv <apex> --validated-by SSLCOM`
   (the `DNS-PERSIST` option's record value).
3. **Check the new network paths** from the pusher machine:
   `dig +short @1.1.1.1 TXT _validation-persist.<apex>` and the same with
   `@8.8.8.8` should both return the record. From the Zabbix server or
   proxy, `zabbix_get -s <pusher machine> -k agent.variant` must answer
   (`1` = agent, `2` = agent 2); see the template README's
   *Requirements* for why the variant matters.
4. **Note one item ID.** On the existing host, open
   `certinext.domains.total[prod]` and note its item ID (in the edit
   page's URL). Step 2 uses it to tell an in-place update from a restart.
5. **Read [New alerts after upgrading](#new-alerts-after-upgrading)**, and
   raise `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}` where it applies.

### Both paths: stop, then re-import

1. **Stop every pusher timer, every environment**:
   `systemctl stop 'certinext-zabbix-push*.timer'`. The last step starts
   them again.
2. **Re-import the template over the existing one**
   (*Data collection → Templates → Import*). Under *Rules*, tick
   *Update existing* everywhere and *Delete missing* for items, triggers
   and discovery rules, so the old `[sandbox]` copies are removed.
   The 0.2.0 template keeps the 0.1.x `[prod]` items' UUIDs, so Zabbix is
   expected to update them **in place** to the bare keys, and the linked
   host keeps their history. This hasn't been confirmed by a live import
   yet, so check it: open the item ID you noted. If it now has the bare key
   `certinext.domains.total` and its pre-upgrade values, history carried
   over. If the ID no longer exists, history restarted; nothing else is
   lost. The `[sandbox]` items and their history are deleted either way.

### Path A: keep the existing host

3. **Turn the existing host into the prod host.** `{$CERTINEXT.ENV}`
   defaults to `prod`, so it needs no macro. Add an **agent interface**
   pointing at the pusher machine if the host doesn't have one (it does
   if the host *is* the pusher machine with an agent template linked).
   Existing host macros, such as
   `{$CERTINEXT.SENDER.ALLOWED}` and threshold overrides, keep working:
   no macro was renamed.
4. **Review the DCV-threshold overrides.** If you raised or disabled the
   `{$CERTINEXT.DCV.*_DAYS}` macros on the host to quiet 0.1.x WARNINGs on
   SSL.com dns-persist domains, delete those overrides: persist domains no
   longer reach those triggers
   ([ADR 0014](adr/0014-dns-persist-checked-by-zabbix-agent.md)).
   Overrides equal to the new defaults (3/2/1/0) are redundant, and
   harmless either way.
5. **If you monitor the sandbox**, create its new host as in
   [Optional: monitoring the sandbox too](#optional-monitoring-the-sandbox-too),
   step 1. It starts with none of the old host's macros. Set
   `{$CERTINEXT.ENV}` = `sandbox` and `{$CERTINEXT.SENDER.ALLOWED}`
   (copy the old host's value; 0.1.x installs set it there as a host
   override). If your sandbox action condition matches a literal `env` tag,
   it keeps working: the tag value is still `sandbox`.

### Path B: fresh hosts

3. **Create `CertiNext prod`** (and `CertiNext sandbox`, if you monitor it)
   as in [One-time Zabbix server setup](#one-time-zabbix-server-setup-not-a-playbook-task)
   steps 2–3. Each needs an agent interface pointing at the pusher machine,
   the same *Monitored by* as the trapper sends, the template linked, and
   both macros: `{$CERTINEXT.ENV}` and `{$CERTINEXT.SENDER.ALLOWED}`
   (copy the old host's value). Carry
   over any threshold overrides you still want; the old host's macros don't
   move.
4. Leave the old host linked for now. Step 9 clears it once the new hosts
   are verified.

### Both paths: upgrade the pusher and verify

6. **Upgrade the pusher**: run the prepared configuration-management
   change, or re-run [Install](#install) step 3 pinned to 0.2.0 and set
   `ZABBIX_HOSTNAME` in each environment's env file (Path A: the sandbox
   only; Path B: both). Under 0.1.x both environments sent to the same
   host name, so a sandbox env file left unchanged lands on the prod host
   and raises its misroute trigger.
7. **Start the timers again**: `systemctl start 'certinext-zabbix-push*.timer'`
   (or let the configuration-management run enable them).
8. **Verify:** start each environment's daily unit once
   (`systemctl start certinext-zabbix-push-expiry.service`, plus the
   sandbox's), then check in Zabbix that:
   - `certinext.env` reads the right environment on each host;
   - the discovery rule created `certinext.dcv.persist_apexes` × 3
     `net.dns.record` items;
   - no *pusher data from the wrong environment* or *dns-persist record
     missing* problem is open once the first lookups have run (about
     15 minutes for the local one).
9. **Path B only: clear the old host.** After step 2's history check is
   recorded and step 8 passes, *Unlink and clear* **CertiNext DCV by
   Zabbix trapper** from the old host. That deletes its CertiNext items,
   their history and any open CertiNext problems on it. Other templates on
   the host are untouched.

### New alerts after upgrading

0.2.0 adds triggers that 0.1.x never had, and some can open on the first
run:

| Trigger | Opens at once when |
| --- | --- |
| pusher data from the wrong environment (HIGH) | a run reaches a host whose `{$CERTINEXT.ENV}` doesn't match: an unchanged sandbox `ZABBIX_HOSTNAME`, or a sandbox host left at the `prod` default |
| dns-persist record missing for `{#APEX}` (HIGH, per apex) | the published record doesn't hold the value the CA's API gives for that apex, or the record is gone. If the trigger's expected value reads `NO-USABLE-DNS-PERSIST-VALUE-FROM-CA-API-SEE-PUSHER-LOG`, DNS may be fine: the pusher log says why the CA's value was unusable |
| dns-persist record not visible publicly / lookup failing (AVERAGE, per apex) | outbound DNS to the public resolvers is blocked, or the agent can't run the lookup |
| no certificate issued for over `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}` days (WARNING) | the account issues less often than the macro (default 30 days): typical of a sandbox. The item existed in 0.1.x, but the trigger didn't |
| dns-persist reuse period lapsed (INFORMATION) | the SSL.com reuse window has passed; informational by design |
| dns-persist apex count dropped (WARNING) | not on day one; only after an apex leaves the discovery list |

## Validation

```bash
# 1. Metrics computed correctly, nothing sent — combining both flags here is
#    just a convenient one-off check; the scheduled deployment above runs
#    them on separate timers (see the "Three schedules" section)
sudo -u certinextzbx bash -c 'set -a; . /etc/certinext-zabbix/certinext-zabbix.env; set +a; \
  exec /opt/certinext-zabbix/bin/certinext-zabbix-push --dry-run --expiry-days 14 --order-health -v'

# 2. One real frequent-run push, then check the values arrived
systemctl start certinext-zabbix-push.service
journalctl -u certinext-zabbix-push.service -o cat --since -10m
```

In Zabbix, *Monitoring → Latest data* for the host should show fresh
values for `certinext.env`, `certinext.domains.total` and
`certinext.domains.unverified`.
A `"Zabbix rejected item values"` error means the host name, template
link, or the `{$CERTINEXT.SENDER.ALLOWED}` macro doesn't match (or a
0.1.x template is still in place — see
[Upgrading from 0.1.x](#upgrading-from-01x)).

After the first daily run, the host should also have three
`net.dns.record[…_validation-persist.<apex>,TXT]` items per dns-persist
apex, each returning a line that contains your expected value in quotes.
To check one lookup by hand from the Zabbix server or proxy:
`zabbix_get -s <pusher machine> -k 'net.dns.record[,_validation-persist.example.edu,TXT]'`.

## Proxies and proxy groups

`ZABBIX_SERVER` must name whatever endpoint actually owns the host's trapper
items:

| Host's *Monitored by* | Point `ZABBIX_SERVER` at |
|---|---|
| Zabbix server | the server |
| Proxy | **that proxy** — not the server |
| Proxy group | the server, which redirects the sender to the assigned proxy |

Sending to the server for a proxy-monitored host does not work; that is standard
Zabbix behaviour, not a quirk of this script. `ZABBIX_HOSTNAME` is unaffected —
it is always the host name as registered in Zabbix, wherever the data is sent.

The proxy's **active/passive operating mode makes no difference here**: that
setting governs how the proxy and server talk to each other, while a trapper
send connects to the proxy's own listening port either way. Mode does affect how
quickly values reach the server afterwards — an active proxy pushes on its own
schedule, a passive one waits to be polled — which is worth accounting for when
sizing the nodata windows below.

### Redirect loops

For a **proxy group**, the endpoint you connect to replies with a redirect to
the assigned proxy's *Address for active agents*, and the sender re-sends there.
`zabbix_utils` follows that redirect by recursing, with no hop limit and no loop
detection — so a proxy group that hands back an address which redirects to
itself recurses until Python's stack limit, roughly a thousand TCP connections
in about a second.

`certinext-zabbix-push` catches this and fails with `Redirect loop sending to
<host>:<port>` rather than an unhandled `RecursionError`, and does **not** retry
it — a retry would just replay the connection storm. If you see it:

- Check each proxy in the group has a **distinct** *Address for active agents*
  matching its own host. Two proxies sharing one address produces exactly this
  loop, because a proxy told to forward to the assignee's address finds that
  address is its own.
- Confirm the assigned proxy has the host in its configuration — a proxy that
  does not believe it owns the host will forward rather than accept.
- As an immediate unblock, point `ZABBIX_SERVER` straight at the proxy that owns
  the host, bypassing the redirect entirely. Note this gives up the proxy
  group's failover.

The full recursion is recoverable from the debug log — see
[Recovering a traceback from an unattended run](#recovering-a-traceback-from-an-unattended-run).

## Network requirements

| Destination | Port | Direction | Purpose |
|---|---|---|---|
| `us-api.certinext.io` | 443 | outbound | CertiNext API + OAuth token endpoint (proxy-able via `HTTPS_PROXY`) |
| `sandbox-us-api.certinext.io` | 443 | outbound | Only when running with `--sandbox` |
| Your Zabbix server or proxy | 10051 | outbound | `certinext-zabbix-push` trapper sends (same port an active-mode Zabbix agent uses) — see [Proxies and proxy groups](#proxies-and-proxy-groups) |
| From your Zabbix server or proxy | 10050 | inbound | Passive agent checks of the dns-persist TXT records (the hosts' agent interfaces point at this machine) |
| `{$CERTINEXT.DNS.RESOLVER.1}` / `RESOLVER.2` (default 1.1.1.1 / 8.8.8.8) | 53 | outbound | The agent's "visible publicly" lookups; the local lookups use this machine's own resolver |

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
   `mode: "0600"`, `owner: root`); secrets from vault. If
   `CERTINEXT_ZABBIX_DEBUG_LOG` is enabled, also create
   `/var/log/certinext-zabbix/` (`file`, `state: directory`, owned by
   `certinextzbx`, mode `0750`) and a `logrotate` policy for it — this
   repo does not rotate the file itself.
5. Install the six unit files (`copy`/`template` into
   `/etc/systemd/system/`): the `certinext-zabbix-push`,
   `certinext-zabbix-push-order-health` and
   `certinext-zabbix-push-expiry` timer/service pairs. Then
   `systemd: daemon_reload: true`. For sandbox monitoring, a second set
   with its own env file (which sets the sandbox `ZABBIX_HOSTNAME`).
   A role written for 0.1.x very likely renders one `ZABBIX_HOSTNAME` into
   both env files. Bumping it to 0.2.0 without separating them sends
   sandbox data to the prod host, and nothing reports it until the
   misroute trigger fires. Guard it: assert that the sandbox
   `ZABBIX_HOSTNAME` differs from prod's whenever the pin is ≥ 0.2.0 and
   sandbox is enabled (`assert`).
6. Enable + start the three **timers** — not the services (`systemd`,
   `name: certinext-zabbix-push.timer`, `enabled: true`,
   `state: started`; same for the `-order-health` and `-expiry` timers).
   The machine also needs a Zabbix agent reachable by the server or proxy,
   and outbound DNS to the two public resolvers (see
   [Network requirements](#network-requirements)); in most estates the
   agent is already managed by its own role.
7. Optional verification task: the `--dry-run` command from
   [Validation](#validation) as a `command` task with
   `changed_when: false`.
8. No handlers needed on upgrades: `Type=oneshot` services pick up the
   new binary at the next timer firing. Env-file or unit changes only
   need `daemon_reload` (units) — nothing to restart.

Facts a playbook must parameterize: the version pin, the two credential
values, `ZABBIX_HOSTNAME` and `CERTINEXT_VALIDATED_BY` per environment,
any other `ZABBIX_*` overrides, and the three timer schedules.

Run the pusher on **exactly one machine per environment**. Each
environment's Zabbix host has a fixed name
([ADR 0013](adr/0013-one-zabbix-host-per-certinext-environment.md)), so
two machines in the same inventory group would both push to it. Its agent
interface would also cover only one of them. Under 0.1.x, host names
defaulted to each machine's FQDN, which hid this.

Out of playbook scope (one-time, done in the Zabbix UI): importing
`templates/template_certinext/7.0/template_certinext.yaml`, creating one
host per environment with its agent interface and host macros, and linking
**CertiNext DCV by Zabbix trapper** to each — see
[One-time Zabbix server setup](#one-time-zabbix-server-setup-not-a-playbook-task).
