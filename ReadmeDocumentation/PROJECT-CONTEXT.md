# Project Context: AI Agent-Augmented Salesforce Delivery Pipeline

**Purpose of this file:** paste this whole document into a new chat to restore full context without re-explaining anything.

**Last updated:** 6 October 2026 (night), **v8**, after:
- **v7 merged** (Steps 1–3: git_sync, PR tracking + ai-waiting, batching). User confirmed all v7 PRs merged.
- **CLAUDE-18** (feature: edit an existing registration) first run: Copilot stopped with 0 commits (0.46 credits) because `.github/copilot-instructions.md` counted 3 report files toward the 5-file limit and treated a required `isUpdateable()` check as an authorization change → **C15**. Fixed on `fix/agent-stop-rules` (rules v2 + invoke_copilot early stop `agent_no_changes` + review_gate empty-branch reject + `test_agent_stop.py` 11). Branch had to be re-pushed after an empty push (G16).
- **Dashboard local dates** (`fix/dashboard-local-dates`): month-to-date was missing evening runs (UTC grouping) → D1.
- **v7.1** (delivered, not yet confirmed merged/tested): `pr_tracker` writes `logs/pr-status.json`; dashboard shows merged/closed/open, step start/end/duration timeline, end-to-end + lead times, Where the time goes, Review queue; **live progress line** in PowerShell (`progress.py`, elapsed vs median typical, files edited/commits for the agent step). Tests: `test_dashboard_v71.py` 18.
- **v7.2** (delivered, not yet merged/tested): **Salesforce deploy tracking** (`deploy_tracker.py`): merge → `ai-merged`, stays In Review; deploy run followed via GitHub Actions API; success → `ai-deployed` + Jira **Done** (user decision: Done only after successful deploy); failure → `ai-deploy-failed` + alert; later successful deploy covers earlier; no run → alert; approval reminder; approval-wait and deploy times. `salesforce-deploy.yml`: removed `|| true`, summary/artifact `if: always()`, concurrency. Dashboard deploy states. Tests: `test_deploy_v72.py` 19. Branch `feat/v7.2-deploy-tracking` contains 7.1 too.
- All deliverables refreshed to **v8** (Impl. Guide Part 14, Troubleshooting 5.12 / 6.7 / Part 12, deck slide 15 "Merge to live | v8"). 93 issues; 158 offline tests.

**Where we stopped:** user went offline (late). To do when back: (1) merge `fix/agent-stop-rules` and `fix/dashboard-local-dates` if not merged; (2) add **Actions: Read-only** to the fine-grained token; (3) commit/push/merge `feat/v7.2-deploy-tracking` (Impl. Guide 14.8) and run the 4 test files; (4) re-run **CLAUDE-18** (labels → ai-ready only) and walk Impl. Guide 14.10 (merge → ai-merged/In Review → approve deploy → ai-deployed/Done); (5) then the v7 five-ticket test (13.11).

**The next chat should start at Section 16 ("Next steps"), item A1.**

### What changed in v8 (one-screen summary)

| # | Item | Status |
|---|---|---|
| C15 | Agent stopped with no changes (rules misread: report files counted; CRUD check seen as auth change) | Rules v2 (Section 0 pipeline mode, `AGENT-STOP:` first line, code-only limits incl. batch 8/350 10/450, CRUD/FLS required, features/batches), invoke_copilot `agent_no_changes`/`agent_uncommitted` (+reason to Jira, empty branch deleted, UTF-8 BOM log), review_gate rejects empty branch |
| D1 | Month-to-date < total spend | Local-date grouping, 30D default, period label on Total card |
| D2 | Merged shown as PR open | `pr-status.json` each poll; dashboard rebuilt after PR/deploy changes |
| D3 | Progress line glyphs/width | ASCII fallback, truncation, `WATCH_PROGRESS=false` |
| F1–F4 | Deploy tracking: token Actions read; In Review until deploy; deploy failed; no run | `deploy_tracker.py` |
| G16 / E11 | Empty branch pushed; Select-String regex | Procedure (git status between add/commit; `-SimpleMatch`) |
| Issues | 83 | **93** |

#### v8 design decisions
- **Fix the rules, not the pipeline** for C15: the agent behaved correctly per repo rules; keep it strict on real permission changes.
- **Progress is honest:** no fake %; elapsed vs median typical, capped 99%, "longer than usual"; real git signal for the agent step; review gate untouched.
- **Done = live in Salesforce**; rejected: Done on merge (v7), new Jira status (admin work). Labels ai-merged / ai-deployed / ai-deploy-failed.
- **Match deploy run by merge commit**; a later successful run of main covers an earlier failed/missing one (whole force-app deploys). Failed is not terminal (re-run → Done). Track for DEPLOY_TRACK_DAYS.
- **Workflow fails honestly** (no `|| true`), summary/artifact always, deploys serialized.
- **Fail open:** Actions API 403 → one warning per session, watcher continues.

### What changed in v6 (one-screen summary)

