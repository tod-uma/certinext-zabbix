---
status: in-progress
depends-on: []
implements-adr: [0013, 0014]   # both written in session 3; 0014 supersedes 0012
---

# One Zabbix host per CertiNext environment + dns-persist TXT checks (0.2.0)

Approved 2026-11-18. This in-repo copy **supersedes** the private draft the plan was written in; it is the
only live copy. Branch: `feat/env-hosts-dns-persist`. Version: `0.2.0a1` (breaking: every item key changes).

## Status

- [x] **Session 1 — pre-work + pusher** (steps 0, 1, pusher half of 3), 2026-11-18
  - [x] keyring extra + ADR 0012 landed on main (MR !19, merged 2026-11-18)
  - [x] `review/` in `.git/info/exclude`
  - [x] branch `feat/env-hosts-dns-persist`, version `0.2.0a1`
  - [x] this plan committed
  - [x] pusher code (step 1), commit `fd7d24e`
  - [x] pusher tests (step 3, first two bullets): 120 passed locally; ruff, mypy, pyright clean
  - [ ] Verification 2 (live prod `--dry-run`): not run in session 1; Tod's call, can run any time before
    session 4
- [x] **Session 2 — template** (step 2, `tests/test_template.py`, IDEA-003 trigger), 2026-11-18
  - [x] template rewritten single-environment (prod UUIDs kept, sandbox block deleted, 13 new UUIDs)
  - [x] `tests/test_template.py`: 24 tests, mutation-checked (renamed key, dangling dependency, undeclared
    macro each fail it); `pyyaml` + `types-PyYAML` added to the `dev` extra
  - [x] IDEA-003: baseline measured, Tod chose **4** days; idea marked Accepted
  - [x] 144 tests pass; ruff, mypy, pyright clean
  - **As built — read before session 3** (these differ from or sharpen *Design*):
    - **No freshness trigger on the discovery rule.** A trigger expression can't reference an LLD rule's key
      (to be confirmed at import, Verification 3). It is redundant anyway: the pusher never sends expiry
      metrics without discovery, so the existing `nodata()` on `dcv.expiring` (26h) fires whenever discovery is
      stale. That trigger's description now says so.
    - **"Lookup failing" covers all three lookups** (`nodata` on local `or` resolver 1 `or` resolver 2), not
      just the local one: Verification 5's resolver-1 negative test needs it. `{$CERTINEXT.DNS.NODATA}` = 3h,
      so it must stay above `DNS.INTERVAL.EXTERNAL`.
    - **"Not visible publicly" is one trigger prototype** (`find(R1,#2)=0 or find(R2,#2)=0`), on the rule,
      depending on "record missing".
    - **`{$CERTINEXT.PERSIST.EXPECTED}` holds the value without its quotes**; the triggers add them. Triggers
      use the context form `{$CERTINEXT.PERSIST.EXPECTED:{#APEX}}` (unquoted context), which is what
      Verification 5's per-apex override needs. *Step 5's "full quoted value" means the full value, unquoted.*
    - **`{$CERTINEXT.ENV}` defaults to `prod`**, so prod-only installs need no macro; a sandbox host left at
      the default raises the misroute HIGH on its first run.
    - **`{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}` = `4`**, a plain number (the item is in days; `4d` would
      expand to seconds). The sandbox host will want an override (low issuance volume).
    - The persist INFO trigger uses a literal `<=0`: it means "lapsed", not a tunable threshold.
    - Days-left gradient moved to the top-level `triggers:` list (multi-item, gated on `token_domains>0`),
      keeping its UUIDs. Template name unchanged (`CertiNext DCV by Zabbix trapper`), although it now has
      agent prototypes too; renaming means editing every expression, so it wasn't done here. Raise it in session 3 if wanted.
    - Item-prototype `type` is omitted (the default is the passive Zabbix agent). The preprocessing step is
      `CHECK_NOT_SUPPORTED` with parameters `['1', '^Cannot perform DNS query']` and `error_handler:
      CUSTOM_VALUE` with no params (empty string). The rule uses `lifetime: 3d` and
      `enabled_lifetime_type: DISABLE_NEVER`. These export constants come from memory of the 7.0 schema and
      have **not** been through an import yet: Verification 3 must confirm them.
