"""Offline tests for run_console.py (the local run console).
Builds a FAKE agent folder in a temp directory (a fake watcher that prints output in the same formats as the real one,
including a review-gate child process that reads y/n from stdin). Never touches the real repo, Jira, GitHub or Salesforce.
Run from the agent folder:  python test_run_console.py"""
import http.client, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_console as rc

FAKE = {'watch_queue.py': '"""FAKE watcher for testing the run console. Prints output in the same formats as the real watch_queue.py."""\nimport os, sys, time, subprocess\nSC = os.environ.get("FAKE_SCENARIO", "success")\nSP = float(os.environ.get("FAKE_SPEED", "1"))\nKEY = os.environ.get("FAKE_KEY", "CLAUDE-24")\ndef p(s=""): print(s, flush=True)\ndef nap(x): time.sleep(x * SP)\np("  [git-sync] OK   (startup) On main @ 2c93eea, up to date with origin")\np("Running pre-flight and security checks...\\n"); nap(.2)\np("=" * 60); p("PRE-FLIGHT CHECKS"); p("=" * 60); p()\np("Repository state (incl. sync to latest origin/main)")\nfor l in ["A1 working tree clean", "A2 on base branch (main)", "A3 synced to latest origin/main"]: p("  PASS " + l)\np("Copilot CLI"); p("  PASS B1 Copilot CLI logged in"); p("  PASS B2 folder trusted for shell/write")\np("Credentials and config"); p("  PASS C1 Jira + GitHub reachable"); p("  PASS C2 GITHUB_REPO matches git remote"); p("  PASS C3 SF_TARGET_ORG set")\np("Salesforce"); p("  PASS D1 org \'eventreg-dev\' authenticated"); p("-" * 60)\np("ALL PRE-FLIGHT CHECKS PASSED - safe to start the watcher.\\n"); nap(.2)\np("  PASS S1 org is a sandbox, not production"); p("  PASS S2 main branch is protected"); p("  PASS S3 GitHub token scope is minimal"); nap(.2)\np("  [index] refreshed (startup): 211 files, 0 re-parsed")\np("\\nWatching CLAUDE for ai-ready tickets every 300s (Ctrl+C to stop)...")\np("Limits: budget $25/month, 5 tickets/day, 60 credits/ticket"); nap(.3)\np("  [pr-tracker] open ticket PRs: none | new updates: 0"); p("  [index] refreshed (poll cycle): 211 files, 0 re-parsed")\np("Claimed: " + KEY); nap(.2)\np("\\n" + "#" * 60 + f"\\n# Processing {KEY}  (run 8f3a1c)\\n" + "#" * 60)\np(f"\\n--- Re-checks before {KEY} ---")\np("  [git-sync] OK   (before ticket) On main @ 2c93eea, up to date with origin")\np("  PASS  working tree clean  "); p("  PASS  production-org guard  "); nap(.2)\nif SC == "waiting":\n    p(f"\\n>>> [1/6] prepare_fix.py {KEY}"); p("  [overlap] WAIT PR #31 (fix/CLAUDE-19-x) already changes: EventRegistrationController.cls")\n    p(f"[{KEY}] Waiting on an open PR (ai-waiting). No AI spend. Moving on.")\n    p("  [git-sync] OK   (end of ticket) On main @ 2c93eea, up to date with origin")\n    p("  [dashboard] rebuilt from 19 ticket run(s) -> logs/pipeline-dashboard.html")\n    nap(600); sys.exit(0)\np(f"\\n>>> [1/6] prepare_fix.py {KEY}"); nap(.4)\np("  [phase1] localisation confidence=0.77 complexity=moderate"); p("  routed to tier: standard (--model auto --auto-tier balance)")\np("  done: branch fix/CLAUDE-24-guest-count-above-10-is-accepted created from latest main")\np("  done: prompt file docs/ai-reports/CLAUDE-24-prompt.md"); nap(.3)\np(f"\\n>>> [2/6] invoke_copilot.py {KEY}")\np("  model  : --model auto --auto-tier balance"); p("  tier   : standard (routed by prepare_fix.py)")\np("  budget : $0.17 of $25.00 this month"); p("  Running Copilot CLI headlessly (timeout 15 min)..."); nap(1.2)\np("  cost: 0.87 AI credits (~$0.01) [source: usage_file]"); p("  changes: 2 commit(s) on fix/CLAUDE-24-guest-count-above-10-is-accepted")\np("  done. Log: logs/CLAUDE-24-copilot-cli.log"); nap(.2)\np(f"\\n>>> [3/6] review_gate.py {KEY}")\np("=" * 60); p(f"REVIEW GATE | {KEY}"); p("=" * 60)\np("\\nBranch: fix/CLAUDE-24-guest-count-above-10-is-accepted"); p("Commits: 2")\np("  3e91a02 fix(CLAUDE-24): enforce the 10-guest limit"); p("  8b22d4f test(CLAUDE-24): cover guest count boundaries")\np("Files changed: 5\\n\\nObjects/components impacted:\\n  Apex Classes:\\n    - force-app/main/default/classes/EventRegistrationController.cls\\n    - force-app/main/default/classes/EventRegistrationControllerTest.cls\\n  AI Reports / Docs:\\n    - docs/ai-reports/CLAUDE-24.md")\np("\\nDiff stat:\\n   ...EventRegistrationController.cls     |  2 +-\\n   ...EventRegistrationControllerTest.cls | 31 +++++\\n   3 files changed, 34 insertions(+), 1 deletion(-)")\np("\\n" + "-" * 60); sys.stdout.flush()\n# the real review gate is a CHILD process that inherits stdin/stdout: do the same\nrc = subprocess.run([sys.executable, "-u", "-c",\n    "import sys,os\\nopen(os.environ.get(\'FAKE_CHILD_PID\',\'child.pid\'),\'w\').write(str(os.getpid()))\\nans=input(\'Proceed to validation + draft PR + Jira update? [y/N]: \').strip().lower()\\n"\n    "print(\'\\\\nApproved. Proceeding to Step 7 (validate_fix.py).\' if ans in (\'y\',\'yes\') else \'\\\\nRejected. Branch fix/CLAUDE-24-guest-count-above-10-is-accepted left as-is for inspection.\')\\n"\n    "sys.exit(0 if ans in (\'y\',\'yes\') else 1)"]).returncode\ndef end_of_ticket():\n    p("  [git-sync] OK   (end of ticket) On main @ 2c93eea, up to date with origin")\n    p("  [dashboard] rebuilt from 19 ticket run(s) -> logs/pipeline-dashboard.html"); p("  [index] refreshed (end of ticket): 211 files, 0 re-parsed")\nif rc != 0:\n    p(f"[{KEY}] Stopped at review_gate.py."); end_of_ticket(); nap(600); sys.exit(0)\nnap(.3); p(f"\\n>>> [4/6] validate_fix.py {KEY}")\np("=" * 60); p(f"AGENT VALIDATION | {KEY} | base: main"); p("=" * 60)\nchecks = ["On the ticket\'s fix branch - fix/CLAUDE-24-guest-count-above-10-is-accepted", "Working tree clean", "Has at least one commit - 2 commit(s)",\n          "Commit messages reference ticket", "Code files changed <= 5 - 2 file(s)", "Only allowed paths changed", "No restricted files changed",\n          "Code lines changed <= 200 - 34 line(s)", "No secrets in added lines", "No SeeAllData=true", "No \'without sharing\' introduced",\n          "Apex test updated or added", "Solution Report committed - docs/ai-reports/CLAUDE-24.md", "CLAUDE-24-pr.md exists", "CLAUDE-24-jira.md exists"]\nfor i, c in enumerate(checks):\n    p("  PASS  " + c); nap(.06)\nif SC == "validate_fail":\n    p("  ....  Running Apex tests in \'eventreg-dev\' (validate-only, rolled back): EventRegistrationControllerTest"); nap(.6)\n    p("  FAIL  Apex tests in org - 20 run, 1 failed, status Failed | EventRegistrationControllerTest.testTooManyGuestsIsRejected")\n    p("-" * 60); p("BLOCKED: 1 check(s) failed. Do not push.")\n    p(f"[{KEY}] Stopped at validate_fix.py.")\n    p("\\n" + "!" * 60); p("! NOTIFICATION [failure]: Pipeline failed: " + KEY); p("!   Step: validate_fix.py")\n    p("!   Guidance: Check which specific check(s) printed FAIL - common ones: a real Apex test failure."); p("!" * 60)\n    end_of_ticket(); nap(600); sys.exit(0)\np("  ....  Running Apex tests in \'eventreg-dev\' (validate-only, rolled back): EventRegistrationControllerTest"); nap(.7)\np("  PASS  Apex tests in org - 20 run, 0 failed, status Succeeded"); p("-" * 60); p("ALL CHECKS PASSED. Safe to proceed to Step 8 (push + draft PR).")\nnap(.2); p(f"\\n>>> [5/6] create_pr.py {KEY}")\np("=" * 60); p(f"AGENT CREATE PR | {KEY} | Dry run: False"); p("=" * 60)\np("\\nRe-running validation before push:"); p("  done: pushed fix/CLAUDE-24-guest-count-above-10-is-accepted"); nap(.3)\np("  done: DRAFT PR created: https://github.com/pvellure-hash/EventRegistrationDemo/pull/33")\np("\\n" + "-" * 60); p("PR: https://github.com/pvellure-hash/EventRegistrationDemo/pull/33"); p("Saved to logs/CLAUDE-24-pr.json (used by Step 9 Jira update)")\nnap(.2); p(f"\\n>>> [6/6] update_jira.py {KEY}"); p("  done: comment added"); p("  done: label ai-pr-created"); p("  done: status In Review"); nap(.2)\np(f"[{KEY}] Completed in 3m 12s. Draft PR awaiting human review/merge. Moving on to the next ticket.")\nend_of_ticket(); nap(.4)\n# later polls: the person merges, then approves the deploy\npd = float(os.environ.get("FAKE_POST_DELAY", "0.6"))\ntime.sleep(pd); p("  [pr-tracker] open ticket PRs: #33 | new updates: 1")\np("  [pr-tracker] MERGED   PR #33 CLAUDE-24 ")\ntime.sleep(pd); p("  [pr-tracker] open ticket PRs: none | new updates: 0"); p("  [deploy] PR #33 CLAUDE-24: deploy pending -> awaiting_approval")\ntime.sleep(pd * 1.5); p("  [deploy] PR #33 CLAUDE-24: deploy awaiting_approval -> deploying")\ntime.sleep(pd * 1.5); p("  [deploy] PR #33 CLAUDE-24: deploy deploying -> deployed"); p("  [deploy]   CLAUDE-24: deployed -> Done")\ntime.sleep(600)\n', 'preflight.py': 'import sys\nprint("============================================================\\nPRE-FLIGHT CHECKS\\n============================================================")\nprint("Repository state\\n  PASS A1 working tree clean\\n  PASS A2 on base branch (main)\\n  FAIL A3 synced to latest origin/main        behind by 1 commit")\nprint("------------------------------------------------------------\\nPRE-FLIGHT CHECKS FAILED - fix the items above before starting the watcher."); sys.exit(1)\n', 'generate_pipeline_dashboard.py': 'import os\nos.makedirs("../logs", exist_ok=True); open("../logs/pipeline-dashboard.html","w").write("<html><body>dashboard</body></html>")\nprint("[dashboard] rebuilt from 4 ticket run(s), 2 merged, 2 deployed -> ../logs/pipeline-dashboard.html")\n', 'read_queue.py': 'def fetch_queue():\n    return [{"key":"CLAUDE-24","fields":{"summary":"Guest count above 10 is accepted on event registration","issuetype":{"name":"Bug"}}},\n            {"key":"CLAUDE-25","fields":{"summary":"Submit button label is unclear","issuetype":{"name":"Bug"}}}]\n'}
TMP = Path(tempfile.mkdtemp(prefix="rc-test-"))
AG = TMP / "agent"; AG.mkdir(); (TMP / "logs").mkdir()
for n, s in FAKE.items(): (AG / n).write_text(s, encoding="utf-8")
shutil.copy(HERE / "run_console.html", AG / "run_console.html")
os.environ.update(FAKE_CHILD_PID=str(AG / "child.pid"), FAKE_SPEED="0.03", FAKE_POST_DELAY="0.12", FAKE_KEY="CLAUDE-24")
ok = 0; T0 = time.time()
def chk(c, m):
    global ok
    if not c: raise AssertionError(m)
    ok += 1; print(f"  PASS [{time.time()-T0:5.1f}s]", m)
