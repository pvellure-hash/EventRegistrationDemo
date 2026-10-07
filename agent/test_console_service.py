"""Offline tests for console_service.py (the background-service layer of the run console).
Builds a FAKE agent folder in a temp directory with a fake watcher that prints output in the same formats as the real
one (including a review-gate child process that reads y/n from stdin). Never touches the real repo, Jira, GitHub or Salesforce.
Run from the agent folder:  python test_console_service.py"""
import base64, http.client, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_console as rc
import console_service as cs
import install_console_app as inst

FAKE_WATCHER = r'''
import os, sys, time, subprocess
def p(s=""): print(s, flush=True)
SP = float(os.environ.get("FAKE_SPEED", "1")); KEY = os.environ.get("FAKE_KEY", "CLAUDE-24")
def nap(x): time.sleep(x * SP)
p("  [git-sync] OK   (startup) On main @ 2c93eea, up to date with origin"); p("Running pre-flight and security checks...\n"); nap(.1)
p("PRE-FLIGHT CHECKS"); p("  PASS A1 working tree clean"); p("  PASS D1 org 'eventreg-dev' authenticated"); nap(.1)
p("  [index] refreshed (startup): 211 files, 0 re-parsed"); p("\nWatching CLAUDE for ai-ready tickets every 300s (Ctrl+C to stop)...")
nap(.2); p("Claimed: " + KEY); p("\n" + "#" * 60 + f"\n# Processing {KEY}  (run 8f3a1c)\n" + "#" * 60)
p("  [git-sync] OK   (before ticket) On main @ 2c93eea, up to date with origin"); p("  PASS  working tree clean  "); nap(.1)
p(f"\n>>> [1/6] prepare_fix.py {KEY}"); p("  [phase1] localisation confidence=0.77 complexity=moderate"); nap(.1)
p(f"\n>>> [2/6] invoke_copilot.py {KEY}"); p("  cost: 0.87 AI credits (~$0.01) [source: usage_file]"); p("  changes: 2 commit(s) on fix/CLAUDE-24-x"); nap(.1)
p(f"\n>>> [3/6] review_gate.py {KEY}"); p("REVIEW GATE | " + KEY); p("Commits: 2"); p("Files changed: 5"); p("-" * 60); sys.stdout.flush()
rc = subprocess.run([sys.executable, "-u", "-c",
    "import sys\nans=input('Proceed to validation + draft PR + Jira update? [y/N]: ').strip().lower()\n"
    "print('\\nApproved.' if ans in ('y','yes') else '\\nRejected.')\nsys.exit(0 if ans in ('y','yes') else 1)"]).returncode
def end():
    p("  [git-sync] OK   (end of ticket) On main @ 2c93eea, up to date with origin"); p("  [dashboard] rebuilt from 5 ticket run(s)")
if rc != 0:
    p(f"[{KEY}] Stopped at review_gate.py."); end(); time.sleep(600); sys.exit(0)
nap(.1); p(f"\n>>> [4/6] validate_fix.py {KEY}"); p("  PASS  Working tree clean"); p("ALL CHECKS PASSED. Safe to proceed to Step 8 (push + draft PR).")
p(f"\n>>> [5/6] create_pr.py {KEY}"); p("  done: DRAFT PR created: https://github.com/example/repo/pull/33"); p("PR: https://github.com/example/repo/pull/33")
p(f"\n>>> [6/6] update_jira.py {KEY}"); p("  done: comment added")
p(f"[{KEY}] Completed in 3m 12s. Draft PR awaiting human review/merge. Moving on to the next ticket."); end()
d = float(os.environ.get("FAKE_POST_DELAY", "0.4"))
if os.environ.get("FAKE_HOLD_MERGE") == "1": time.sleep(600)
time.sleep(d); p("  [pr-tracker] MERGED   PR #33 CLAUDE-24 ")
time.sleep(d); p("  [deploy] PR #33 CLAUDE-24: deploy pending -> awaiting_approval")
time.sleep(d); p("  [deploy] PR #33 CLAUDE-24: deploy awaiting_approval -> deploying")
time.sleep(d); p("  [deploy] PR #33 CLAUDE-24: deploy deploying -> deployed"); p("  [deploy]   CLAUDE-24: deployed -> Done")
time.sleep(600)
'''
IDLE_WATCHER = 'import time\nprint("  [git-sync] OK   (startup) On main @ 1, up to date", flush=True)\nprint("\\nWatching CLAUDE for ai-ready tickets every 300s (Ctrl+C to stop)...", flush=True)\ntime.sleep(600)\n'