- [ ] **Session 3 — docs + ship** (steps 4, 5 repo side: ADRs 0013/0014, docs, MR, 0.2.0 pre-release tag)
- [ ] **Session 4 — UMS cut-over, with Tod** (step 5 Tod's list, Verification 3–5)
- [ ] **Session 5 — observation** (Follow-ups; runs any time after 2026-11-20 ~13:41Z)

### Cold-start protocol

A fresh session reads **this document only**, plus the files its row in the session table names. It does not
re-read the earlier sessions' files, redo the research under *Context*, or reopen the decisions under *Design*.
These were settled after a Gemini review and a Codex review. Each session ends with `/wrap-up`: tick the
checklist above, commit, push, then `bash ~/.claude/scripts/ci-wait.sh`. The next session's opening prompt is
just "continue certinext-zabbix env-hosts plan, session N".

## Context

- SSL.com DCV via dns-persist ([draft-ietf-acme-dns-persist][dns-persist]) looks up `_validation-persist.<apex>`
  **at validation/issuance time**. CertiNext's `validTill` is a reuse clock, not a dependency. ADR 0012's
  days-left triggers (3/2/1/0) fire every ~10-day cycle: prod and sandbox WARNINGs have been live since
  2026-11-18 11:30Z.
- Nothing monitors the real dependency, the TXT record in public DNS.
- Tod's requirements:
  - Zabbix (not the pusher) does the lookups.
  - Both environments can be monitored; sandbox is optional.
  - Each institution is alerted only for the DCV method it uses.
  - Clear keys, with no tricks such as a trailing dot or a dummy parameter.
- **Verified facts:**
  - **agent2 7.0.31 on the pusher host, passive items** ([`net.dns.record`][agent-keys]):
    - `net.dns.record[,_validation-persist.<apex>,TXT]` returns one line per value, `<name> TXT "<value>"`.
    - 1.1.1.1 and 8.8.8.8 agree with the local resolver.
    - A missing name gives `Cannot perform DNS query.`
    - A dead resolver gives `Cannot fetch data: … i/o timeout.` These two can be told apart.
  - **Zabbix 7.0 docs ([trapper items][trapper]):** *"The Zabbix trapper process does not expand macros used
    in the item key"*. So trapper keys cannot carry `{$CERTINEXT.ENV}`, and the `[prod]`/`[sandbox]` key
    parameter is dropped instead.

## Design

<details>
<summary>Why one host per environment instead of one host with both environments?</summary>

- Chosen 2026-11-18 after the Gemini and Codex reviews.
- A shared discovery rule fed by two pusher runs would need a snapshot-merge coordinator, because each
  environment's run would otherwise send a partial apex list. If that coordinator fails, LLD's lost-resource
  handling deletes the other environment's items without any warning.
- Two hosts give each environment its own discovery rule, and that rule only ever receives a complete list.
- The `[env]` key parameter can't simply be macro-driven, because the trapper doesn't expand macros in keys
  (quote above). Dropping it is cleaner than keeping a hard-coded duplicate block.
- Rejected: a trailing-dot key variant and a dummy timeout parameter to make keys unique. Both work, but they
  are tricks a reader can't decode (Tod's "clear keys" requirement). Also rejected: a UserParameter wrapper,
  and doing DNS in the pusher (requirement: Zabbix does the lookups).
</details>

