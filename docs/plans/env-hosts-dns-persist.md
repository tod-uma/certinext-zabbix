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
      **Corrected in session 3 (R1):** scope `'1'` was inverted (7.0 API: `0` = error matches, `1` = doesn't),
      so it is now `'0'`, with an explicit `error_handler_params: ''`. See *Session 3*.
- [x] **Session 2R — external review R1: package and hand over** (pusher + template + tests; see *External review
  checkpoints*). Packaging only: it ends when the bundle or brief is handed over. **Recommended.** 2026-11-18
  - [x] revision pinned: `73bab0b` (pipeline 56328 green), base `17809f4`
  - [x] briefs written, every deliberate decision re-verified against source at `73bab0b`:
    `review/REVIEW-BRIEF-codex-R1.md` (agentic) and `review/REVIEW-BRIEF-paste-R1.md` (paste-only, generated
    from the Codex brief: only *Revision* and *Output format* differ). Bundle:
    `review/certinext-zabbix-R1-review-bundle.md` (243 KB, 11 files, 0 omitted; line numbers spot-checked)
  - [x] outgoing artifacts swept (gitleaks clean on the scope and on the bundle; the only grep hits are the internal
    GitLab URL, already on the public mirror); Tod chose **both** reviewers (Codex + a paste-only one).
    Table rows filled at hand-off; the paste-only service and both products' self-IDs are confirmed in session 3
- [ ] **Session 3 — docs + ship** (steps 4, 5 repo side: ADRs 0013/0014, docs, MR, 0.2.0 pre-release tag)
  - [x] starts by processing R1's findings (fresh session; re-derive each against source), 2026-11-18. Both
    findings files name `73bab0b`. Re-derived against the [7.0 item object API][item-api] (preprocessing type
    26, `error_handler_params`), the [7.0 template export schema][export-schema], the plan's recorded agent2
    test, and the installed certinext 1.4.0rc5 `Domain.get_list()`:

    | Finding | Verdict | Evidence / action |
    | --- | --- | --- |
    | Codex 1: scope `'1'` selects the opposite errors | **Real** | API: `0` = matches, `1` = doesn't match. A missing record stayed unsupported; a timeout raised "record missing". Fixed `41765c0` |
    | Codex 2 / Gemini 1: `error_handler_params` missing | **Plausible, adopted** | API marks it required for a custom value; the schema lists no default. Explicit `''` costs nothing; Verification 3 settles it. `41765c0` |
    | Codex 3 (risk): discovery only as complete as the domain list | **Real, narrowed** | `get_list()` raises on non-2xx, but a short page returned early ends paging silently. Tod chose detection: `certinext.dcv.persist_apexes` + "apex count dropped" (`last < max(4d)`, WARNING, manual close). `8a3c3c5` |
    | Gemini 2: `CHECK_NOT_SUPPORTED` takes only a pattern, drop `'1'` | **Wrong** | API defines `<scope>\n<pattern>`; removing the scope would make the pattern the scope |
    | Gemini 3 (risk): tests don't check LLD macros | **Real (test gap)** | New test: every `{#…}` under the rule is a column the pusher sends. The predicted runtime symptom is unverified. `5998084` |
    | Gemini 4 (risk): agent may strip TXT quotes | **Wrong** | Recorded fact: agent2 7.0.31 on the pusher host returns `<name> TXT "<value>"`, quotes included. Verification 5 re-confirms |
    | Gemini 5 (style): `enabled_lifetime_type` may be misnamed | **Wrong** | The 7.0 export schema lists `enabled_lifetime_type` with `DISABLE_NEVER` |

    - Codex's extra notes, not findings: "record missing" and "lookup failing" can both be open (intended; they
      have different causes); the stale `nodata()` window for freshly discovered items needs the live check in
      Verification 3. Gemini's "unverified items" need no action.
    - 148 tests pass (3 new template tests, 1 pusher test, 2 CLI assertions), each mutation-checked; ruff, mypy,
      pyright clean. The three commits carry `External-review:` trailers.
    - **For step 4 / ADR 0014:** document the apex-count item and trigger, and why its window is 4 days (the
      rule's 3-day lifetime plus one daily run). It does not catch one apex removed and another added in the
      same run.
  - [x] docs and ADRs (step 4), 2026-11-18: `5fd865a` (ADRs 0013/0014; 0012 marked superseded, 0009 amended
    note, IDEA-005 answered), `10d2e55` (template README rewrite, `docs/deployment.md` incl. *Upgrading from
    0.1.x*, examples), `b8361d1` (README, llms.txt, AGENTS/CLAUDE key-sync + remote `origin`, IDEA-007,
    IDEA-002/004 notes). **As built:**
    - **The upgrade section leads with in-place** (Tod's choice): re-import over the existing template with
      *Delete missing*; the kept prod UUIDs should rename the existing host's items to bare keys and keep
      history, so that host becomes the prod host and only sandbox gets a new host. **Unverified**: the doc
      says so and tells the operator how to check; Verification 3 settles it. Fresh hosts are documented as
      the alternative. Step 5's Tod list below is rewritten to match.
    - Fixed pre-existing drift while there: the deployment checklist said four unit files / two timers (it has
      been six / three since the hourly order-health timer, #8).
    - The README screenshot is captioned as a 0.1.x view, not retaken (needs a live 0.2.0 host).
    - IDEA-007 coordinating issue: #11 (opened 2026-11-18).
  - [x] ends by packaging R2 and handing it over, 2026-11-18. **Tod chose the sysadmin-ansible workspace as
    the R2 reviewer** (see *R2* below): brief posted as a comment on Ansible/tower-projects#4 (local copy
    `review/REVIEW-BRIEF-ansible-R2.md`), pinned at `e8802f9` (pipeline for `2380008` green; `e8802f9` adds only
    the IDEA-007 issue link). Findings come back as a reply on #4; Tod then prompts session 3R here.
- [ ] **Session 3R — process R2 findings, then ship** (MR, merge, 0.2.0 pre-release tag). Skipped, with session 3
  doing the shipping, if Tod drops R2.
- [ ] **Session 4 — UMS cut-over, with Tod** (step 5 Tod's list, Verification 3–5)
- [ ] **Session 5 — observation** (Follow-ups; runs any time after 2026-11-20 ~13:41Z)

### Cold-start protocol

A fresh session reads **this document only**, plus the files its row in the session table names. It does not
re-read the earlier sessions' files, redo the research under *Context*, or reopen the decisions under *Design*.
These were settled after a Gemini review and a Codex review. Each session ends with `/wrap-up`: tick the
checklist above, commit, push, then `bash ~/.claude/scripts/ci-wait.sh`. The next session's opening prompt is
just "continue certinext-zabbix env-hosts plan, session N". N can be `2R` or `3R`: those are the external-review
hand-off sessions. A review session packages a bundle *or* processes findings, never both, because a session that
wrote the brief has already formed opinions about what the reviewer should have found.

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
| 2R | Review R1: package | R1 (pusher, template, tests) | sessions 1–2 on the branch | bundle or brief handed to the reviewer; table row filled in; **stop** | small |
| 3 | Docs (+ R1 findings, + R2 package) | 4, R1 findings, R2 | R1's findings file; sessions 1–2 on the branch | R1 findings triaged and fixes pushed; ADRs 0013/0014 and README/deployment/upgrade docs written; R2 handed over, or dropped | medium |
| 3R | Review R2: process, then ship | R2 findings, 5 (repo side) | R2's findings file | R2 findings triaged and fixed; CI green; MR merged; 0.2.0 pre-release tagged and verified | small to medium |
| 4 | UMS cut-over (with Tod) | 5 (Tod's list), Verification 3–5 | the released version | hosts + macros set; deploy done; MCP checks pass; negative tests run | mostly live checks |
| 5 | Observation | Follow-ups | 2026-11-20 after 13:41Z | `dcvStatus` behaviour after lapse recorded in the ADR/issue | short |

**Session 3 has to come after session 2,** because the docs describe the final template. Session 1 must finish
first. Sessions 4 and 5 are independent: if 2026-11-20 arrives before session 4, run session 5 on its own. Its
finding may change the `unverified` metric, so if it lands in time it gets folded back into this plan as an
extra step before session 3.

## External review checkpoints

Two checkpoints, placed where an outside reader adds the most before the next step makes a mistake expensive.
The earlier Gemini and Codex reviews covered the *design*; nothing yet has read the *build*. Mechanics, the
gotchas and the findings-processing method are in the `external-review` skill: load it at 2R and 3R rather than
working from this section alone.

<details>
<summary>Why these two places, and why not others?</summary>

- **R1 sits between the template and the docs.** The template is hand-written and has never been imported (its
  export constants are listed as unverified under *Session 2 — As built*), and the trigger expressions carry
  quoting, `or`-joined `nodata()` and dependency subtleties. A defect found after the docs are written means
  rewriting prose as well as YAML. R1 is also the cheapest moment: the diff is entirely local and self-contained.
- **R2 sits between the docs and the MR/tag.** What an operator will actually follow is the 0.1.x → 0.2.0 upgrade
  section, and the author is the worst reader of it. A cold read for missing steps is worth having before the tag,
  because session 4 follows it live with Tod.
- **No checkpoint in session 4.** That session's evidence is the live import (Verification 3) and the live
  alerts (4–5), which beat any reviewer's opinion. If the import shows a constant was wrong, fix it and re-import:
  exporting the live template and diffing it against the file is a stronger check than asking another model.
- **No checkpoint in session 5.** It records an observation, not a design.
- **Cost, stated plainly:** each checkpoint adds a hand-over round trip whose turnaround we don't control, and
  Apple distrusts the emSign G1 root on **2026-11-30**; session 4 should not slip past that for the sake of a
  review. R1 is recommended. R2 is optional and the first thing to drop if time is short.
</details>

### R1 — build review (session 2R packages; session 3 processes)

- **Revision:** the branch tip after session 2 (`b618da7` at the time of writing; re-pin at packaging). Diff scope
  against `main`: `certinext_zabbix/`, `templates/`, `tests/`, `docs/plans/` and `docs/adr/`.
- **Reviewers:** an agentic reviewer working in this tree (the Codex-style path: no bundle; the brief pins the SHA
  and tells it to read via `git show <SHA>:<path>`), and, if wanted, a paste-only reviewer (Gemini or ChatGPT) on
  a bundle from `make-review-bundle.py --path certinext_zabbix --path templates --path tests`. Two reviewers
  agreeing is the useful signal: the earlier rounds' reviewers converged on the same defects.
- **Look for, in priority order:**
  1. Zabbix 7.0 import correctness of the hand-written YAML: the export constants (`CHECK_NOT_SUPPORTED`
     parameters, `error_handler: CUSTOM_VALUE`, `enabled_lifetime_type: DISABLE_NEVER`, `lifetime`), canonical
     ordering (a mis-ordered file reports `changed` on every import), and trigger prototypes that depend on other
     prototypes.
  2. Trigger semantics: the `find(…,#3,"like","\"…\"")` quoting with a context macro, the three-way `or` of
     `nodata()`, `last()` gates, behaviour when items are unsupported or freshly discovered, and whether
     "lookup failing" and "record missing" can both fire.
  3. Pusher: that discovery can never be sent partial, the `get_dcv()` failure path and its exit code, what
     `token_domains` counts, and the misroute guard.
  4. Whether `tests/test_template.py` tests what its names claim (it was mutation-checked three ways; a reviewer
     may find a fourth).
- **Skip the linters:** `ruff`, `mypy`, `pyright` and `pytest` (144 tests) are green, CI pipeline 56328.
- **Deliberate decisions that look like defects** (each needs the reason and an invitation to argue; re-verify
  each against source at packaging time): bare keys with no `[env]` (the trapper doesn't expand macros in keys);
  one host per environment (a shared rule would need a merge coordinator); no freshness trigger on the discovery
  rule (the `dcv.expiring` `nodata()` already covers it); `{$CERTINEXT.ENV}` defaults to `prod`;
  `{$CERTINEXT.PERSIST.EXPECTED}` defaults to `CONFIGURE-ME` and fails closed; the persist INFO trigger is
  lapse-only with a literal `<=0`; `orders.expiring` has no trigger (ADR 0008); the template name is unchanged
  although it now has agent prototypes.
- **Exit:** a findings file, with the reviewer's self-ID block, on disk or pasted back and the SHA it names
  checked against the pinned one. Session 3 triages it (real / wrong, with the evidence / stylistic), fixes what
  survived, and credits it with an `External-review:` trailer per finding.

### R2 — operator-doc cold read (session 3 packages; session 3R processes)

- **Revision:** the branch tip once ADRs 0013/0014 and the docs are written, before the MR.
- **Scope:** the README and the template README, `docs/deployment.md` including the 0.1.x → 0.2.0 upgrade
  section, ADRs 0013/0014, and the template YAML for reference.
- **Look for:** steps missing from the upgrade (re-import, relink, history restarting, macro moves, the
  `ZABBIX_HOSTNAME` per-run setting); contradictions between the docs and the template; anything an operator
  with only these files could not do; ADR claims that the template doesn't bear out.
- **Optional:** Tod decides at the end of session 3. Dropped → session 3 opens the MR and tags, and 3R doesn't happen.
- **As run (2026-11-18, Tod's choice):** the reviewer is the **sysadmin-ansible workspace** (Claude Code on WSL),
  the docs' real consumer, rather than an outside model. It works in three steps: report the deployed state from
  tower-projects, critique the docs *before* drafting, then draft the tower-projects changes on a branch (no
  run, no deploy) plus a session-4 walkthrough from the docs alone. **Known weakness:** it knows UMS, so it can
  fill a doc gap from local knowledge without noticing; the brief makes it list every such fill, and session 3R
  treats each as a finding. Not independent of this model family, so it does not replace session 4's live
  evidence.

### Rules for both checkpoints

- **Ask Tod before sending anything.** Name the service and what is in the bundle. Sending is publication, even
  though the repo already mirrors publicly: this plan and the sessions' as-built notes were written for a public
  mirror, but the outgoing artifact is swept again (`gitleaks`, plus a grep for internal hostnames, the prod
  apex, order numbers and IPs), and every hit is accounted for out loud.
- **Name any gitignored file that holds real secrets by path** in an agentic reviewer's brief. CertiNext
  credentials live in the OS keyring, but run `git status --ignored` at packaging time and list anything else.
  `review/` is already in `.git/info/exclude`: put briefs and findings there, named per reviewer and round
  (`review/REVIEW-BRIEF-<reviewer>-R1.md`, `review/REVIEW-<reviewer>-R1.md`), because a generic name overwrites the
  previous round with no git copy.
- **Build the hand-over prompt from the brief just written**, naming that brief, the output file and the pinned
  SHA; check each against `review/`. A prompt copied forward from an earlier round once re-reviewed an old commit.
- **On return, check the findings file's `Reviewed:` SHA first.** Re-derive every finding against source: the
  failure modes seen so far are fabricated premises and real findings with a fabricated supporting detail.
- **Record the hand-off half of the attribution** while it's fresh (which service and product Tod pasted into,
  and when), because the reviewer's self-report is unreliable:

| Checkpoint | Service / product | Date | Pinned SHA | Findings file | Processed in |
|---|---|---|---|---|---|
| R1 (agentic) | Codex coding agent (OpenAI); self-ID "GPT-6, exact model version unknown" | 2026-11-18 | `73bab0b` | `review/REVIEW-codex-R1.md` | session 3 |
| R1 (paste-only) | Gemini web app, Gemini 3 Pro (per Tod; self-ID just "Gemini") | 2026-11-18 | `73bab0b` | `review/REVIEW-gemini-R1.pdf` (the `.md` is truncated mid-finding 2) | session 3 |
| R2 | Claude Code, sysadmin-ansible workspace (WSL); model self-ID in its reply | 2026-11-18 | `e8802f9` | reply comment on Ansible/tower-projects#4 | session 3R |

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
  - the metrics table (including `certinext.dcv.persist_apexes`, added in session 3)
  - **an upgrade section from 0.1.x** (re-import, relink, history restarts, macro moves)
- `AGENTS.md` / `CLAUDE.md` key-sync note.
- Wishlist **IDEA-007**: token-method record check while DCV is pending.

### 5. Ship

CI green → `/mr` → merge → release the 0.2.0 pre-release.

**Tod's UMS cut-over (session 4)**, revised in session 3 to the in-place path (Tod's choice; follows
`docs/deployment.md` *Upgrading from 0.1.x*, which is also what R2 would review):

1. Re-import the template over the existing one (*Update existing* + *Delete missing* for items, triggers,
   discovery rules). Check *Latest data* on the current pusher host: do the bare-key items carry pre-upgrade
   history? Record the answer here (it confirms or refutes the doc's "expected" claim).
2. The current pusher host becomes the prod host: `ENV` stays at the `prod` default; it already has an agent
   interface; set `PERSIST.EXPECTED` (the full value, unquoted). `SENDER.ALLOWED` and the 3/2/1/0 overrides
   carry over.
3. Create the sandbox logical host (pusher machine's agent IP, same proxy as today), macros `ENV=sandbox`,
   `PERSIST.EXPECTED` (sandbox value), `SENDER.ALLOWED`, and a higher `ORDER.DAYS_SINCE_ISSUED_MAX`.
4. tower-projects (ums.pki): new version, `ZABBIX_HOSTNAME` = the sandbox host on the sandbox units only.
5. Drop any `-9999` DCV overrides.

The fresh-hosts path (two new logical hosts, unlink and clear the old template) remains the fallback if step 1
shows the in-place rename misbehaving.

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
- [Zabbix 7.0 item object API (preprocessing parameters)][item-api] ·
  [template export/import schema][export-schema]

[dns-persist]: https://datatracker.ietf.org/doc/draft-ietf-acme-dns-persist/
[agent-keys]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/zabbix_agent
[trapper]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/trapper
[lld]: https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery
[preproc]: https://www.zabbix.com/documentation/7.0/en/manual/config/items/preprocessing
[macros-loc]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/macros/supported_by_location
[fn-string]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/functions/string
[fn-history]: https://www.zabbix.com/documentation/7.0/en/manual/appendix/functions/history
[item-api]: https://www.zabbix.com/documentation/7.0/en/manual/api/reference/item/object
[export-schema]: https://www.zabbix.com/documentation/7.0/en/manual/xml_export_import/templates