TMP = Path(tempfile.mkdtemp(prefix="cs-test-"))
AG = TMP / "agent"; AG.mkdir(); (TMP / "logs").mkdir()
(AG / "watch_queue.py").write_text(FAKE_WATCHER, encoding="utf-8")
shutil.copy(HERE / "run_console.html", AG / "run_console.html")
SD = TMP / "logs" / "run-console"
os.environ.update(FAKE_SPEED="0.05", FAKE_POST_DELAY="0.3")
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
SCRIPT = str(HERE / "console_service.py")
def cli(*args, env=None, timeout=40):
    return subprocess.run([sys.executable, SCRIPT, "--agent-dir", str(AG), *args], capture_output=True, text=True, timeout=timeout, env=env or os.environ)

print("pure helpers")
chk(cs.pid_alive(os.getpid()) and not cs.pid_alive(0) and not cs.pid_alive(None) and not cs.pid_alive(4_000_000), "pid_alive: this process yes, nonsense no")
cmd = base64.b64decode(cs.toast_command('Approval <needed> & "now"', "CLAUDE-24: it's ready")).decode("utf-16-le")
chk("&lt;needed&gt; &amp; &quot;now&quot;" in cmd and "it&apos;s ready" in cmd.replace("''", "'") and "ToastNotificationManager" in cmd, "toast text is XML-escaped and the command is well formed")
chk(cs.console_python().lower().endswith(("python", "python3", "python.exe", "python3.12", "python3.13", "python3.11")) or "python" in cs.console_python().lower(), "the watcher is given a console-subsystem interpreter")
sd = TMP / "logs" / "s1"; sd.mkdir(parents=True)
chk(cs.load_settings(sd) == {"notify": True, "autoStartWatcher": False}, "default settings: notify on, auto-start off")
cs.save_settings(sd, {"notify": False, "autoStartWatcher": True, "junk": 1}); chk(cs.load_settings(sd) == {"notify": False, "autoStartWatcher": True}, "settings round-trip; unknown keys are ignored")
(sd / "settings.json").write_text("{not json"); chk(cs.load_settings(sd) == {"notify": True, "autoStartWatcher": False}, "a damaged settings file falls back to defaults")
sh = inst.build_script(inst.items(startup=True))
chk("AI Delivery Console.lnk" in sh and "--app" in sh and "--background" in sh and "Startup" in sh and "console_service.py" in sh, "installer script creates the desktop and start-up shortcuts")
chk("Remove-Item" in inst.build_script(inst.items(startup=True), remove=True) and inst.ps_quote("it's") == "'it''s'", "installer remove script and quoting")
chk(not any(str(HERE) in it["name"] for it in inst.items(True)) and all(it["where"] in ("Desktop", "Startup") for it in inst.items(True)), "shortcuts go to Desktop/Startup, never inside the repository")

print("hub: orphan watcher guard")
hub = cs.ServiceHub(AG, "tok")
chk(hub.orphan is None, "no orphan on a clean start")
sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
(SD).mkdir(parents=True, exist_ok=True); (SD / "watcher.pid").write_text(json.dumps({"pid": sleeper.pid, "started": 1}))
hub = cs.ServiceHub(AG, "tok")
chk(hub.orphan and hub.orphan["pid"] == sleeper.pid and hub.snapshot()["orphan"]["pid"] == sleeper.pid, "a watcher left by an earlier service is detected and reported")
try: hub.start_watcher(); chk(False, "start must be refused")
except RuntimeError as e: chk("earlier session" in str(e), "a second watcher cannot start while an orphan is running")
hub.kill_orphan(); sleeper.wait(timeout=10)
chk(hub.orphan is None and not (SD / "watcher.pid").exists() and not cs.pid_alive(sleeper.pid), "the orphan can be stopped from the page")
(SD / "watcher.pid").write_text(json.dumps({"pid": 4_000_000, "started": 1})); hub = cs.ServiceHub(AG, "tok")
chk(hub.orphan is None and not (SD / "watcher.pid").exists(), "a stale watcher.pid is cleaned up")