def wait(cond, secs=15, what="condition"):
    t = time.time()
    while time.time() - t < secs:
        if cond(): return True
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)

def run_fake(scn, answer):
    env = dict(os.environ, FAKE_SCENARIO=scn, PYTHONUNBUFFERED="1")
    import subprocess
    p = subprocess.Popen([sys.executable, "-u", "watch_queue.py"], cwd=str(AG), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    p.stdin.write(answer + "\n"); p.stdin.flush()
    out = []
    t0 = time.time()
    for l in p.stdout:
        out.append(l.rstrip("\n"))
        if scn == "success" and answer == "y" and "deployed -> Done" in l: break
        if (scn != "success" or answer != "y") and any("[dashboard] rebuilt" in x for x in out): break
        if time.time() - t0 > 20: break
    p.kill(); return out
def parse(lines, answer="y"):
    P = rc.Parser(); evs = []; t = 1000
    for l in lines:
        t += 100
        if l.startswith("Proceed to validation"):
            evs += P.prompt(l, t); t += 50; evs += P.answered(answer, t)
        evs += P.feed(l, t)
    return P, evs
def st(P, jid, i): return P.steps[jid][i]["status"]

print("parser: successful run")
OK_LINES = run_fake("success", "y")
P, evs = parse(OK_LINES)
jid = "CLAUDE-24#1"; j = P.jobs[jid]
chk(P.jobs["startup"]["status"] == "ok" and P.jobs["poll"]["status"] == "running", "start-up finished, polling started")
chk(len(P.steps["startup"][2]["checks"]) >= 10 and all(c["s"] == "PASS" for c in P.steps["startup"][2]["checks"]), "pre-flight and security checks captured as a checklist")
chk(all(st(P, jid, i) == "ok" for i in range(1, 14)) and j["status"] == "ok", "all 13 steps succeeded and the ticket is live")
chk(j["prNumber"] == 33 and j["pr"].endswith("/pull/33"), "draft PR link and number captured")
chk(abs(j["credits"] - 0.87) < 1e-9, "AI cost read from the agent step")
f4 = {f["k"]: f["v"] for f in P.steps[jid][4]["facts"]}
chk("AI cost" in f4 and "Model" in f4 and "Tier" in f4, "agent step shows cost, model and tier")
f5 = {f["k"]: f["v"] for f in P.steps[jid][5]["facts"]}
chk(f5.get("Files changed") == "5" and f5.get("Commits") == "2", "review gate shows files changed and commits")
chk(len(P.steps[jid][6]["checks"]) == 16, "validation checks captured (15 static + Apex)")
cmd = [e for e in evs if e["type"] == "line" and e["job"] == jid and e["step"] == 4 and e["lvl"] == "cmd"]
chk(cmd and cmd[0]["text"] == "$ python invoke_copilot.py CLAUDE-24", "the command each step runs is shown")
chk(any(e["type"] == "line" and e["lvl"] == "you" and e["job"] == jid for e in evs), "the review answer is recorded in the log")
chk(P.steps[jid][10]["start"] and P.steps[jid][12]["end"] >= P.steps[jid][12]["start"], "post-merge steps carry timings")

print("parser: failures")
P, evs = parse(run_fake("validate_fail", "y")); j = P.jobs[jid]
chk(st(P, jid, 6) == "failed" and j["status"] == "failed", "validation failure stops the ticket at step 6")
chk(all(st(P, jid, i) == "skipped" for i in (7, 8, 10, 11, 12, 13)), "later steps are skipped")
chk(st(P, jid, 9) == "ok", "the repository is still returned to main")
chk(any("validate_fix.py" in a for a in j["alert"]), "the pipeline alert text is captured")
chk(any(c["s"] == "FAIL" for c in P.steps[jid][6]["checks"]), "the failing check is visible")
P, evs = parse(run_fake("waiting", "y")); j = P.jobs[jid]
chk(j["status"] == "waiting" and st(P, jid, 3) == "waiting" and st(P, jid, 4) == "skipped", "ai-waiting: ticket waits, no AI step runs")
P, evs = parse(run_fake("success", "n"), answer="n"); j = P.jobs[jid]
chk(st(P, jid, 5) == "failed" and j["status"] == "failed" and st(P, jid, 6) == "skipped", "rejecting at the review gate fails that step and skips the rest")

print("parser: robustness")
P = rc.Parser(); P.feed("something the parser has never seen", 1); out = P.feed("another unknown line", 2)
chk(any(e["type"] == "line" for e in out), "unknown lines are never dropped")
P = rc.Parser(); P.feed("Watching CLAUDE for ai-ready tickets every 300s", 1); out = P.feed("Claimed: none", 2)
chk(not any(e["type"] == "job" for e in out) and not [k for k in P.jobs if k.startswith("CLAUDE")], "'Claimed: none' does not create a ticket")
a = rc.Parser(); b = rc.Parser(); L = OK_LINES
ea = [e for l in L for e in a.feed(l, 5)]; eb = [e for l in L for e in b.feed(l, 5)]
chk(json.dumps(ea) == json.dumps(eb), "the parser is deterministic (replay gives identical events)")
chk(rc.redact("x ghp_" + "a" * 30 + " y") == "x [REDACTED] y" and "ATATT" not in rc.redact("ATATT" + "b" * 30), "token-like text is redacted")

# the claim line may be indented, or missing: the ticket must still appear
L = [("  " + l if l.startswith("Claimed:") else l) for l in OK_LINES]
P, _ = parse(L); chk(P.jobs["CLAUDE-24#1"]["status"] == "ok", "an indented 'Claimed:' line still creates the ticket")
L = [l for l in OK_LINES if not l.startswith("Claimed:")]
P, _ = parse(L); chk(P.jobs["CLAUDE-24#1"]["status"] == "ok" and st(P, "CLAUDE-24#1", 1) == "ok", "a ticket is created even if the claim line is missing")
P, _ = parse(OK_LINES + [l for l in OK_LINES if l.startswith(("Claimed:", "#", ">>>", "[CLAUDE"))][:0]); chk(len([k for k in P.jobs if k.startswith("CLAUDE")]) == 1, "one ticket gives exactly one job")
P = rc.Parser(); P.begin(500)
chk(P.jobs["startup"]["start"] == 500 and P.steps["startup"][1]["status"] == "running", "the start-up card runs from the moment the watcher is launched")
print("hub: live run through the real process machinery")
hub = rc.Hub(AG, "tok123")
os.environ["FAKE_SCENARIO"] = "success"
hub.start_watcher()
chk(hub.status in ("starting", "running"), "watcher starts")
try: hub.start_watcher(); chk(False, "second start must be refused")
except RuntimeError: chk(True, "a second watcher cannot be started")
wait(lambda: hub.prompt is not None, 20, "the review-gate prompt")
chk(hub.prompt["key"] == "CLAUDE-24", "the review gate prompt is detected from a partial line")
chk(any(e["type"] == "state" and e.get("active") == "CLAUDE-24" for e in hub.events), "state is pushed as soon as a ticket becomes active")
snap = hub.snapshot(); chk(snap["prompt"] and snap["active"] == "CLAUDE-24", "state says the page must ask for approval")
chk(any(e["type"] == "prompt" for e in hub.events), "a prompt event is sent to the page")
hub.answer("y")
chk(hub.prompt is None, "the prompt clears after the answer")
wait(lambda: any(e["type"] == "job" and e["job"]["id"] == "CLAUDE-24#1" and e["job"]["status"] == "ok" for e in hub.events), 25, "ticket live")
chk(True, "the answer reached the review-gate child process and the run completed through deploy")
chk(hub.snapshot()["active"] is None, "after the ticket hands off to review, the console no longer says it is processing it")
try: hub.answer("y"); chk(False, "answer with nothing waiting must be refused")
except RuntimeError: chk(True, "an answer is refused when nothing is waiting")
sf = sorted((TMP / "logs" / "run-console").glob("session-*.jsonl")); chk(len(sf) == 1, "a transcript was saved")
rep = hub.replay(sf[0].name); jobs = {e["job"]["id"]: e["job"] for e in rep if e["type"] == "job"}
chk(jobs["CLAUDE-24#1"]["status"] == "ok" and jobs["CLAUDE-24#1"]["prNumber"] == 33, "the saved transcript replays to the same result")
try: hub.replay("../../etc/passwd"); chk(False, "bad session name")
except ValueError: chk(True, "session names are validated (no path traversal)")
hub.stop(); wait(lambda: hub.proc is None, 10, "stop"); chk(hub.status == "stopped", "stop ends the watcher")

print("hub: stop in the middle of a ticket, and the safety rules")
os.environ["FAKE_SPEED"] = "0.15"
hub = rc.Hub(AG, "tok123"); hub.start_watcher()
wait(lambda: hub.prompt is not None, 30, "prompt")
pid = hub.proc.pid; popen_ref = hub.proc; hub.stop(); wait(lambda: hub.proc is None, 10, "stop")
def alive(pid):
    """Portable liveness check. os.kill(pid, 0) must NOT be used on Windows: it kills the process there."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:    # a zombie that has exited but not been reaped still answers kill(0)
        return open(f"/proc/{pid}/stat").read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True
chk(popen_ref.poll() is not None, "stopping ends the watcher process")
chk(wait(lambda: not alive(pid), 10, "watcher gone"), "the watcher process is really gone")
cp = AG / "child.pid"
if cp.exists():
    cpid = int(cp.read_text()); chk(wait(lambda: not alive(cpid), 10, "child gone"), "the review-gate child process is gone too")
jb = {e["job"]["id"]: e["job"] for e in hub.events if e["type"] == "job"}["CLAUDE-24#1"]
chk(jb["status"] == "failed" and "Stopped by you" in jb["note"], "a ticket stopped mid-run is marked as stopped by you")
chk("ai-locked" in hub.snapshot()["hint"], "the page is told to set the ticket back to ai-ready")
try: hub.run_util("rm -rf"); chk(False, "unknown action")
except ValueError: chk(True, "only the three allow-listed scripts can be started")
hub.run_util("preflight"); wait(lambda: any(e["type"] == "job" and e["job"]["id"] == "util-1" and e["job"]["status"] == "failed" for e in hub.events), 10, "util")
us = [e for e in hub.events if e["type"] == "step" and e["job"] == "util-1"][-1]["step"]
chk(any(c["s"] == "FAIL" for c in us["checks"]), "the pre-flight check shows its failing item")
hub.run_util("dashboard"); wait(lambda: (TMP / "logs" / "pipeline-dashboard.html").exists(), 10, "dashboard")
chk(True, "the dashboard rebuild runs from the page")

print("http: security and endpoints")
srv, hub2 = rc.make_server(AG, 8900, token="secret-token")
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
def get(path, tok="secret-token", host=None, hdr=None, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    h = {"Host": host or f"127.0.0.1:{port}"}
    if tok is not None: h["X-Console-Token"] = tok
    h.update(hdr or {}); c.request(method, path, body=body, headers=h); r = c.getresponse(); d = r.read(); c.close(); return r.status, d
chk(get("/", tok=None)[0] == 200, "the page loads without a token (it contains no data)")
chk(get("/api/state", tok=None)[0] == 401 and get("/api/state", tok="wrong")[0] == 401, "the API refuses a missing or wrong token")
chk(get("/api/state", host="evil.example.com")[0] == 403, "a foreign Host header is refused (DNS-rebinding guard)")
chk(get("/api/start", method="POST", body="{}", hdr={"Origin": "http://evil.example.com"})[0] == 403, "a foreign Origin is refused on actions")
s, d = get("/api/state"); chk(s == 200 and "context" in json.loads(d), "state endpoint works with the token")
chk(get("/dashboard")[0] in (200, 404) and get("/dashboard", tok=None)[0] == 401, "the dashboard needs the token")
s, d = get("/api/queue"); q = json.loads(d); chk(q["ok"] and q["items"][0]["key"] == "CLAUDE-24", "the ready queue preview works")
s, d = get("/api/util", method="POST", body=json.dumps({"name": "evil"})); chk(s == 409, "an unknown action is refused over HTTP")
s, d = get("/api/answer", method="POST", body=json.dumps({"answer": "y"})); chk(s == 409, "an answer with nothing waiting is refused over HTTP")
os.environ["FAKE_SPEED"] = "0.03"
s, d = get("/api/start", method="POST", body="{}"); chk(s == 200, "start works over HTTP")
s, d = get("/api/start", method="POST", body="{}"); chk(s == 409, "a second start is refused over HTTP")
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", f"/api/stream?after=0&t=secret-token", headers={"Host": f"127.0.0.1:{port}"})
r = c.getresponse(); chk(r.status == 200 and "text/event-stream" in r.getheader("Content-Type"), "the live stream opens")
buf = b""; t0 = time.time()
while time.time() - t0 < 6 and b'"type": "prompt"' not in buf: buf += r.read1(4096)
chk(b'"type": "reset"' in buf and b'"type": "prompt"' in buf, "the stream delivers events, including the review-gate prompt")
c.close()
s, d = get("/api/answer", method="POST", body=json.dumps({"answer": "y"})); chk(s == 200, "Approve works over HTTP")
wait(lambda: any(e["type"] == "job" and e["job"]["status"] == "ok" and e["job"]["id"] == "CLAUDE-24#1" for e in hub2.events), 25, "done")
s, d = get("/api/transcript"); chk(s == 200 and b"invoke_copilot.py CLAUDE-24" in d, "the transcript downloads")
s, d = get("/api/stop", method="POST", body="{}"); wait(lambda: hub2.proc is None, 10, "stop"); chk(s == 200, "Stop works over HTTP")
srv.stopping = True; srv.shutdown()
print(f"\n{ok}/{ok} passed")
shutil.rmtree(TMP, ignore_errors=True)