1. **One host per environment.** Two logical hosts (e.g. `CertiNext prod` and `CertiNext sandbox`):
   - Both have an agent interface pointing at the pusher machine (passive checks don't care about the host
     name), and the same "monitored by" proxy.
   - Each host is linked to the one template. Host macro `{$CERTINEXT.ENV}` = `prod` / `sandbox`.
   - Each pusher run targets its host via the existing `--zabbix-host` / `ZABBIX_HOSTNAME`
     (`zabbix_push_cli.py`), so no routing code is needed.
   - Prod-only installs just don't create the sandbox host.
2. **Single-environment template.**
   - Item keys lose the `[env]` parameter (e.g. `certinext.dcv.min_days_left`), and the duplicated sandbox
     block (~400 lines) goes away.
   - Names and tags use `{$CERTINEXT.ENV}`, which is supported in names, tags and triggers
     ([macros by location][macros-loc]), just not in trapper keys.
   - **Misroute guard:** the pusher sends a TEXT `certinext.env` value every run, and a HIGH trigger fires on
     `last(/T/certinext.env)<>"{$CERTINEXT.ENV}"`. This replaces the key-mismatch rejection we lose.
3. **dns-persist discovery** ([low-level discovery][lld]).
   - The pusher sends `certinext.dcv.persist.discovery` = `[{"{#APEX}": …}]` for DNS-PERSIST domains. Method
     comes from `Domain.get_dcv()`, read on the daily expiry run. It never sends a partial list.
   - **Item prototypes** (ZABBIX_PASSIVE, TEXT, history 7d):
     - `net.dns.record[,_validation-persist.{#APEX},TXT]` every `{$CERTINEXT.DNS.INTERVAL.LOCAL}` (5m)
     - `net.dns.record[{$CERTINEXT.DNS.RESOLVER.1},…]` and `…[{$CERTINEXT.DNS.RESOLVER.2},…]` every
       `{$CERTINEXT.DNS.INTERVAL.EXTERNAL}` (1h). Defaults are 1.1.1.1 / 8.8.8.8.
   - **Preprocessing** ([preprocessing][preproc]): "Check for not supported value", *error matches*
     `^Cannot perform DNS query` → set value to `""`. Other errors, such as a resolver timeout, stay
     unsupported.
   - **Trigger prototypes** ([`find`][fn-string], [`nodata`][fn-history]):
     - Record missing, local resolver, HIGH: `find(…,#3,"like","\"{$CERTINEXT.PERSIST.EXPECTED}\"")=0`.
       Matches the exact quoted value; `#3` ≈ 15 min debounce.
     - Not visible from an external resolver, AVERAGE: `#2`, depends on the local trigger.
     - Lookup failing (resolver down), AVERAGE: `nodata(…,{$CERTINEXT.DNS.NODATA})`.
   - **Discovery rule:** trapper, `allowed_hosts {$CERTINEXT.SENDER.ALLOWED}`; lost resources deleted after
     3d, never disabled; plus a nodata trigger on the rule's key (freshness).
   - **Macro default:** `{$CERTINEXT.PERSIST.EXPECTED}` defaults to `CONFIGURE-ME`, so it fails closed. The
     real UMS value goes **only in host macros**, because the repo mirrors publicly.

   <details>
   <summary>Why LLD here, when ADR 0011 rejected it for the CA dimension?</summary>

   - The set of apexes is data (it changes as institutions move to dns-persist), and each one needs its own
     *agent* check. That is what LLD exists for. The CA dimension is a fixed small set, so static keys suit it.
   - LLD's main risk, items being deleted after a partial list, is handled by the "never send a partial list"
     rule and the 3-day lost-resource window. ADR 0014 records this in full (session 3).
   </details>

4. **Alerting by DCV method, automatically.**
   - `dcv.expiring` / `dcv.min_days_left` count **token-method** domains only.
   - New `dcv.token_domains` gates the days-left triggers (`and last(token_domains)>0`). This also fixes the
     stale `last()` when `min_days_left` isn't sent.
   - New `dcv.persist_min_days_left` gets one **INFORMATION** trigger at `<= 0`: "reuse period lapsed — next
     issuance re-checks DNS". This is lapse-only, as Tod chose.
   - Persist items exist only if persist domains exist.