print("hub: notifications")
calls = []
hub = cs.ServiceHub(AG, "tok"); hub.notifier = lambda h, t, m, w: calls.append((t, m, w))
hub.window_url = "http://127.0.0.1:1/#t=x"
hub.notify("Approval needed", "CLAUDE-24: review", window=True); wait(lambda: len(calls) == 1, 3, "notifier")
chk(calls[0][2] is True, "no window open: the approval alert also opens the window")
hub.client_connected(); calls.clear(); hub.notify("Approval needed", "x", window=True); wait(lambda: len(calls) == 1, 3, "notifier")
chk(calls[0][2] is False, "a window is already open: notification only, no second window")
hub.update_settings({"notify": False}); calls.clear(); hub.notify("Approval needed", "x", window=True); time.sleep(0.3)
chk(calls == [] and cs.load_settings(SD)["notify"] is False, "turning notifications off silences them and is saved")
hub.update_settings({"notify": True, "bogus": True}); chk("bogus" not in hub.settings, "only known settings can be changed")
hub.client_disconnected(); hub.client_disconnected(); chk(hub.clients == 0, "the window counter never goes below zero")
# step/job events produce the right notifications, once each
hub = cs.ServiceHub(AG, "tok"); calls = []; hub.notifier = lambda h, t, m, w: calls.append((t, m, w))
P = rc.Parser(); hub.parser = P; t = 1000
P._mk_ticket("CLAUDE-24", t); P.ctx = "poll"
evs = []
for jid in ["CLAUDE-24#1"]:
    P._set(jid, 10, "waiting", t); P._set(jid, 11, "waiting", t); P._job_status(jid, "ok", t)
evs = P.out; P.out = []
hub.emit(evs); hub.emit(evs); wait(lambda: len(calls) >= 3, 3, "notifications"); time.sleep(0.3)
titles = sorted(c[0] for c in calls)
chk(titles == ["Approve the deploy", "Live in Salesforce", "Ready to merge"], "merge, deploy-approval and live each notify exactly once")
chk(all(c[2] is False for c in calls), "those notifications never open a window by themselves")