| # | Finding | Status |
|---|---|---|
| R8 | PR #10 merged with no review | **Closed.** Timeline showed a manual merge by `pvellure-hash` (`auto_merge` null; no merge API call in `agent/`). Solo-owner fix: `environment: salesforce-deploy-gate` on the deploy job (PR #11) + environment Required reviewer = pvellure-hash + auto-merge off. Proven: deploy waited for "pvellure-hash approved". |
| C12 | Credits never captured | **Closed.** `--usage-output-file logs/<KEY>-usage.json` (PR #12); parser reads `totalNanoAiu / 1e9` (PR #14). CLAUDE-16 = 1.6703 credits, ~$0.0167, `credits_source: usage_file`. |
| C13 | First C12 parser looked for a key containing "credit"; real JSON has none | Fixed by PR #14 (found by testing before any real ticket) |
| C14 | GitHub "AI usage" page shows "No usage" | Enterprise-assigned seat: only Copilot settings → Usage (percentages, e.g. 23%) is visible; per-user breakdown needs the enterprise billing manager |
| G9 | PR 422 printed as FAIL | **Closed** (PR #13): `find_existing_pr(state="all")` + 2 s re-check on 422 |
| G10 | `checkout -b` failed on an existing name, commit landed on another branch, empty branch pushed; later "nothing to compare" because commit never ran | Procedure: check `git branch --show-current` + `git status` before committing; rename/cherry-pick to recover |
| G11 | **After a ticket, repo stays on `fix/<KEY>-...`; next ticket fails A2 "not on main" and would lack the unmerged previous fix** | **OPEN — highest priority** |
| E7 | GitHub Actions outage 5 Oct (from 19:11 UTC): "job was not acquired by Runner" | Wait, then Re-run all jobs and re-approve the deploy gate |
| E8 | PowerShell 5.1: `Out-File -Encoding utf8` adds BOM → Apex "Invalid identifier '﻿Id'"; `utf8NoBOM` not available; stray `\]`, `--`, `**` from copying | Use `[System.IO.File]::WriteAllText(path, $code, (New-Object System.Text.UTF8Encoding($false)))` |
| S11 | `sf apex run --file -` not supported | Write a file |
| S12 | Anonymous Apex → `LimitException: Can only throw this exception type from VisualForce or Aura context` | `AuraHandledException` can't be thrown from anonymous Apex; verify with test classes |
| S13 | Manual UI test misleading (Submit not clicked; wrong date; HTML5 `min` blocks input) | Test exact boundary, click Submit, trust Apex tests |
| Phase 2 | Live dashboard | **Built**: `generate_pipeline_dashboard.py` + `dashboard_template.html`, called from `watch_queue.refresh_dashboard()` (PR #17) → `logs/pipeline-dashboard.html` |

Issue total: **74** (65 + 9 new: C13, C14, E7, E8, S11, S12, S13, G10, G11).

---

### What changed in v7 (one-screen summary)

| # | Item | Status |
|---|---|---|
| G11 | Watcher left on fix branch; next ticket skipped and lacked unmerged fix | **Closed (Step 1)** — `git_sync.sync_base()` at startup, before and after every ticket |
| Flow | Watcher waited for nothing but could not continue | **v7:** after a PR or a wait, next poll in `WATCH_CONTINUE_SECONDS` (5) |
| PR lifecycle | Merge/close/conflict not reflected in Jira; dashboard only knew `pr_open` | **Step 2:** `pr_tracker.py` + `update_jira.on_pr_event()`; state in `logs/pr-state.json` (first run baselines old closed PRs silently) |
| Overlap | Two PRs on same file → conflict | **Step 2:** overlap guard in `prepare_fix.py` → `ai-waiting`, `logs/waiting.json`, `release_waiting()` |
| Dependencies | none | **Step 2:** `claim_ticket.py` checks Jira "is blocked by" (status category Done) → `ai-waiting` |
| Similar bugs | Separate runs + conflicting PRs | **Step 3:** `batching.py`; `routing.write_batch()`; `create_pr` title/table; members updated in Jira |
| Validation | 5 files / 200 lines for every run | **Step 3:** batch-aware: 2 tickets 8/350, 3+ 10/450 (caps); member keys accepted in commit messages |
| Issues | 74 | **83** (+G12–G15, E9, E10, J3–J5) |

#### v7 design decisions (and why)
- **Do not stack branches** on unmerged PRs: a rejected/changed PR A would poison B; reviewers must judge each PR alone.
- **Do not wait for merges**: one slow review would stall the queue overnight. **Do not auto-merge**: removes the human safety net.
- **Chosen:** independent branches from merged `main` + **overlap guard** (whole-file granularity, conservative) + **batching** for related bugs that are ready together.
- **Never discard work:** `sync_base()` refuses on dirty tree or local `main` ahead of origin; only fast-forwards.
- **GitHub catches up, not the laptop:** behind PRs use server-side **Update branch** (once per new main sha); Validate re-runs.
- **Report once:** `pr-state.json` remembers last action; reminders every `PR_REMIND_HOURS`.
- **Batch lead owns the run** (lowest key): all existing steps unchanged because branch starts `fix/<LEAD>-` and reports use lead key; `pr_tracker` reads every key from branch name.
- **Bugs only, max 3, tier +1**; features go alone (a feature overlapping a bug in the same cycle → one waits a cycle).
- **Failure memory:** `logs/batch-failed.json` (symmetric) so a failed pair is retried alone, never loops.
- **Fail open vs closed:** efficiency checks (overlap GitHub call, Jira link read) fail open with a warning; safety checks (dirty tree, diverged main, injection) fail closed.
- **Honest limit:** the terminal `y/n` review gate still pauses each run → truly unattended overnight needs the gate in the browser (Phase 3).

## 1. What this project is

An AI-assisted fix and feature pipeline for Salesforce codebases. **Design intent:** nothing is auto-merged or auto-deployed. **v6:** the ruleset still has 0 approvals (solo owner), but every real deploy now waits for a person to approve the `salesforce-deploy-gate` environment (R8 closed). The flow below is current as of v6.

```
Jira ticket (label ai-ready)
  │
watch_queue.py  ── startup hard gates: preflight.run_all() + security_checks.run_all()
  │               ── builds/refreshes code index (no AI)
  │               ── every poll: budget + daily-cap gates (claims nothing if closed)
claim_ticket.py → per-ticket re-check: git state + production-org guard
  │
prepare_fix.py  ── PHASE 1, before touching git: localise ticket → confidence
  │                  < 0.35 → label ai-needs-info, comment, STOP (zero AI cost)
  │                  injection found in code → label ai-blocked, STOP
  │                  else → context pack + model tier → logs/routing/<KEY>.json
  │               ── creates/resumes fix/<KEY>-<slug>, writes prompt (context pack on top)
invoke_copilot.py ── PHASE 0 gates: run lock, budget, per-ticket ceiling, prod-org guard,
  │                  MCP allow-list; reads routed TIER; runs Copilot CLI headless with
  │                  prompt on STDIN (v5, C9) and --model auto --auto-tier <pref> (v5, C10);
  │                  records AI credits; redacts log; always releases lock
review_gate.py  ── HUMAN CHECKPOINT #1 (y/n); auto-rejects restricted paths
validate_fix.py ── 15 static checks + real Apex tests via `sf project deploy validate`
  │                  (validate-only, never saved; v5, S9)
create_pr.py → update_jira.py (draft PR, comment, Solution Report, In Review)
  │
HUMAN CHECKPOINT #2: reviewer merges → salesforce-deploy.yml deploys
  │   v6: + HUMAN CHECKPOINT #3: deploy job waits in environment salesforce-deploy-gate until approved
  │
events.py logs every step to logs/run-events.jsonl (redacted)
generate_cost_dashboard.py rebuilds logs/cost-dashboard.html after each ticket
generate_pipeline_dashboard.py (v6) rebuilds logs/pipeline-dashboard.html from run-events.jsonl after each ticket
  v7: G11 fixed — watcher returns to main after every ticket (see v7 flow below)
notify.py: toast + log + optional email (recipients.json)
```


**v7 poll cycle (current):**
```
every poll:
  track_prs()        pr_tracker.track_all() → update_jira.on_pr_event() (MERGED/REJECTED/CONFLICT/UPDATE/REMIND)
                     → update_jira.release_waiting(open_prs)   (ai-waiting → ai-ready)
  refresh_index(); budget + daily cap
  claim_next()       claim_ticket: skips ai-waiting; Jira "is blocked by" not Done → ai-waiting, claim next
process_ticket(KEY):
  sync_to_base("before ticket") + preflight.check_git_state() + org guard
  prepare_fix.py     localise → overlap guard (open ticket PR touches same files → ai-waiting, exit 3)
                     → plan_batch (related ready Bugs claimed as members) → sync_base → new branch
                     → prompt (single or batch) → routing (+ <LEAD>.batch.json)
  invoke_copilot → review_gate (y/n) → validate_fix (batch-aware) → create_pr (batch title/table) → update_jira (+ members)
  finally: run event; if failed → batching.release_members(); sync_to_base("end of ticket"); dashboards; refresh_index
  outcome pr_open or waiting → next poll after WATCH_CONTINUE_SECONDS (5); else WATCH_POLL_INTERVAL_SECONDS
```

**What to call it:** an **agentic pipeline**. Deterministic Python orchestration wraps one genuinely agentic step (Copilot CLI), and humans sit at the two highest-risk decision points.

**Framing:** a **validated proof of concept**, not a production-proven system. The five demo tickets (CLAUDE-11 to CLAUDE-15) were deliberately seeded defects in sandbox apps. Benefits are hypotheses for a pilot to measure.

**People and context:**
- Pavan Vellure, Senior Consultant, Deloitte (BC).
- Proof of concept for the OperateNext Salesforce CI/CD automation initiative (led by Devasheesh Nautiyal).
- Uses stock GitHub Copilot (CLI + VS Code Agent mode), not the internal "Codify" tool.

## 2. Projects

| | Project 1 | Project 2 (main focus now) |
|---|---|---|
| App | Contact summary | Event Registration |
| Repo | pvellure-hash/SalesforceAgentDemo | pvellure-hash/EventRegistrationDemo (**now PUBLIC**, ruleset `protect-main`) |
| Local path | C:\Users\pvellure\Downloads\SFRepo\SalesforceAgentforce | C:\Users\pvellure\Downloads\SFRepo\EventRegistrationApp |
| Org | alias demo-dev | Developer Edition **'Agentforce_CICD' (00Dau00000COSx7EAH)** |
| Tickets | CLAUDE-11, 12, 13 | CLAUDE-14, 15, 16, + same-day bug + edit-registration feature (v6; keys to record) |
| Phase 0/1 | not yet applied | **applied** |

**EventRegistrationApp real code:**
- Classes: `EventRegistrationController.cls` (submitRegistration, getRecentRegistrations, validateInputs, isValidEmail, EMAIL_REGEX, MIN_GUESTS=1, MAX_GUESTS=10; date rule `eventDate == null || eventDate <= Date.today()`) and `EventRegistrationControllerTest.cls` (11 tests as of v6: testSuccessfulRegistration, testBlankNameIsRejected, testInvalidEmailIsRejected, testZeroGuestsIsRejected, testNegativeGuestsIsRejected, testMinimumBoundaryGuestsIsAccepted, testTooManyGuestsIsRejected, testMaxBoundaryGuestsIsAccepted, testPastEventDateIsRejected, testTodayEventDateIsRejected, testGetRecentRegistrationsReturnsNewestFirst).
- **No phone validation exists** (phone saved as-is) and, before the v6 feature ticket, **no update/edit path** (insert + read-only list of 10).
- LWC: `eventRegistrationForm`.
- Object: `Event_Registration__c`, with fields `Attendee_Name__c`, `Email__c`, `Phone__c`, `Event_Date__c`, `Number_of_Guests__c` and `Status__c`.
- The code index counts **10 files**. It showed 11 only while a stray `Temp.cls` existed.

## 3. Environment

**Machine:** Deloitte-managed Windows. winget and the Store are blocked, and the corporate TLS proxy intercepts traffic.
- The `NODE_TLS_REJECT_UNAUTHORIZED=0` workaround is still in place.
- The proper fix is `NODE_EXTRA_CA_CERTS` pointing at the corporate CA (open item).

**Copilot CLI:** installed with `npm install -g @github/copilot` and signed in with `copilot login --device-code`.
- **Billing:** since 1 June 2026, Copilot bills metered **AI Credits** ($0.01 each), based on input, output and cached tokens.
- **Caching:** cached input costs about 10% of fresh input. CLAUDE-14/15 showed more than 90% cache hits (596.6k of 657.8k tokens).
- **What resets the cache:** editing `copilot-instructions.md`, adding or removing an MCP server, or switching models mid-session.

**Other tools:**
- Python: a `.venv` in each repo.
- Salesforce CLI: authenticated to both orgs.
- `gh`: deliberately not installed (defence in depth).

## 4. Repository layout (EventRegistrationApp, v8)

```
.env (never committed)   .github/copilot-instructions.md   .github/workflows/{salesforce-validate,salesforce-deploy}.yml
force-app/main/default/  docs/ai-reports/
logs/  agent-audit.jsonl, notifications.jsonl, run-events.jsonl (Phase 0), cost-tracking.jsonl (legacy),
       cost-dashboard.html, code-index.json (Phase 1), routing/<KEY>.json, locks/<KEY>.lock, <KEY>-copilot-cli.log
agent/
  v3:      check_connections jira_client read_queue claim_ticket preflight prepare_fix invoke_copilot
           review_gate validate_fix create_pr update_jira run_agent watch_queue notify recipients.json
  Phase 0: events guards security_checks redact generate_cost_dashboard test_phase0
  Phase 1: code_index localizer context_pack injection_scan escalation routing tool_allowlist
           test_phase1 test_phase1_lang
  v5:      model_resolve (tier -> --model auto --auto-tier)
  v6:      generate_pipeline_dashboard.py, dashboard_template.html (light theme default, ◐ toggles dark)
  v7:      git_sync.py (Step 1), pr_tracker.py (Step 2), batching.py (Step 3)
  v8:      progress.py (7.1), deploy_tracker.py (7.2); test_agent_stop.py (11), test_dashboard_v71.py (18), test_deploy_v72.py (19)
           replaced: invoke_copilot, review_gate, watch_queue, pr_tracker, generate_pipeline_dashboard, dashboard_template,
           .github/copilot-instructions.md (rules v2), .github/workflows/salesforce-deploy.yml
logs/ (v8 additions): pr-status.json, deploy-state.json
           test_step2_tracker.py (18), test_step2_flow.py (7), test_step3_batch.py (19), test_step3_validate.py (8)
logs/ (v7 additions): pr-state.json, waiting.json, batch-failed.json, routing/<LEAD>.batch.json
logs/ (v6 additions): <KEY>-usage.json (Copilot usage record), pipeline-dashboard.html
```

**`.github/workflows/salesforce-deploy.yml` (read in v5):** triggers on `push` to `main` with `paths: force-app/**`, plus `workflow_dispatch`. Steps: checkout → `npm install -g @salesforce/cli` → login with secret `SF_SFDX_AUTH_URL` (temp file, deleted) → `sf project deploy start --test-level RunLocalTests --target-org target-org --json > deploy-result.json || true` → step summary → upload artifact. Notes: it is a **real** deploy; `|| true` means the job shows green even if the deploy fails (read the summary/artifact or Setup → Deployment Status); no Teams/email notification step. **v6:** the `deploy` job has `environment: salesforce-deploy-gate` (required reviewer pvellure-hash), so it waits for approval; a seeded bug that breaks a test never lands in the org because RunLocalTests rolls it back (by design).

**Files changed in v5 (all merged to main via PRs #7, #8, #9):**
- `invoke_copilot.py`: prompt passed with `input=prompt_text` (stdin), `-p` removed (C9); model args come from `model_resolve.model_cli_args(tier, explicit_model)`; log line now records the model args. An explicit `AGENT_MODEL_NAME`/`COPILOT_CLI_MODEL` still overrides.
- `model_resolve.py` (new): `economy→efficiency`, `standard→balance`, `premium→intelligence`; `model_cli_args()` and `describe()`.
- `validate_fix.py`: Apex step uses `sf project deploy validate --source-dir force-app --test-level RunSpecifiedTests --tests ...` (no `--dry-run`) (S9).
- App code (CLAUDE-15, PR #10): `EventRegistrationController.validateInputs()` now builds the exception, calls `ex.setMessage(String.join(errors, ' '))`, then throws (S10); `EventRegistrationControllerTest.testInvalidEmailIsRejected` asserts the exact message for input `notanemail`.

**Files replaced or edited in v4 (still current unless noted above):**
- `invoke_copilot.py`: Phase 0 rewrite, plus the Phase 1 edits (read routing, MCP allow-list).
- `watch_queue.py`: Phase 0 rewrite, plus `refresh_index()` at startup, every poll cycle and the end of each ticket.
- `prepare_fix.py`: Phase 1 rewrite. Uses `jc.update_labels(key, add=[...])`, prints scores as `:5.1f`, and writes routing.
- `jira_client.audit()` and `notify.notify_failure()`: each gained a one-line `redact.redact(...)` call.

**`jira_client` API (real):**
- Functions: `add_comment`, `adf_paragraphs`, `assign`, `audit`, `get_issue`, `my_account_id`, `transition`, `update_labels(key, add=(), remove=())`.
- Constants: `PROJECT`, `ENABLED`, `DRY_RUN`, `STATUS_IN_PROGRESS`, `STATUS_IN_REVIEW`.
- There is **no** `add_label` function.


**v7 module APIs (real, as delivered):**
- `git_sync`: `sync_base(base=None, cwd=None) -> (ok, msg)`, `current_branch()`, `dirty_files()`, `overlapping_open_prs(files, exclude_head=None, base=None)`, `unresolved_blockers(issue_json)`; CLI `status | sync | overlap <files>`.
- `pr_tracker`: `ticket_keys(pr)`, `list_pipeline_prs()` (fix/ or batch/ heads with a PROJECT key), `decide(pr, prev, now)`, `track_all(apply=True, now=None, prs=None, detail_fn=None) -> (events, open_numbers)`, `update_branch()`, `mark_waiting(key, kind, **info)`, `waiting_info()`, `clear_waiting()`, `KEY_RE`; CLI `python pr_tracker.py [--apply]`.
- `update_jira`: unchanged `main()` + `on_pr_event(ev)`, `release_waiting(open_prs)`, `update_member(member, lead, pr)`; constants `MERGED_LABEL`, `REJECTED_LABEL`, `WAITING_LABEL`, `STATUS_DONE`.
- `claim_ticket`: + `jira_blockers(key)`, `wait_for_blockers(key, blockers)`; `claim(key, account_id)` reused by batching.
- `prepare_fix`: `run_localisation(key, summary, description, branch)` now returns 7 items (… , files); `check_overlap()`, `localise_only()`, `plan_batch()`, `build_batch_pack()`, `write_batch_prompt()`; `EXIT_WAITING = 3`.
- `batching`: `select_members()`, `gather_candidates()`, `drop_open_pr_overlaps()`, `claim_members()`, `release_members(lead, outcome)`, `batch_branch()`, `bump_tier()`, `excluded_for()`, `record_failure()`.
- `routing`: + `write_batch(lead, members, tier, files, branch)`, `read_batch(lead)` (None unless members), `member_keys()`, `clear_batch()`.
- `validate_fix`: `batch_members(key, branch)` (only if batch record branch == current branch), `size_limits(n)`, `bad_commits(commits, keys)`.
- `notify`: + `notify_event(kind, ticket, title, detail="", url="")`, category `pr`; `_toast_safe()`.
- `watch_queue`: `sync_to_base(key, when)`, `track_prs()`, `run_step()` returns `(ok, step, returncode)`, `process_ticket()` returns outcome string.

**v8 module APIs (real, as delivered):**
- `progress`: `typical_seconds(step, evs)`, `status_line(index,total,step,elapsed,typical,extra,timeout_s)`, `git_activity()`, `run_with_progress(cmd,cwd,index,total,step,interactive,repo_root,base,timeout_s,out)`; `ENABLED`, `DEFAULT_TYPICAL`.
- `deploy_tracker`: `decide(entry, runs, now) -> (state, run, note)` states pending/awaiting_approval/deploying/deployed/failed/no_run; `on_merged(ev)`; `track(apply, now, runs, jobs_fn) -> lines`; `list_runs()` (GET /actions/workflows/{DEPLOY_WORKFLOW}/runs?branch=main), `job_times(run_id)`; `STATE_FILE` logs/deploy-state.json; CLI `python deploy_tracker.py [--apply]`.
- `pr_tracker` (7.1+): events carry `merge_sha`, `merged_at`; `build_snapshot()/write_snapshot()` → logs/pr-status.json; CLI `--snapshot`.
- `watch_queue.track_prs()`: MERGED → `deploy_tracker.on_merged` if enabled else `update_jira.on_pr_event`; then `deploy_tracker.track()`; dashboard rebuilt each poll (loud on change). `run_step` uses `progress.run_with_progress` (human step interactive).
- `invoke_copilot`: `extract_stop_reason()`, `branch_state()`, `delete_empty_branch()`, `check_agent_output()`.
- `generate_pipeline_dashboard`: records have `steps[]` (start,end,dur_s,status), `e2e_s`, `prState/prNumber/mergedAt`, `timeToMerge_s`, `leadTime_s`, `deploy{...}`, `mergeToDeploy_s`, `leadTimeDeployed_s`; outcomes add deployed / deploy_failed / pr_rejected / waiting / agent_no_changes.

## 5. Jira queue design

**Labels:**
- Set by a human: `ai-ready`.
- Set by the agent: `ai-locked`, `ai-needs-info`, `ai-blocked`, `ai-pr-created`.

**When the agent applies the warning labels:**
- `ai-needs-info`: a required heading is missing, **or (v4)** localisation confidence is below 0.35.
- `ai-blocked`: injection text in the ticket, **or (v4)** in a code file the fix would read.

**Ticket format:** the Description must contain the plain-text headings Steps to Reproduce / Expected Result / Actual Result / Acceptance Criteria.

**Retrying a ticket by hand:** remove `ai-needs-info` but keep `ai-locked` when running `prepare_fix.py` manually. For the watcher to pick it up again, reset the labels to `ai-ready` only.

## 6. .env (EventRegistrationApp, never committed)

```ini
JIRA_BASE_URL=https://one-atlas-lixv.atlassian.net
JIRA_EMAIL=pvellure@deloitte.ca
JIRA_API_TOKEN=<token>
JIRA_PROJECT_KEY=CLAUDE
JIRA_STATUS_IN_PROGRESS=In Progress
JIRA_STATUS_IN_REVIEW=In Review
GITHUB_TOKEN=<fine-grained PAT, THIS repo only: Contents RW, Pull requests RW, Metadata R>
GITHUB_REPO=pvellure-hash/EventRegistrationDemo
GITHUB_BASE_BRANCH=main
AGENT_ENABLED=true
AGENT_DRY_RUN=false
SF_TARGET_ORG=<eventreg org alias>
AGENT_RUN_APEX_TESTS=true
COPILOT_CLI_ENABLED=true
COPILOT_CLI_MODEL=
COPILOT_CLI_TIMEOUT_MINUTES=15
WATCH_POLL_INTERVAL_SECONDS=300
NOTIFY_ENABLED=true
NOTIFY_TOAST_ENABLED=true
NOTIFY_EMAIL_ENABLED=false
AZURE_CLIENT_ID=
AZURE_AUTHORITY=https://login.microsoftonline.com/organizations
# ---- Phase 0 ----
MONTHLY_BUDGET_USD=25
BUDGET_WARN_PCT=80
MAX_TICKETS_PER_DAY=10
TICKET_CREDIT_CEILING=60
CREDIT_TO_USD=0.01
BASE_BRANCH=main
ALLOWED_ORG_IDS=00Dau00000COSx7EAH
AGENT_ENVIRONMENT=local
# ALLOW_UNPROTECTED_BRANCH=true   (local-demo waiver; NOT used - repo made public instead)
# ---- Phase 1 ----
SF_PACKAGE_DIR=force-app/main/default
LOCALIZATION_MIN_CONFIDENCE=0.35
CONTEXT_PACK_MAX_TOKENS=12000
# COPILOT_MODEL_ECONOMY / _STANDARD / _PREMIUM: no longer used by invoke_copilot.py (v5, C10).
# The tier now maps to --model auto --auto-tier. Leave COPILOT_CLI_MODEL blank unless you
# deliberately want to pin one exact model name (it then overrides auto routing).
ALLOWED_MCP_SERVERS=
# ---- v7 ----
WATCH_CONTINUE_SECONDS=5
OVERLAP_GUARD_ENABLED=true
PR_REMIND_HOURS=4
PR_AUTO_UPDATE_BRANCH=true
JIRA_STATUS_DONE=Done
BATCH_ENABLED=true
BATCH_MAX_TICKETS=3
# use BATCH_KINDS=Bug,Task if bugs are raised as Task (never put a comment on the value line, R1)
BATCH_KINDS=Bug
# ---- v8 ----
WATCH_PROGRESS=true
WATCH_PROGRESS_INTERVAL=1
DEPLOY_TRACKING_ENABLED=true
DEPLOY_WORKFLOW=salesforce-deploy.yml
DEPLOY_NO_RUN_HOURS=2
DEPLOY_TRACK_DAYS=3
# GITHUB_TOKEN now also needs Actions: Read-only (deploy tracking)
# optional: BATCH_EXTRA_FILES=3 BATCH_EXTRA_LINES=150 BATCH_MAX_FILES_CAP=10 BATCH_MAX_LINES_CAP=450
```

**Rule:** never put a comment on the same line as an empty value (Issue R1).

**Org alias:** `SF_TARGET_ORG=eventreg-dev` → user `pvellure_ai@deloitte.ca`, org `00Dau00000COSx7EAH` (confirmed in v5 with `sf org list`).

**Exports:** use `git archive --format=zip -o ..\export.zip HEAD`. Tokens were rotated once after an earlier exposure.

## 7. Guardrails and GitHub setup

**Guardrail file:** `.github/copilot-instructions.md` is change-controlled, both for correctness and to protect the prompt cache.

**EventRegistrationDemo:**
- **Repository:** made public after a history scan was clean (no secrets, and `.env` never committed).
- **Ruleset `protect-main`:** active on the default branch, with Restrict deletions, Block force pushes, Require PR, **0 approvals**, and an empty bypass list. 0 approvals is used because the pipeline opens PRs under the owner's token and GitHub does not let authors approve their own PRs.
- **v5 finding (R8):** PR #10 (CLAUDE-15) shows "Merged", attributed to `pvellure-hash`, **"No reviews"**, "1 check passed". Commit `98e02d7` on main then triggered "Salesforce Deploy (real deploy on merge to main)" (35 s, green), and Setup → Deployment Status shows deploy `0Afau000006qeUB` **Succeeded** at 2:21 p.m. The exact mechanism (repository auto-merge vs. a click) is still to confirm, but either way nothing in the ruleset stopped it. **Fix:** require 1 approval from a second account plus CODEOWNERS (and confirm "Allow auto-merge" is off), then re-test that a pipeline PR cannot merge on its own.
- **v6 resolution (R8):** PR #10 timeline (API) = `merged`/`closed` by `pvellure-hash`, `auto_merge` empty, so it was a manual click; `Select-String -Pattern merge agent\*.py` shows no merge API call anywhere. One account cannot satisfy "1 approval" (author can't self-approve), so instead: `environment: salesforce-deploy-gate` added to the deploy job (PR #11), environment created with Required reviewers = pvellure-hash (possible because repo is public), auto-merge confirmed off. CODEOWNERS idea dropped for solo use (branch `chore/add-codeowners` deleted). For a team: add 1 approval + CODEOWNERS + second account as well.
- **End-to-end human clicks now:** (1) review gate y/n in the terminal, (2) merge the PR on GitHub, (3) approve the deployment in Actions.
- **Token:** scoped to this repo only.

**`security_checks.py` live result:** no FAIL.

| Check | Result | Note |
|---|---|---|
| Salesforce org | PASS | Developer Edition 'Agentforce_CICD' |
| Base branch protection | WARN | Rules can't be read with a least-privilege token; expected |
| Token scope | WARN | Account has admin because it is the owner; expected |

**Copilot CLI folder trust:** approvals live in `%USERPROFILE%\.copilot\permissions-config.json`. Verify with `copilot -p "run: git status --short --branch" -s --no-ask-user`.

## 8. Phase 0: cost and safety foundations (BUILT)

| Module | What it does |
|---|---|
| events.py | `emit_event(ticket, step, status, **fields)` appends one redacted JSON line to `logs/run-events.jsonl`. Never raises; writes with O_APPEND. Run id is shared through `AGENT_RUN_ID`. Helpers: `credits_for_ticket`, `spend_this_month_usd`, `tickets_started_today`. |
| guards.py | `check_budget` (WARN at BUDGET_WARN_PCT), `check_daily_cap`, `check_ticket_ceiling` (sums earlier attempts), `acquire_lock` / `release_lock` (atomic O_EXCL; stale after timeout + 10 min). |
| security_checks.py | `check_salesforce_org` (SOQL on Organization: sandbox or Developer Edition, optional ALLOWED_ORG_IDS, fails closed); `check_branch_protection` (branch protected + ruleset pull_request, else classic protection, else WARN); `check_github_token_scope` (classic token = FAIL; multiple repos or admin = WARN). Windows `cmd /c` for npm shims. |
| redact.py | Removes credentials (Atlassian, GitHub, SF auth URL / session id, private key, AWS, Bearer, URL credentials, key=value secrets) and PII (email, phone, card, SIN/SSN). Applied to events, the Copilot log, audit and alerts. |
| invoke_copilot.py | Gate order: lock → budget → ceiling → prod-org guard → MCP allow-list → run (prompt on stdin; `--model auto --auto-tier`) → parse "AI Credits X.XX" → event + legacy cost log → flag single-run overshoot. Lock released in `finally`. **Dry run no longer calls Copilot** (no cost). **v5:** the "AI Credits" line is never present in `-s` (silent) output, so credits are logged as unknown (C12). |
| watch_queue.py | Startup preflight + security checks; budget/cap before each claim (one alert per day); a step-level start/end event with duration; outcome mapping (prepare→blocked, agent_step→agent_failed, review→rejected_gate, validate→failed_validation, pr→pr_failed, jira→jira_failed); dashboard refresh after the agent and at ticket end. |
| generate_cost_dashboard.py | Self-contained HTML (inline SVG, no CDN) in `logs/cost-dashboard.html`, rebuilt automatically and never blocking. The data stays append-only. |

**Tests:** `test_phase0.py` passes 23/23 on Pavan's machine. One `WinError 3` warning line is expected.

**Live:** security checks were run with no FAIL.

**Not yet done:** deliberately triggering budget, ceiling, lock and org guard on a live ticket (Implementation Guide Part 9, Step 9).

**Known limits:**
- **v6: C12 closed.** Credits now come from `--usage-output-file` (`totalNanoAiu / 1e9`). Historical note — **v5: credits were not captured at all (C12).** `-s` suppresses the usage stats. Fix: add `--usage-output-file logs/<KEY>-usage.json` to the Copilot command (documented in `copilot --help`) and read credits from that JSON; until then budget and ceiling gates see $0.00 and cannot fire on real spend.
- Credits arrive only at the end of a run, so the ceiling can't stop a run mid-way; the timeout is the live limit.
- A rejected `--model` value fails fast before any prompt is processed (observed $0.00 after each rejection).
- review_gate rejection and a crash both record `rejected_gate`.

## 9. Phase 1: code intelligence and model routing (BUILT)

**Why it exists:** on a big repo, letting the agent grep and read for every bug is the main cost driver. Phase 1 maps the code once, refreshes it incrementally, scopes the AI to a small context pack, and sizes the model to the defect.

**Key design correction (user feedback, important):** reporters must **not** need to know class names or API names. The first version matched only exact API names, so "Email format validation is not enforced" scored 0.0. Pavan rejected that approach, rightly. The index now:
- Stores field labels.
- Splits every identifier into plain words.
- Lets fields inherit their parent object's words.
- Handles plurals.

**Modules:**

| Module | Key behaviour |
|---|---|
| code_index.py (schema 2) | Parses classes, triggers, LWC .js, Aura .cmp, objects and fields. Records edges (class refs, object/field refs, `@salesforce/apex` imports), test flag and guessed test target. `words_own` comes from the API name and label; `words` adds parent-object words for fields. Keeps `word_index` and `generic_words`. Incremental by content hash. Functions: `find_symbol`, `find_by_word` (with plural fallback), `dependents_of`, `tests_for`. |
| localizer.py | Scores signals (table below). Applies the confidence formula. Treats linked files as one group, not competitors. Expands only strong candidates (≥ 50% of the top score) and adds the dependents of a matched field or object. |
| escalation.py | Sizes complexity from strong candidates only. simple (conf ≥ 0.65, ≤ 2 files, short) → economy; moderate (≥ 0.40, ≤ 5 files, ≤ 3 components) → standard; else premium. Below 0.35 → `ask_for_info`. Ladder: attempt 2 = same tier with feedback, attempt 3 = next tier, then hand to a person. A ceiling breach hands to a person immediately. Has a zero-score edge-case guard. |
| context_pack.py | Markdown block of full files within the token budget (4 characters per token); files over budget are summarised; also builds sparse-checkout paths. |
| injection_scan.py | Phrase and structure scan (ignore instructions, push to main, skip review, exfiltrate, curl, eval, suspicious block comments, zero-width characters) on the files about to be sent. |
| routing.py | Writes and reads `logs/routing/<KEY>.json`. Needed because each step runs as a separate process, so environment variables don't reach the next step. |
| tool_allowlist.py | Checks for unexpected MCP servers in `~/.copilot/*.json` against `ALLOWED_MCP_SERVERS`; a missing config file passes. Closes Issue E5 once wired. |
| prepare_fix.py | Runs localisation **before** `check_repo_safe` and branching. Uses `update_labels` and comments. Saves routing. |
| watch_queue.py | `refresh_index()` at startup, every poll cycle and the end of each ticket. Fails open and only warns. |

**Scores (localizer.py):**

| Signal | Points |
|---|---|
| File mentioned | 10 |
| Stack frame | 9 |
| Class identifier or method call | 8 |
| Object/field API name | 4 |
| Each file that uses a matched object/field | +2 |
| Specific plain word | 3 (×2 if in the summary line) |
| Word inherited only from the parent object | 1 |
| Generic word, or a word found in ≥ 50% of files | 0.5 |
| Test classes | ×0.5 |
| Plain-word cap per file | 9 |

**Confidence:** 0.40 × separation (top score vs. next *unrelated* file) + 0.25 × corroboration + 0.35 × signal strength. If the top score is below 3 (generic only), confidence is capped at 0.20. The threshold is 0.35.

**Tests (all passing on Pavan's machine):**
- `test_phase1.py`: 24.
- `test_phase1_lang.py`: 11.
- Both build temporary fixture repos and never touch the real repo.

**Live result: CLAUDE-15 on the real repo through `prepare_fix.py`**

| Attempt | Confidence | Top file | Tier | Context pack |
|---|---|---|---|---|
| Before the language fix | 0.0 | — | refused (ai-needs-info) | — |
| After the language fix | 0.42 | 4-way tie at 9.0 (Event_Date__c first) | **premium** | 4,055 tokens, 10 files |
| Final | **0.55** | **Email__c 7.0** (next unrelated 4.0) | **standard** (v5: `--auto-tier balance`) | **2,515 tokens, 5 files** |

The final run resumed the branch, wrote the prompt and posted the Jira comment.

**Simulated on a copy of the real repo layout:**
- "negative number of guests": 0.71 → `Number_of_Guests__c` → economy.
- "event date in the past": 0.72 → `Event_Date__c` → economy.
- "registration page is broken": 0.20 → refused.

**Model routing after v5 (C10):** the tier from `escalation.py` is passed to Copilot as an auto-routing preference, so no model name is stored anywhere:

| Tier | Copilot CLI arguments |
|---|---|
| economy | `--model auto --auto-tier efficiency` |
| standard | `--model auto --auto-tier balance` |
| premium | `--model auto --auto-tier intelligence` |

Console line on a real run: `model  : auto (--auto-tier balance, from standard tier)`. Why: Copilot CLI has no non-interactive model list (open request github/copilot-cli #700) and, on this account, every explicit name was rejected even when shown in `/model`. Other useful flags from `copilot --help`: `--reasoning-effort`, `--max-ai-credits`, `--usage-output-file`, `--output-format json`.

**Done in v5:** Phase 1 PR merged (PR #6); index rebuilt on main (10 files); routed tier reached `invoke_copilot.py`; CLAUDE-15 ran through review, validation, PR and deploy.

**Still open for Phase 1:**
- Wire retry-with-feedback into `validate_fix.py` (the S10 case was exactly this: the first Apex failure carried the exact cause).
- Run 3–4 more plain-language tickets and record confidence/top file/tier against what was actually changed.
- Confirm which concrete model `auto` picked (check `--usage-output-file` JSON once C12 is done).

## 10. Issue index: 93 issues (IDs match the v8 Troubleshooting Guide, Part 5)

**v3 issues (36):**
- **P1–P4 (pre-flight):** P1 npm `.cmd` shims need `cmd /c`; P2 GITHUB_TOKEN leaked into preflight's own Copilot call; P3 use `sys.executable`; P4 SF timeout raised to 60s.
- **E1–E6 (environment):** winget/Store blocked; TLS warnings; pasting instruction text into PowerShell; watcher looks frozen; **unknown Atlassian MCP (E5)**; export leaking `.env`.
- **S1–S8 (Salesforce):** required-field permission set; first-deploy security error; flaky CreatedDate sort; seed bug rolled back by tests; browser validation hides the bug; LWC not on a page; source-tracking warning; wrong org in `.env`.
- **C1–C8 (Copilot CLI):** folder not trusted; approvals incomplete; token leak; tries `gh pr create` or push; fix not committed; report files missing; review gate approved with 0 files changed; quota or credits exhausted.
- **J1–J2 (Jira):** headings in the wrong field; stale labels.
- **G1–G8 (Git/GitHub):** dirty tree; diverged branches; `reset --hard` lost work; tooling committed on a fix branch; commit on the wrong branch; token scoped to the wrong repo; printed PR link doesn't create a PR; can't approve your own PR.

**New in v4: R (Phase 0 guardrails)**

| ID | Issue | Fix |
|---|---|---|
| R1 | `ALLOWED_ORG_IDS` inline comment read as the value → org FAIL | Pin the org id; accept only `00D…` values |
| R2 | Private repo on GitHub Free can't be protected | Scan history, make public, ruleset `protect-main`, 0 approvals (or Pro, or local waiver) |
| R3 | Branch WARN: rules unreadable with a least-privilege token | Expected; confirm by eye |
| R4 | Token WARN: reaches 2 repos | Scope to one repo |
| R5 | Token WARN: account has admin | Expected for the owner |
| R6 | `test_phase0` prints a `WinError 3` warning | Expected |
| R7 | A single run exceeds the ceiling | By design; credits arrive only at the end |

**New in v4: L (Phase 1 code intelligence)**

| ID | Issue | Fix |
|---|---|---|
| L1 | `test_phase1` assumed fixture files in the real repo (16 failures); stray `Temp.cls` | Self-contained temp repos; delete `Temp.cls` |
| L2 | PowerShell multi-line `python -c` and a stray backslash `[:5\]` | Use a script file (`try_localize.py`, not committed) |
| L3 | Printed literal `['score']` | Missing `c` |
| L4 | **Plain-language tickets scored 0.0** | Design flaw, fixed with the word index |
| L5 | Windows backslash paths → KeyError in tests | Normalise separators |
| L6 | `jira_client.add_label` doesn't exist | Use `update_labels(key, add=[...])` |
| L7 | `:3d` format on float scores | Use `:5.1f` |
| L8 | **Routed model lost between processes** | `routing.py` file hand-off |
| L9 | Context pack had only the field XML | Add the field's dependents |
| L10 | **4-way tie, premium, 10 files** | Common-word, summary, inherited-word and test weighting; linked files not competitors; strong-candidate sizing |
| L11 | Two `test_phase1` failures after the reweighting | Zero-score bug; outdated expectation |
| L12 | Uncommitted-tree STOP after adding Phase 1 files (G1 again) | Commit via feature branch + PR |
| L13 | Retry after ai-needs-info | Remove the label, keep ai-locked |
| L14 | "Branch already exists - resuming it" | Expected |

**New in v5 (8): found while running CLAUDE-15 end to end (Troubleshooting Guide 5.9)**

| ID | Issue | Fix |
|---|---|---|
| C9 | `The command line is too long.` — 14,272-char prompt via `-p`; npm `copilot` shim runs through `cmd.exe` (8,191-char limit) | Pipe prompt on stdin (`input=prompt_text`), drop `-p` (PR #7) |
| C10 | `Error: Model "<name>" from --model flag is not available.` for every name tried | `--model auto --auto-tier <pref>` via `model_resolve.py` (PR #8) |
| C11 | Copilot run "Blocked: the working tree is not clean" because `invoke_copilot.py`/`model_resolve.py` were uncommitted on the ticket branch | Commit tooling on its own branch + PR, merge main into the fix branch, re-run |
| C12 | `cost: WARNING - 'AI Credits' line not found` on every run | **Open:** `--usage-output-file` and parse the JSON |
| S9 | `deploy start --dry-run --test-level RunSpecifiedTests`: 0 tests, 0% coverage, "Fatal Error"; direct `sf apex run test` passed 10/10 | `sf project deploy validate` (PR #9) |
| S10 | `Expected: A valid email address is required., Actual: Script-thrown exception` | Call `ex.setMessage(...)` before throwing `AuraHandledException` |
| G9 | `create_pr.py`: `FAIL GitHub PR: HTTP 422 ... No commits between main and fix/...` but PR #10 existed and merged | **Open:** look up an existing PR for the branch before failing; treat 422 as "check again" |
| R8 | PR merged with "No reviews"; real deploy fired | **Open (priority):** 1 approval from a second account + CODEOWNERS; auto-merge off |

**Lessons from v5 worth keeping:**
- A passing `sf apex run test` only proves the code **already deployed** in the org; `deploy validate` tests the local source. The two disagreed on CLAUDE-15 until S10 was fixed.
- Pipeline tooling changes go on their own branch and PR first, then `git merge main` into the ticket branch (G4/L12/C11).
- The Copilot narrative can be loose ("4 files changed" when 3 report files were committed): always check `git status`, `git log origin/<branch>..HEAD` and `git show --stat` rather than trusting the CLI's own summary.

**New in v6 (9): Troubleshooting Guide 5.10**

| ID | Issue | Fix |
|---|---|---|
| C13 | First C12 parser searched for a key containing "credit"; real usage JSON has none (keys: totalPremiumRequestCost, totalUserRequests, totalNanoAiu, tokenDetails{input,cache_read,cache_write,output}, totalApiDurationMs, sessionStartTime, codeChanges{linesAdded,linesRemoved,filesModifiedCount,filesModified}, modelMetrics{<model>:{requests,usage,totalNanoAiu}}, agentMetrics, currentModel, lastCallInputTokens/OutputTokens) | credits = totalNanoAiu / 1e9 (PR #14); prints raw JSON + model each run |
| C14 | github.com/settings/billing → AI usage shows "No usage"; "your enterprise will be billed" | Enterprise seat: use Copilot settings → Usage (23% included credits; inline suggestions 3%, free); per-user report from enterprise billing manager |
| E7 | Actions outage 5 Oct 2026 from 19:11 UTC → "job was not acquired by Runner of type hosted", Internal server error | Check githubstatus.com; Re-run all jobs after recovery; re-approve deploy gate |
| E8 | PS 5.1 BOM, no utf8NoBOM, stray characters in copied commands | .NET WriteAllText with UTF8Encoding($false); retype suspicious commands; delete scratch files (A1 clean tree) |
| S11 | `sf apex run --file -` → "File not found ... -" | Use a real file |
| S12 | Anonymous Apex → LimitException for AuraHandledException | Verify via test class / PR validate check, not anonymous Apex |
| S13 | Manual UI test misleading (no Submit, 4 Oct vs today 5 Oct, HTML5 min=1) | Exact boundary value + Submit; trust Apex tests |
| G10 | Branch name collision → commit on wrong branch; empty branch pushed; "nothing to compare" when commit never ran | Check branch/status before commit; `git branch -m`, cherry-pick; delete stale branches |
| G11 | **Watcher left on fix branch → next ticket fails A2; next branch would lack unmerged fix** | **OPEN**: return to main + ff-only at end of every ticket (planned) |

**Closed in v6:** C12 (PRs #12, #14), G9 (PR #13), R8 (PR #11 + environment).

**Lessons from v6 worth keeping:**
- Run a tool once and read its real output before writing a parser (C13 would have silently failed again).
- A seeded bug that breaks an existing test is **never live in the org**; prove it with the PR Validate failure and Setup → Deployment Status ("Failed — N Test Failures"), not the UI.
- `sf apex run test` tests what is already deployed; use it after a deploy to confirm the fix (11/11 after the same-day fix).
- The watcher only automates up to draft PR + Jira; merge and deploy are deliberate human clicks (user accepted this after discussion; "full automation" would remove the only safety net).


**New in v7 (9) + G11 closed: Troubleshooting Guide 5.11**

| ID | Issue | Fix |
|---|---|---|
| G11 | Watcher left on fix branch | **Closed** (Step 1, git_sync) |
| G12 | `git add agent/git_sync.py` from inside `agent\` → pathspec error, commit "nothing added" | Paths are relative: `git add git_sync.py` |
| E9 | `can't open file ...agent\git_sync.py` after download | Browser saved to Downloads; `Copy-Item` into agent\ |
| E10 | "LF will be replaced by CRLF" | Harmless; optional .gitattributes |
| G13 | Second PR "out-of-date with base branch" | Watcher presses Update branch (PR_AUTO_UPDATE_BRANCH); ruleset requires up to date |
| G14 | PR conflict | Resolve on GitHub, or close + delete branch + ai-ready only |
| G15 | Batch fails "Code lines <= 200" with old validate_fix | v7 validate_fix scales limits; else members retried alone |
| J3 | Ticket stuck ai-waiting | PR still open / blocker not Done; `python pr_tracker.py`; manual release + edit logs/waiting.json |
| J4 | Related bugs never batched | BATCH_KINDS vs issue type (Task); read `[batch] skip` reasons |
| J5 | "no transition to 'Done'" after merge | Set JIRA_STATUS_DONE to an available transition |

**Lessons from v7 worth keeping:**
- Real files first, then patch — every v7 change was made against the user's actual `prepare_fix.py`, `watch_queue.py`, `preflight.py`, `claim_ticket.py`, `update_jira.py`, `jira_client.py`, `notify.py`, `create_pr.py`, `invoke_copilot.py`, `localizer.py`, `routing.py`, `validate_fix.py` (L6 lesson held).
- `validate_fix.py` turned out to be the hidden coupling for batching (size limits + commit rule) — check downstream checks before widening a unit of work.
- Give commands for the folder the user is actually in (G12).

**New in v8 (10): Troubleshooting Guide 5.12**

| ID | Issue | Fix |
|---|---|---|
| C15 | Copilot exit 0, 0 commits; plan then "can't proceed without written approval" (CLAUDE-18) | Rules v2; `agent_no_changes` with reason in Jira; empty branch deleted |
| G16 | Compare page "nothing to compare" after push | Nothing committed; `git status` between add and commit; re-push |
| E11 | Select-String misses `getFullYear()+'-'` | `-SimpleMatch` |
| D1 | Month to date < Total AI spend | Local dates (template fix) |
| D2 | Merged shown as PR open | `pr_tracker.py --snapshot`; v7.1 dashboard |
| D3 | Progress line odd chars / wraps | widen window or `WATCH_PROGRESS=false` |
| F1 | `[deploy] WARNING ... HTTP 403` | Token: Actions Read-only |
| F2 | Ticket stays In Review after merge | By design; approve deploy |
| F3 | ai-deploy-failed, job red | Read summary; re-run or fix forward |
| F4 | No deploy run found | No force-app change / workflow name / Actions down |

**Lessons from v8:** read the agent's own words before changing code (C15 was a rules problem); never show a fake percentage; "Done" must mean live; a `|| true` in CI silently defeats every downstream signal.

## 11. Demo tickets

| Ticket | Project | Seeded defect | Outcome |
|---|---|---|---|
| CLAUDE-11 | SalesforceAgentDemo | Age off by one on birthday | Merged (PR #1); deploy to re-verify |
| CLAUDE-12 | SalesforceAgentDemo | No age for contacts under 1 | PR #5; merge/deploy to re-verify |
| CLAUDE-13 | SalesforceAgentDemo | Blank Languages__c shown as "null" | First fully headless run; merged and deployed |
| CLAUDE-14 | EventRegistrationDemo | Guest count > 10 accepted | Merged and deployed |
| CLAUDE-15 | EventRegistrationDemo | Invalid email accepted | Merged and deployed earlier (10/10 Apex); re-used in v4 as the Phase 1 live test; **v5: full end-to-end run.** Copilot strengthened the test (`notanemail` + exact message); validation then exposed S10; `setMessage()` fix added; 10/10 via `deploy validate`; **PR #10 merged (no review, R8)**; deploy `0Afau000006qeUB` Succeeded. Jira update to confirm (`update_jira.py` output not captured). Solution Report text is stale (says no production-code change). |
| CLAUDE-16 | EventRegistrationDemo | Guest count 0 or negative accepted (seed PR #15 removed `numberOfGuests < MIN_GUESTS`; broke testZeroGuestsIsRejected + testNegativeGuestsIsRejected; real deploy "Failed — 2 Test Failures") | **v6, fully unattended via watch_queue.py.** localise 0.77 moderate → `--auto-tier balance`; agent 131.1 s, **1.6703 credits (~$0.0167)**; review 10.7 s; validate 25 s; PR 22.3 s; Jira 8.2 s; total 218.1 s, outcome pr_open. Commits: "fix(CLAUDE-16): reject guest counts below minimum", "test(CLAUDE-16): cover guest count lower boundary". PR #16 merged; deploy approved at gate; Succeeded; form rejects 0. |
| CLAUDE-17 (PR #20) | EventRegistrationDemo | Same-day event date accepted (seed: `eventDate <= Date.today()` → `<`; broke only testTodayEventDateIsRejected; real deploy 0Afau000006qlAf "Failed — 1 Test Failure" 7:07 p.m.) | v6: agent fixed it, PR merged, deploy **Succeeded 7:31 p.m.**; 11/11 org tests pass; UI rejects today on Submit (confirmed). |
| CLAUDE-18 | EventRegistrationDemo | **Feature**: edit an existing registration — `updateRegistration(registrationId, phone, numberOfGuests, eventDate)` re-using guest (1–10) and date rules, `isUpdateable()` check, Edit action per row in LWC, name/email read-only | v6 run exposed G11. **v8 first run (6 Oct, 21:28):** Copilot (gpt-6-luna, auto balance) planned 4 code files + 3 reports, then stopped (C15), 0.46 credits, 0 commits; branch deleted, labels reset. **Re-run pending** after the rules PR. |

## 12. Cost model: decisions and reasoning

- **The metric that matters is cost per *delivered* fix, not cost per ticket.** The biggest controllable cost is wasted spend: runs that never produce a PR (timeouts, failed validation, gate rejections).
- **Levers, in order:**
  1. Don't call the AI (triage, confidence threshold, budget, cap, lock, duplicate detection).
  2. Make each call cheaper (code index, context pack, model tiers, a stable instruction prefix for caching, no stray MCP servers).
  3. Hard-limit each call (ceiling, timeout).
  4. Waste fewer calls (retry with test feedback once).
- **Prove savings with a measurement before quoting any number to a client.** Run the same defects with and without the context pack and compare tokens, cost per fix and success rate.

## 13. Dashboards built

| File | What it is | Status |
|---|---|---|
| logs/cost-dashboard.html (via generate_cost_dashboard.py) | Static, auto-refreshed: KPI cards, cumulative-spend trend, cost-per-ticket bars, detail table | **Live in the pipeline** (reads `cost-tracking.jsonl`) |
| ai-pipeline-command-center.html | Interactive v1 (dark/light, filters, drill-down drawer, CSV export, spend vs. budget, funnel, tiers, scatter, guardrail panel). v6: light theme default | Sample-data prototype; **superseded by the live version below** |
| **logs/pipeline-dashboard.html** (via `agent/generate_pipeline_dashboard.py` + `agent/dashboard_template.html`) | v6 live command center: same UI as v1, data = run-events.jsonl grouped by run_id + `<KEY>-usage.json` tokens + `<KEY>-pr.json` title/URL. Rebuilt by `watch_queue.refresh_dashboard()` after agent step and at ticket end (non-blocking subprocess). First run: "rebuilt from 14 ticket run(s)" | **Live** (PR #17). Gaps: tool calls & files/lines not logged (usage file `codeChanges` can feed this); merged/deployed not observed (shows pr_open; poll GitHub `merged_at`); same usage file reused for retries of one key |
| ai-delivery-command-center-v2.html | Three tabs: **Executive** (plain-English summary, 4 big numbers, weekly chart, "is it safe?" panel, value estimate with editable assumptions, decisions needed); **Delivery** (plain-language statuses, who-does-what, stage times); **Engineering** (cost drivers, code-intelligence panel, escalation ladder). Plus a glossary and downloads (PDF, offline HTML, CSV) | Sample data only. **Remaining Phase 2 work: feed it from the same generator** |

**Cost math (as explained to the user):** `totalNanoAiu ÷ 1,000,000,000 = credits`; `credits × $0.01 = USD` (GitHub's published rate). CLAUDE-16: 1,670,317,500 → 1.6703 credits → $0.0167. Test "say hello": 226,008,000 → 0.226 credits → $0.0023 (CLAUDE-16 ≈ 7.4× the trivial call). The ÷1e9 step is a best fit, not reconciled with an invoice (C14). Totals/cost-per-fix/waste% are simple sums and ratios.

**Design principle:** one telemetry stream, three audiences, two delivery modes:
- Local file.
- An Entra ID-protected cloud link, e.g. Azure Static Web Apps. Downloads remain available.
- A Salesforce LWC version is possible only if a client wants it (custom object + Apex REST + LWC).

## 14. Target cloud architecture (Phase 3, not built)

Salesforce is SaaS, so nothing here needs Windows; any Linux machine with git, Node, Python, sf and Copilot CLI will do. GitHub Actions is the natural host.

| Concern | Local today | Cloud target |
|---|---|---|
| Trigger | watch_queue polls | Jira Automation webhook → repository_dispatch → one workflow run per ticket |
| Review gate | y/n in a terminal | GitHub Environment with required reviewers (approve in a browser or on a phone) |
| Lock | Lock file | Workflow concurrency group per ticket |
| Secrets | .env | GitHub secrets / OIDC → Key Vault; **GitHub App** short-lived tokens |
| Org | CLI login | Sandbox-only auth URL; **no production secret exists** |
| Runner | Laptop | Ephemeral Linux runner (GitHub-hosted, or self-hosted in the client's Azure) with an egress allow-list |
| Index | On each poll | Workflow on every merge to main |
| Dashboard | Local file | Published after each run behind SSO |
| Alerts | Toast + email | Teams + email |

**Main open risk:** how Copilot CLI authenticates on an unattended CI runner, and whether Deloitte's and the client's Copilot licences permit it. Confirm before Phase 3 starts.

## 15. Phases and status

| Phase | Goal | Status | Exit test |
|---|---|---|---|
| 0 Foundations | Measure, cap, close safety gaps | **Built.** 23 tests; security checks live; **v6: credits captured (C12 closed), deploy gate enforced (R8 closed).** Gap: live guard triggers (Step 9) still to do | One real ticket gives a complete redacted event trail **with credits**; each guard proven; a pipeline PR cannot merge without a second person |
| 1 Code intelligence | Cost scales with the defect, not the repo | **Built; proven on CLAUDE-15 (0.55) and CLAUDE-16 (0.77)** with auto-tier routing. Retry-with-feedback and feature-ticket data outstanding | ~10 defects with vs. without the context pack: fewer tokens and lower cost per fix, success not worse |
| 2 Live dashboard | Every audience sees current data automatically | **v6: single-view live dashboard built and wired.** Three-view version, merge status, files/lines still to do | Three-view dashboard updates after a run with no manual step |
| v7 Continuous flow | Queue keeps moving; no clashes; related bugs together | **Code complete; 52 offline tests; Step 1 merged; Steps 2–3 merge to confirm; live 5-ticket test pending** | Impl. Guide 13.11 checklist passes in one session |
| v8 Merge to live | Done = deployed; step times; progress; clear agent stops | **Code complete; 48 new offline tests; dashboard checked in a browser; 7.2 PR to merge; live checks pending** | Impl. Guide 14.10 |
| 3 Cloud and agentic | No laptop in the loop | Planned | Label → draft PR with nobody at a keyboard; approval in the browser |
| 4 Enterprise | Client-ready | Planned | Security review signed off; pilot started |
| 5 Earned autonomy | Fewer human touches where proven safe | Optional | Pilot data shows a low-risk class is safe (a human still merges) |

## 16. Next steps (start here in the next chat)

**A. Land and prove v8 (do first)**
1. Confirm `fix/agent-stop-rules` and `fix/dashboard-local-dates` merged (test_agent_stop 11/11).
2. GitHub token: add **Actions: Read-only**.
3. Branch `feat/v7.2-deploy-tracking`: copy deploy_tracker.py, progress.py, watch_queue.py, pr_tracker.py, generate_pipeline_dashboard.py, dashboard_template.html, test_deploy_v72.py, test_dashboard_v71.py, test_step2_flow.py into agent\ and salesforce-deploy.yml into .github\workflows\; tests 19/19, 18/18, 7/7, 18/18; commit/push/merge (Impl. Guide 14.8). Add v8 .env lines. `python pr_tracker.py --snapshot; python generate_pipeline_dashboard.py`.
4. Re-run **CLAUDE-18** (labels ai-ready only): expect progress lines, 4 code files + 3 reports, PR. Merge → ai-merged + In Review → approve deploy → `[deploy] ... -> deployed`, ai-deployed, Done. Check the dashboard timeline and deploy times.
5. Then the v7 five-ticket test (Impl. Guide 13.11).

**B. Then**
- Daily digest email; wasted-spend by reason view; split batch credits; retry with test feedback.
- Review gate in the browser (Phase 3); cloud runner; three-view dashboard; apply to SalesforceAgentDemo; C14 reconciliation; E2, E5, injection tuning; pilot approval.

## 17. Deliverables (current = v8)

| File | What it is |
|---|---|
| Pipeline-Implementation-Guide-v8.docx | v7 + **Part 14** (CLAUDE-18/C15 rules fix, local dates, PR status + step times, live progress, deploy tracking with options/decision, labels, files, install, .env, tests, live plan, limits, future); labels table, token permissions, Appendix A, guide map |
| Pipeline-Troubleshooting-and-Operations-Guide-v8.docx | v7 + **5.12** (C15, G16, E11, D1–D3, F1–F4 → 93), **6.7** deploy notifications, **Part 12** Operating v8; 3.2, area table, roadmap, Appendix F |
| AI-Agent-Salesforce-Pipeline-Executive-Briefing-v8.pptx | 26 slides: new slide 15 "Merge to live \| v8" (6 cards); 158 offline tests; 93 issues; roadmap NOW incl. deploy tracking |
| Code v8 | fix/agent-stop-rules (copilot-instructions.md, invoke_copilot.py, review_gate.py, test_agent_stop.py); fix/dashboard-local-dates (dashboard_template.html); feat/v7.2-deploy-tracking (deploy_tracker.py, progress.py, watch_queue.py, pr_tracker.py, generate_pipeline_dashboard.py, dashboard_template.html, salesforce-deploy.yml, test_deploy_v72.py, test_dashboard_v71.py, test_step2_flow.py) |
| PROJECT-CONTEXT.md | This file (v8) |

## 18. How to resume
```powershell
cd C:\Users\pvellure\Downloads\SFRepo\EventRegistrationApp
.venv\Scripts\Activate.ps1
git checkout main; git pull --ff-only origin main; git status   # must be clean, on main
cd agent
python preflight.py; python security_checks.py
python watch_queue.py
python git_sync.py status            # main, clean : True
python pr_tracker.py                 # read-only PR table
python deploy_tracker.py             # deploy state per merged PR
python pr_tracker.py --snapshot; python generate_pipeline_dashboard.py
# evidence
Get-Content ..\logs\run-events.jsonl | ConvertFrom-Json | Format-Table time,ticket,step,status,ai_credits,outcome,reason_code
Get-Content ..\logs\<KEY>-usage.json
python generate_pipeline_dashboard.py; start ..\logs\pipeline-dashboard.html
sf apex run test --target-org eventreg-dev --class-names EventRegistrationControllerTest --synchronous --result-format human
# GitHub: Actions → Salesforce Deploy → Review deployments → approve (salesforce-deploy-gate)
Get-Content ..\logs\pr-state.json; Get-Content ..\logs\waiting.json; Get-Content ..\logs\batch-failed.json
```

## 19. Working preferences (how Pavan likes to work)

- Step by step: one concrete action, check the output, then the next step.
- Exact commands for PowerShell on a managed Windows machine.
- Ask to see the real file before patching it, and never assume function names (lesson from L6).
- Test before handing over.
- Be honest about what is proven versus simulated.
- Keep things crisp and clear for someone new to the system.
- Don't require reporters to know technical names.
- Think from the finance, client and security perspective.
- Enterprise-grade deliverables, with Deloitte black, white and green styling.
- Prefer solutions that keep working when vendors change things (e.g. auto-tier instead of model names).
- When sharing a code fix, give the full updated file to copy, not just a diff.
- Land every tooling fix on its own branch and PR; never on a ticket branch.
- Accepts human gates as the end state: "run the watcher, review the PR, merge, approve deploy" — automation up to the PR, people at merge and deploy.
- For demos with one GitHub account, prefer controls that work solo (environment approval) and document the team-grade version alongside.
- Wants dashboards generated by the pipeline itself (never hand-built in chat) and documents kept in sync after each milestone.
- Prefers to build all steps first and **test once at the end** (v7).
- Wants design thinking, options considered, pending items and future state captured in the documents, and the executive deck kept light on technical detail.
- Wants Jira Done only after a successful Salesforce deploy (v8 decision).
- Wants live step progress and start/end/duration per step visible both in PowerShell and on the dashboard.
- When short on time, wants complete replacement files plus updated documents delivered in one go, to apply and test later.