## Sessions (context boundaries)

The splits fall where the set of files touched changes completely, so no session needs the previous one's file
reads.

| # | Session | Steps | Starts from | Done when (exit check) | Rough size |
|---|---|---|---|---|---|
| 1 | Pre-work + pusher | 0, 1, pusher half of 3 | this plan | keyring MR merged; branch exists; plan committed; pusher code + its tests green in CI | largest (Python + 2 test files) |
| 2 | Template | 2, `test_template.py`, IDEA-003 trigger | session 1's commit; reads only the YAML + `KEY_*` constants | YAML rewritten; `test_template.py` green; CI green | YAML-heavy, little else |
| 3 | Docs + ship | 4, 5 (repo side) | sessions 1–2 on the branch | ADRs 0013/0014, README/deployment/upgrade docs; MR merged; 0.2.0 pre-release tagged and verified | medium |
| 4 | UMS cut-over (with Tod) | 5 (Tod's list), Verification 3–5 | the released version | hosts + macros set; deploy done; MCP checks pass; negative tests run | mostly live checks |
| 5 | Observation | Follow-ups | 2026-11-20 after 13:41Z | `dcvStatus` behaviour after lapse recorded in the ADR/issue | short |

**Session 3 has to come after session 2,** because the docs describe the final template. Session 1 must finish
first. Sessions 4 and 5 are independent: if 2026-11-20 arrives before session 4, run session 5 on its own. Its
finding may change the `unverified` metric, so if it lands in time it gets folded back into this plan as an
extra step before session 3.

## Steps

### 0. Pre-work — done 2026-11-18

- Landed `feat/keyring-extra-dcv-threshold-adr` (keyring extra + ADR 0012) via MR !19, so ADR 0012 is
  *superseded* on main instead of disappearing.
- Added `review/` to `.git/info/exclude` (the Codex hand-off file lives there untracked).
- Branched `feat/env-hosts-dns-persist`, bumped to `0.2.0a1` (alpha: the key schema keeps changing through
  sessions 1–3 and hasn't run live).
- Committed this plan.

### 1. Pusher (`certinext_zabbix/zabbix_push.py`, `zabbix_push_cli.py`)

- **Keys:**
  - ~~`item_key()` returns the bare key~~ **As built:** `item_key()` is removed and the `collect_*` functions
    lost their `env` parameter; metrics use the `KEY_*` constants directly. Keeping a function that ignores its
    argument would mislead callers. `env` stays in the CLI for logs, locks and `certinext.env`.
  - New constants next to `KEY_*`: `KEY_ENV`, `KEY_PERSIST_DISCOVERY`, `KEY_TOKEN_DOMAINS`,
    `KEY_PERSIST_MIN_DAYS_LEFT`. Also `DCV_METHOD_PERSIST = "DNS-PERSIST"` and `MetricValue = int | float | str`.
  - **As built, for session 2:** `dcv.token_domains` counts verified token domains *with a known expiry* —
    exactly the set `min_days_left` is computed over — so the `>0` gate matches whether `min_days_left` was
    sent. On a `get_dcv()` failure, discovery **and** all expiry metrics are withheld (exit 1). A detail-refresh
    failure withholds only the expiry metrics; discovery still goes out, because the method split was complete.
- **New helpers:**
  - `fetch_dcv_methods(domains) -> (persist, token, failed)`, with retries shaped like `refresh_domain()` and
    the method from `DcvInfo.method`.
  - `persist_discovery_value(persist) -> str`.
- **Changed functions:**
  - `collect_expiry_metrics()`: applied separately to token and to persist domains (persist →
    `persist_min_days_left` only); adds `token_domains`.
  - `push_metrics()`: values typed `int | float | str`.
- **CLI:**
  - Every run adds `certinext.env`.
  - The expiry path calls `fetch_dcv_methods` on the scoped domain list, refreshes verified domains of both
    kinds, and sends discovery **only if no `get_dcv()` failed** (otherwise log a warning and follow the
    existing per-domain-failure exit convention).

### 2. Template (`templates/template_certinext/7.0/template_certinext.yaml`)

Hand-written; new UUIDs via `uuid.uuid4().hex`.

- Delete the sandbox block. Rename the prod items and triggers to bare keys, with `{$CERTINEXT.ENV}` in names
  and an `env: {$CERTINEXT.ENV}` tag.
- Add:
  - the `certinext.env` item + misroute trigger
  - the `token_domains` and `persist_min_days_left` items + the INFO trigger
  - the gate on the days-left triggers
  - the discovery rule, 3 item prototypes, 3 trigger prototypes, and the freshness trigger
  - new macros (`ENV`, `PERSIST.EXPECTED`, `DNS.RESOLVER.1/2`, `DNS.INTERVAL.LOCAL/EXTERNAL`, `DNS.NODATA`)
- Sandbox suppression (ADR 0009) moves from per-item `DISABLED` to an action filter on the `env` tag; document
  it.

### 3. Tests

- `tests/test_zabbix_push.py` (session 1):
  - bare keys
  - `fetch_dcv_methods` split and failure path
  - discovery JSON
  - token/persist expiry split, including `token_domains=0`
  - `str` values
- `tests/test_zabbix_push_cli_mocked.py` (session 1):
  - `certinext.env` is always sent
  - discovery is sent, or withheld after a `get_dcv()` failure
  - all-persist run → no `min_days_left`
  - update existing key assertions to bare keys (`TestValidatedBy`, `TestExpiryPath`, …)
- New `tests/test_template.py` (session 2): parse the YAML and assert every `KEY_*` exists as a trapper item or
  discovery key, plus the new macros. This closes the manual key-sync gap noted in `CLAUDE.md`.

### 4. Docs

- **ADR 0013**, "One Zabbix host per CertiNext environment". Records:
  - the trapper-macro doc quote, why the key parameter was dropped, and the misroute guard
  - the rejected alternatives: shared rule + merge coordinator, trailing dot, timeout, UserParameter,
    pusher-side DNS
  - an amendment to 0009
- **ADR 0014**, "dns-persist records checked by Zabbix agent; DCV alerts by method". Supersedes 0012; explains
  why LLD is fine here although 0011 rejected it for the CA dimension.
- **README / template README / `docs/deployment.md`:**
  - creating the two hosts (agent interface on the pusher machine, same proxy, host macros)
  - a per-run `ZABBIX_HOSTNAME` for the sandbox units in the systemd/cron/Windows examples
  - outbound DNS to the external resolvers
  - the metrics table
  - **an upgrade section from 0.1.x** (re-import, relink, history restarts, macro moves)
- `AGENTS.md` / `CLAUDE.md` key-sync note.
- Wishlist **IDEA-007**: token-method record check while DCV is pending.

### 5. Ship

CI green → `/mr` → merge → release the 0.2.0 pre-release.

**Tod's UMS cut-over (session 4):**

1. Create the two logical hosts, with the pusher host's agent IP and the same proxy as today.
2. Set the host macros: `ENV`, `PERSIST.EXPECTED` (full quoted value, prod and sandbox respectively),
   `SENDER.ALLOWED`.
3. Re-import the template; unlink and clear the old template from the current pusher host.
4. tower-projects (ums.pki): new version, `ZABBIX_HOSTNAME` per environment's units.
5. Drop any `-9999` DCV overrides.

## Verification

1. **Unit checks:** `uv run --extra dev pytest`, `ruff check`, `mypy`.
2. **Dry run:** `uv run --extra keyring certinext-zabbix-push --dry-run --expiry-days 14 --validated-by SSLCOM` →
   bare keys, `certinext.env=prod`, discovery JSON with the 21 apexes, `token_domains=0`, no `min_days_left`.
3. **After import:** a read-only Zabbix MCP check (`template_get`, `discoveryrule_get`, `itemprototype_get`,
   `triggerprototype_get`) confirms the structure. Confirm the preprocessing *error matches* format and the `\"`
   escaping in `find()` survived import.
4. **After the first real runs:**
   - the prod host has 63 DNS items (21 × 3) whose values contain the prod value
   - the sandbox host has its own set
   - no persist or misroute triggers are firing
   - the old days-left WARNINGs are gone
5. **Negative tests (Tod's call; live alerts):**
   - a host-macro context override `{$CERTINEXT.PERSIST.EXPECTED:…}` on one apex → HIGH within ~15 min → revert
   - send one sandbox run to the prod host (`--zabbix-host`) → misroute HIGH → revert
   - point `{$CERTINEXT.DNS.RESOLVER.1}` at 192.0.2.1 on the sandbox host → "lookup failing" fires, "record
     missing" does not → revert

## Wishlist riders (decided 2026-11-18)

- **IDEA-003, days-since-issued trigger: include it (session 2, template-only).** Its blocker was "no baseline".
  Prod `certinext.orders.days_since_issued` has about 3 months of trend data (≈2,100 hourly rows from
  2026-08-19). **The maximum has NOT been measured yet:** `trend_get` ignores `sortfield`, so the 5.7 d seen
  first was just the earliest rows. In session 2, pull the trends and compute the maximum yourself (page through
  `value_max` with `time_from`/`time_till`, take the max). Tod picks the value for one
  `{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}` macro and a WARNING `last()>` trigger. Mark IDEA-003 done.
- **IDEA-005, originator bucketing: docs only.** Its open question was "key parameter vs separate key names,
  since a parameter changes every existing key". This change drops `[env]`, so a future `[class]` parameter
  becomes additive. Record that in ADR 0013 and update IDEA-005's open question. Don't implement it: its
  baseline prerequisite (backlog cleared) isn't met.
- **IDEA-004, per-order LLD: docs only.** This plan builds the first discovery rule, the lost-resource rule and
  the "never send a partial list" rule, which were its main costs. Update its Effort section. Not now: prod has
  285 unissued orders, so it would create hundreds of items.
- **IDEA-002, community templates: docs only.** One single-environment template is closer to what upstream
  expects. Note it in the idea.
- **Not included:**
  - IDEA-006 (ruff format): coupled to certinext IDEA-013 by design.
  - IDEA-001 (credential files): belongs upstream in `certinext.cli_options`.

## Follow-ups (not this change)

- 2026-11-20 ~13:41Z: watch the prod org apex after its `validTill` lapses. If `dcvStatus` leaves VERIFIED, the
  `unverified` metric needs method-awareness too, and the INFO trigger shows whether revalidation is lazy.
- `orders.failed_recent` (prod) = 1 as of 2026-11-18: look at it separately.
- One prod apex carries a persist value for a second CA: ask who authorised it.

## References

- [draft-ietf-acme-dns-persist][dns-persist]
- [Zabbix 7.0 agent item keys (`net.dns.record`)][agent-keys]
- [Zabbix 7.0 trapper items][trapper]
- [Zabbix 7.0 low-level discovery][lld]
- [Zabbix 7.0 item value preprocessing][preproc]
- [Zabbix 7.0 macros supported by location][macros-loc]
- [Zabbix 7.0 string functions (`find`)][fn-string] · [history functions (`nodata`)][fn-history]

[dns-persist]: https://datatracker.ietf.org/doc/draft-ietf-acme-dns-persist/
[agent-keys]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/zabbix_agent
[trapper]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/trapper
[lld]: https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery
[preproc]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/preprocessing
[macros-loc]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/macros/supported_by_location
[fn-string]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/functions/string
[fn-history]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/functions/history