print("hub: event history is bounded")
hub = cs.ServiceHub(AG, "tok"); hub.MAX_EVENTS = 300; hub.KEEP_LINES = 40
hub.emit([{"type": "reset"}]); hub.emit([{"type": "job", "job": {"id": "poll", "kind": "poll", "status": "running"}}])
for i in range(1200): hub.emit([{"type": "line", "job": "poll", "step": 1, "n": i, "t": i, "text": f"poll line {i}", "lvl": "info"}])
hub.emit([{"type": "step", "job": "poll", "step": {"idx": 1, "status": "running", "facts": [], "checks": []}}])
hub.emit([{"type": "job", "job": {"id": "T#1", "kind": "ticket", "key": "T", "status": "running"}}])
n = len(hub.events); kinds = [e["type"] for e in hub.events]
chk(n < 400 and kinds[0] == "reset", f"a long-running service keeps only a bounded history ({n} events)")
lines = [e["n"] for e in hub.events if e["type"] == "line"]
chk(lines and lines[-1] == 1199 and lines[0] > 800 and len(lines) < 300, "the most recent lines are kept, older ones dropped")
chk(sum(1 for e in hub.events if e["type"] == "job") == 2 and any(e["type"] == "step" for e in hub.events), "the latest state of every job and step is kept")
seqs = [e["seq"] for e in hub.events]; chk(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sequence numbers stay unique and ordered")
ev, last = hub.wait_events(seqs[-1] - 1, 1); chk(len(ev) == 1, "a connected window keeps receiving new events after compaction")

print("service: a real detached background service")
env = dict(os.environ); env["FAKE_SPEED"] = "0.05"
r = cli("--background", "--port", "8950", env=env)
chk(r.returncode == 0 and "Started the background service" in r.stdout, "the launcher starts the service")
info = cs.read_info(AG); chk(info and cs.pid_alive(info["pid"]) and cs.responsive(info), "daemon.json is written and the service answers")
launcher_pid = os.getpid(); chk(info["pid"] != launcher_pid, "the service is its own process (survives the launcher exiting)")
r2 = cli("--background", "--port", "8950"); info2 = cs.read_info(AG)
chk("already running" in r2.stdout and info2["pid"] == info["pid"] and info2["port"] == info["port"], "starting again reuses the same service (one at a time)")
s, body = cs.call(info, "/api/state"); st = body["state"]
chk(st["daemon"]["mode"] == "background" and st["settings"]["notify"] is True and st["orphan"] is None and st["clients"] == 0, "state reports background mode, settings, no orphan, no windows")
conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=10)
conn.request("GET", "/", headers={"Host": f"127.0.0.1:{info['port']}"}); page = conn.getresponse().read().decode(); conn.close()
chk("Run console" in page and 'id="svcPop"' in page.replace("'", '"') or "svcPop" in page, "the original page is served with the service panel added")
chk(page.count("</body>") == 1 and page.rfind("svcChip") < page.rfind("</body>"), "the panel is injected just before </body>")
s, _ = cs.call(info, "/api/service/settings", "POST", {"autoStartWatcher": False, "notify": True}); chk(s == 200, "settings can be saved from the page")
s, _ = cs.call({**info, "token": "wrong"}, "/api/service/settings", "POST", {"notify": False}); chk(s == 401, "service endpoints need the token")
s, _ = cs.call(info, "/api/service/kill-orphan", "POST", {}); chk(s == 409, "stopping an orphan with none present is refused cleanly")
# start the watcher through the service, 'close the window', and check it keeps going
s, _ = cs.call(info, "/api/start", "POST", {}); chk(s == 200, "start the watcher through the service")
stream = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=15)
stream.request("GET", f"/api/stream?after=0&t={info['token']}", headers={"Host": f"127.0.0.1:{info['port']}"}); resp = stream.getresponse()
buf = b""; t0 = time.time()
while time.time() - t0 < 12 and b'"type": "prompt"' not in buf: buf += resp.read1(4096)
chk(b'"type": "prompt"' in buf, "a window sees the review-gate prompt")
st = cs.call(info, "/api/state")[1]["state"]; chk(st["clients"] == 1 and st["prompt"] and st["active"] == "CLAUDE-24", "the service knows one window is open and the ticket is waiting for approval")
wpid = st["pid"]; resp.close(); stream.close()                       # the window is closed
wait(lambda: cs.call(info, "/api/state")[1]["state"]["clients"] == 0, 15, "window count to drop")
st = cs.call(info, "/api/state")[1]["state"]
chk(st["status"] == "running" and st["prompt"] and cs.pid_alive(wpid), "CLOSING THE WINDOW DOES NOT STOP THE WATCHER: it is still waiting at the review gate")
# open the window again (new stream from 0) and approve
stream = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=15)
stream.request("GET", f"/api/stream?after=0&t={info['token']}", headers={"Host": f"127.0.0.1:{info['port']}"}); resp = stream.getresponse()
buf = b""; t0 = time.time()
while time.time() - t0 < 8 and b'"type": "prompt"' not in buf: buf += resp.read1(4096)
chk(b'"type": "reset"' in buf and b'CLAUDE-24' in buf and b'"type": "prompt"' in buf, "reopening the window replays the whole run so far, including the pending approval")
s, _ = cs.call(info, "/api/answer", "POST", {"answer": "y"}); chk(s == 200, "approve from the reopened window")
buf = b""; t0 = time.time()
while time.time() - t0 < 15 and b'deployed' not in buf: buf += resp.read1(4096)
chk(b'"status": "ok"' in buf and b"deployed" in buf, "the run continues to Live in Salesforce")
resp.close(); stream.close()
chk((SD / "watcher.pid").exists(), "the watcher's pid is recorded while it runs")
log = (SD / "daemon.log").read_text(encoding="utf-8") if (SD / "daemon.log").exists() else ""
chk("service ready on 127.0.0.1" in log, "the service writes daemon.log (nothing is printed to a window)")
# quit
r = cli("--quit"); chk("stopped" in r.stdout, "--quit reports success")
wait(lambda: not cs.pid_alive(wpid), 10, "watcher gone"); chk(cs.read_info(AG) is None and not cs.pid_alive(info["pid"]), "quitting stops the service, removes daemon.json and stops the watcher too")
chk(not (SD / "watcher.pid").exists(), "the watcher pid file is removed")

print("service: hard kill leaves an orphan, and the next service guards against it")
r = cli("--background", "--port", "8951"); info = cs.read_info(AG); chk(info and cs.responsive(info), "service started again")
os.environ["FAKE_HOLD_MERGE"] = "1"
(AG / "watch_queue.py").write_text(IDLE_WATCHER, encoding="utf-8")
cs.call(info, "/api/start", "POST", {}); wait(lambda: cs.call(info, "/api/state")[1]["state"]["status"] == "running", 10, "running")
wpid = cs.call(info, "/api/state")[1]["state"]["pid"]
os.kill(info["pid"], 9); wait(lambda: not cs.pid_alive(info["pid"]), 5, "service killed")
chk(cs.existing_daemon(AG) is None and not (SD / "daemon.json").exists(), "a dead service's daemon.json is detected as stale and removed")
# the idle fake keeps running (it prints nothing more), so it stays alive: that is the orphan case
chk(cs.pid_alive(wpid), "the watcher outlived the killed service (idle, no output pipe use yet)")
r = cli("--background", "--port", "8952"); info = cs.read_info(AG); st = cs.call(info, "/api/state")[1]["state"]
chk(st["orphan"] and st["orphan"]["pid"] == wpid, "the new service reports the watcher left behind")
s, body = cs.call(info, "/api/start", "POST", {}); chk(s == 409 and "earlier session" in body.get("error", ""), "it refuses to start a second watcher")
s, _ = cs.call(info, "/api/service/kill-orphan", "POST", {}); chk(s == 200, "the orphan is stopped from the page")
wait(lambda: not cs.pid_alive(wpid), 8, "orphan gone"); st = cs.call(info, "/api/state")[1]["state"]; chk(st["orphan"] is None, "state no longer lists an orphan")

print("service: settings survive restarts, and the watcher can resume by itself")
cs.call(info, "/api/service/settings", "POST", {"autoStartWatcher": True})
r = cli("--quit"); wait(lambda: cs.read_info(AG) is None, 10, "quit")
chk(cs.load_settings(SD)["autoStartWatcher"] is True, "the auto-start setting was saved to disk")
r = cli("--background", "--port", "8953"); info = cs.read_info(AG)
wait(lambda: cs.call(info, "/api/state")[1]["state"]["status"] in ("starting", "running"), 10, "auto-start")
chk(cs.call(info, "/api/state")[1]["state"]["status"] in ("starting", "running"), "with auto-start on, the watcher starts by itself when the service starts")
r = cli("--status"); chk("Watcher:" in r.stdout and "Window address" in r.stdout, "--status shows the watcher state and the window address")
r = cli("--quit"); wait(lambda: cs.read_info(AG) is None, 10, "final quit")
r = cli("--quit"); chk("not running" in r.stdout, "--quit when nothing is running says so")
r = cli("--status"); chk(r.returncode == 1 and "not running" in r.stdout, "--status exits non-zero when the service is down")
print(f"\n{ok}/{ok} passed")
shutil.rmtree(TMP, ignore_errors=True)
