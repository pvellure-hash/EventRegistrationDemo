#!/usr/bin/env python
"""console_service.py - run the pipeline console as a BACKGROUND application.

  python console_service.py            open the app window (starts the background service first if needed)
  python console_service.py --background   start the service only (no window), e.g. at Windows sign-in
  python console_service.py --status       is it running? which window address?
  python console_service.py --quit         stop the service (and the watcher it started)

What this changes compared with `python run_console.py`
  run_console.py runs inside your terminal: close the terminal and the watcher dies with it.
  console_service.py splits the console in two:
    - a BACKGROUND SERVICE (no window, no terminal) that owns the watcher process and keeps running
      until you quit it. It keeps every step, status and log in memory, and a transcript on disk.
    - an APP WINDOW (Edge or Chrome in app mode, so it looks like a desktop application) that shows
      the service. Close it any time: the watcher keeps going. Open it again and everything is there.
  The page, the review gate buttons, the status tracking and the safety rules are the same as
  run_console.py: this module imports it and extends it, so nothing in run_console.py changes.

Also added
  - Settings saved in logs\\run-console\\settings.json:
      notify             when the review gate needs you, show a Windows notification and, if no window
                         is open, open one (default on)
      autoStartWatcher   start the watcher by itself whenever the service starts (default off)
  - Notifications for: ready to merge, approve the deploy, ticket live, ticket stopped.
  - One service at a time (a start lock + daemon.json), and a guard against a second watcher: if a watcher
    from an earlier service is still running, the page says so and offers to stop it.
  - A bounded event history, so a service left running for days does not grow without limit.
  - Start at Windows sign-in: python install_console_app.py --startup

Limits you should know
  - The service runs in YOUR Windows session (not as a Windows Service): it can then open windows, use
    your Copilot sign-in and your Salesforce login. Signing out or restarting stops it; with the sign-in
    shortcut it comes back, and with autoStartWatcher the watcher comes back too. A ticket that was in
    progress at that moment is left ai-locked in Jira (the page says so).
  - The watcher's review gate still needs a person. While no one answers, it simply waits.
  - If the service itself is killed, the watcher loses its output pipe and stops at its next message.

Standard library only. Files written: logs\\run-console\\{daemon.json, daemon.log, settings.json, watcher.pid}.
"""
import argparse
import base64
import http.client
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import run_console as rc  # noqa: E402

DETACHED_PROCESS, NEW_PROCESS_GROUP, BREAKAWAY, NO_WINDOW = 0x00000008, 0x00000200, 0x01000000, 0x08000000
DEFAULT_SETTINGS = {"notify": True, "autoStartWatcher": False}
START_LOCK_SECONDS = 30


# --------------------------------------------------------------------------- small helpers
def state_dir(agent_dir):
    return Path(agent_dir).resolve().parent / "logs" / "run-console"


def pid_alive(pid, image_hint=None):
    """Portable liveness check. Never uses os.kill(pid, 0) on Windows (that would kill the process there)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                                 timeout=15, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        for line in out.splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[1] == str(pid):
                return image_hint is None or image_hint.lower() in parts[0].lower()
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    try:    # a finished child that has not been reaped yet still answers kill(0)
        stat = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        if stat == "Z":
            return False
    except OSError:
        pass
    if image_hint:
        try:
            return image_hint.lower() in Path(f"/proc/{pid}/comm").read_text().lower()
        except OSError:
            return True
    return True


def load_settings(sd):
    s = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads((Path(sd) / "settings.json").read_text(encoding="utf-8"))
        for k in DEFAULT_SETTINGS:
            if isinstance(data.get(k), bool):
                s[k] = data[k]
    except (OSError, ValueError):
        pass
    return s


def save_settings(sd, settings):
    Path(sd).mkdir(parents=True, exist_ok=True)
    (Path(sd) / "settings.json").write_text(json.dumps({k: bool(settings.get(k)) for k in DEFAULT_SETTINGS}, indent=2), encoding="utf-8")


def console_python():
    """The console-subsystem interpreter. pythonw.exe has no usable stdout, so the watcher must not run under it."""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "pythonw.exe":
        c = exe.with_name("python.exe")
        if c.exists():
            return str(c)
    return str(exe)


def pythonw_python():
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def toast_command(title, message):
    """PowerShell -EncodedCommand text that shows a Windows toast notification (best effort)."""
    def x(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    xml = f'<toast><visual><binding template="ToastGeneric"><text>{x(title)}</text><text>{x(message)}</text></binding></visual></toast>'
    script = ("$ErrorActionPreference='Stop';"
              "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
              "[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
              "$d=New-Object Windows.Data.Xml.Dom.XmlDocument;"
              "$d.LoadXml('" + xml.replace("'", "''") + "');"
              "$t=[Windows.UI.Notifications.ToastNotification]::new($d);"
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
              "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)")
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def find_browser():
    cands = []
    if os.name == "nt":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
            base = os.environ.get(env)
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        if c.exists():
            return str(c)
    return None


def open_window(url):
    """Open the console in an app-style window (no tabs or address bar), or the default browser."""
    exe = find_browser()
    try:
        if exe:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1440,980"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except OSError:
        pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def default_notifier(hub, title, message, window):
    if os.name == "nt":
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", toast_command(title, message)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError:
            pass
    if window and hub.window_url:
        open_window(hub.window_url)


# --------------------------------------------------------------------------- the hub
class ServiceHub(rc.Hub):
    MAX_EVENTS = 8000
    KEEP_LINES = 1500

    def __init__(self, agent_dir, token, python=None, background=False):
        super().__init__(agent_dir, token, python=python)
        self.background = background
        self.state_dir = self.logs / "run-console"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings(self.state_dir)
        self.clients = 0
        self.window_url = None
        self.notifier = default_notifier
        self.notify_log = []
        self.service_started = rc.now_ms()
        self.orphan = None
        self._notified = set()
        self._check_orphan()

    # ---- orphan watcher (a watcher left behind by an earlier service) ----
    @property
    def pid_file(self):
        return self.state_dir / "watcher.pid"

    def _check_orphan(self):
        try:
            info = json.loads(self.pid_file.read_text(encoding="utf-8"))
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            self.orphan = None
            return
        if pid != os.getpid() and pid_alive(pid, "python"):
            self.orphan = {"pid": pid, "started": info.get("started")}
        else:
            self.orphan = None
            self._clear_pid()

    def _write_pid(self):
        p = self.proc
        if p is not None:
            try:
                self.pid_file.write_text(json.dumps({"pid": p.pid, "started": rc.now_ms()}), encoding="utf-8")
            except OSError:
                pass

    def _clear_pid(self):
        try:
            self.pid_file.unlink()
        except OSError:
            pass

    def kill_orphan(self):
        with self.lock:
            o = self.orphan
        if not o:
            raise RuntimeError("There is no earlier watcher to stop.")
        rc.kill_tree(o["pid"])
        deadline = time.time() + 12
        while time.time() < deadline and pid_alive(o["pid"], "python"):
            time.sleep(0.2)
        if pid_alive(o["pid"], "python"):
            raise RuntimeError(f"The earlier watcher (PID {o['pid']}) did not stop. End it in Task Manager.")
        with self.lock:
            self.orphan = None
        self._clear_pid()
        self._push_state()

    # ---- overrides ----
    def _spawn(self, script, stdin):
        path = self.agent_dir / script
        if not path.exists():
            raise FileNotFoundError(f"{script} not found in {self.agent_dir}")
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "WATCH_PROGRESS": "false", "RUN_CONSOLE": "1"})
        kw = {}
        if os.name == "nt":
            if self.background:            # the service has no console: give children a hidden one instead of a visible window
                kw["creationflags"] = NO_WINDOW
        else:
            kw["start_new_session"] = True
        return subprocess.Popen([self.python, "-u", script], cwd=str(self.agent_dir), env=env, stdin=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)

    def start_watcher(self):
        with self.lock:
            o = self.orphan
        if o:
            raise RuntimeError(f"A watcher from an earlier session (PID {o['pid']}) is still running. Stop it first.")
        super().start_watcher()
        self._write_pid()

    def stop(self):
        with self.lock:
            has_proc, o = self.proc is not None, self.orphan
        if not has_proc and o:
            return self.kill_orphan()
        super().stop()

    def _on_exit(self, who, popen, code):
        super()._on_exit(who, popen, code)
        if who == "watcher":
            self._clear_pid()

    def _on_prompt(self, popen, text):
        super()._on_prompt(popen, text)
        key = self._active_key()
        self.notify("Approval needed", f"{key or 'A ticket'}: review the change, then approve or reject.", window=True)

    def snapshot(self):
        s = super().snapshot()
        s["daemon"] = {"mode": "background" if self.background else "foreground", "pid": os.getpid(), "started": self.service_started}
        s["settings"] = dict(self.settings)
        s["orphan"] = self.orphan
        s["clients"] = self.clients
        return s

    def emit(self, evs):
        super().emit(evs)
        try:
            self._after_emit(evs)
        except Exception:        # notifications and housekeeping must never break the stream
            pass

    # ---- clients, settings, notifications ----
    def client_connected(self):
        with self.lock:
            self.clients += 1
        self._push_state()

    def client_disconnected(self):
        with self.lock:
            self.clients = max(0, self.clients - 1)
        self._push_state()

    def update_settings(self, changes):
        with self.lock:
            for k, v in (changes or {}).items():
                if k in DEFAULT_SETTINGS and isinstance(v, bool):
                    self.settings[k] = v
            save_settings(self.state_dir, self.settings)
        self._push_state()

    def notify(self, title, message, window=False):
        """Tell the person something happened. window=True also opens the app window if none is open."""
        if not self.settings.get("notify", True):
            return
        open_it = bool(window) and self.clients == 0
        self.notify_log.append({"t": rc.now_ms(), "title": title, "message": message, "window": open_it})
        del self.notify_log[:-50]

        def run():
            try:
                self.notifier(self, title, message, open_it)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _key_of(self, jid):
        try:
            return self.parser.jobs[jid]["key"]
        except (AttributeError, KeyError):
            return jid

    def _after_emit(self, evs):
        for e in evs:
            t = e.get("type")
            if t == "step" and e["step"]["status"] == "waiting" and e["step"]["idx"] in (10, 11):
                tag = (e["job"], e["step"]["idx"], "waiting")
                if tag in self._notified:
                    continue
                self._notified.add(tag)
                key = self._key_of(e["job"])
                if e["step"]["idx"] == 10:
                    self.notify("Ready to merge", f"{key}: review and merge the pull request.")
                else:
                    self.notify("Approve the deploy", f"{key}: approve it in GitHub Actions (Review deployments).")
            elif t == "job" and e["job"].get("kind") == "ticket" and e["job"].get("status") in ("ok", "failed"):
                tag = (e["job"]["id"], e["job"]["status"])
                if tag in self._notified:
                    continue
                self._notified.add(tag)
                if e["job"]["status"] == "ok":
                    self.notify("Live in Salesforce", f"{e['job']['key']} is deployed. Jira moves to Done.")
                else:
                    self.notify("Ticket stopped", f"{e['job']['key']} stopped: {e['job'].get('note') or 'open the console for the reason'}.")
        if len(self.events) > self.MAX_EVENTS:
            self.compact()

    def compact(self):
        """Keep the latest state of every job and step plus the most recent lines; drop the rest."""
        with self.lock:
            ev = self.events
            if len(ev) <= self.MAX_EVENTS:
                return
            lastjob, laststep, lines, last_state, last_prompt = {}, {}, {}, None, None
            for e in ev:
                t = e["type"]
                if t == "job":
                    lastjob[e["job"]["id"]] = e
                elif t == "step":
                    laststep[(e["job"], e["step"]["idx"])] = e
                elif t == "line":
                    lines.setdefault((e["job"], e["step"]), []).append(e)
                elif t == "state":
                    last_state = e
                elif t in ("prompt", "prompt_clear"):
                    last_prompt = e
            keep = [ev[0]] if ev and ev[0]["type"] == "reset" else []
            keep += list(lastjob.values()) + list(laststep.values())
            for L in lines.values():
                keep += L[-self.KEEP_LINES:]
            if last_state:
                keep.append(last_state)
            if last_prompt and last_prompt["type"] == "prompt" and self.prompt:
                keep.append(last_prompt)
            keep.sort(key=lambda e: e["seq"])
            self.events = keep


# --------------------------------------------------------------------------- the page additions
SERVICE_CSS = """
.svcpop{position:fixed;top:60px;right:20px;z-index:70;width:380px;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:16px 18px}
.svcpop h4{margin:0 0 4px;font-size:14px}.svcpop .s{color:var(--muted);font-size:12px;margin-bottom:12px;line-height:1.5}
.svcpop .tg{display:flex;align-items:flex-start;gap:9px;margin:9px 0;font-size:12.5px;color:var(--ink);line-height:1.4}.svcpop .tg input{margin-top:2px}
.svcpop .h{color:var(--muted);font-size:11.5px;margin:12px 0;line-height:1.5}.svcpop code{background:var(--surface2);border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:11px}
.svcpop .row{display:flex;justify-content:space-between;align-items:center;gap:10px;border-top:1px solid var(--line);padding-top:12px;margin-top:6px}
.svcgone{position:fixed;inset:0;background:rgba(8,13,25,.78);z-index:120;display:grid;place-items:center}
.svcgone .box{background:var(--surface);border-radius:16px;padding:26px 30px;max-width:460px;text-align:center;border:1px solid var(--line);box-shadow:var(--shadow)}
.svcgone h3{margin:0 0 8px;font-size:18px}.svcgone p{margin:0;color:var(--muted);line-height:1.55}
"""

SERVICE_JS = r"""
(function(){
try{
  var bar=document.querySelector('.topbar'),chip0=document.getElementById('chip'),theme=document.getElementById('bTheme');
  if(!bar||!chip0||typeof S==='undefined'||typeof api==='undefined'||typeof renderChrome!=='function'||typeof renderBanners!=='function')return;
  var SV={gone:false,lost:0,pop:false};
  var chip=document.createElement('span');chip.className='chip';chip.id='svcChip';chip.innerHTML='<i></i><span id="svcT">Service</span>';bar.insertBefore(chip,chip0);
  var gear=document.createElement('button');gear.className='btn';gear.id='bSvc';gear.title='Background service';gear.textContent='\u2699 Service';bar.insertBefore(gear,theme);
  var pop=document.createElement('div');pop.className='svcpop';pop.id='svcPop';pop.hidden=true;document.body.appendChild(pop);
  function ago(ms){if(!ms)return '';var s=Math.round((Date.now()-ms)/1000);if(s<90)return 'just now';var m=Math.round(s/60);if(m<90)return m+' min ago';var h=Math.round(m/60);if(h<48)return h+' h ago';return Math.round(h/24)+' days ago'}
  function popHtml(){
    var d=S.daemon||{},st=S.settings||{};
    return '<h4>Background service</h4><div class="s">'+(d.mode==='background'
      ?'Running without a window. Closing this window does not stop the watcher.'
      :'Running inside a terminal. Closing that terminal stops the watcher. Start the app with <code>python console_service.py</code> to run in the background.')
      +'<br>PID '+(d.pid||'?')+' \u00b7 started '+ago(d.started)+' \u00b7 windows open: '+(S.clients||0)+'</div>'
      +'<label class="tg"><input type="checkbox" data-svcset="notify" '+(st.notify?'checked':'')+'><span>Alert me when I am needed: show a notification, and open this window if none is open.</span></label>'
      +'<label class="tg"><input type="checkbox" data-svcset="autoStartWatcher" '+(st.autoStartWatcher?'checked':'')+'><span>Start the watcher automatically whenever the service starts.</span></label>'
      +'<div class="h">To start the service when you sign in to Windows, run <code>python install_console_app.py --startup</code> once.</div>'
      +'<div class="row"><span class="s" style="margin:0">Stops the service and the watcher.</span><button class="no-btn" data-svc="quit">Quit service</button></div>'}
  function renderPop(){pop.hidden=!SV.pop;if(SV.pop&&!pop.contains(document.activeElement))pop.innerHTML=popHtml()}
  function svcChrome(){
    var d=S.daemon,c=document.getElementById('svcChip'),t=document.getElementById('svcT');if(!d||!c)return;
    var bg=d.mode==='background';c.className='chip '+(bg?'ok':'att');t.textContent=bg?'Background service':'Running in this terminal';
    c.title=bg?'The watcher keeps running when you close this window. PID '+d.pid:'Closing the terminal stops the watcher.';
    var g=document.getElementById('gosub');if(g&&bg&&!SV.gone&&S.status!=='stopped')g.innerHTML+='<br><b>You can close this window.</b> The watcher keeps running in the background.';
    var b=document.getElementById('bStart');if(b&&S.orphan)b.disabled=true;
    renderPop();
  }
  var rc0=renderChrome;renderChrome=function(){rc0();try{svcChrome()}catch(e){console.error(e)}};
  var rb0=renderBanners;renderBanners=function(){rb0();try{
    var h='';
    if(S.orphan)h+='<div class="banner bad"><div class="bi">!</div><div class="bt"><b>A watcher from an earlier session is still running (PID '+S.orphan.pid+')</b><span>This window cannot control it, because the service that started it is gone. Stop it, then start a new one.</span></div><button class="no-btn" data-svc="orphan">Stop it</button></div>';
    if(!connected&&SV.lost&&Date.now()-SV.lost>12000&&!SV.gone)h+='<div class="banner bad"><div class="bi">!</div><div class="bt"><b>The background service is not responding</b><span>If you quit it, reopen the app from your desktop shortcut or run python console_service.py.</span></div></div>';
    if(h)document.getElementById('banners').insertAdjacentHTML('beforeend',h)}catch(e){console.error(e)}};
  setInterval(function(){if(connected)SV.lost=0;else if(!SV.lost)SV.lost=Date.now();schedule()},3000);
  document.addEventListener('click',async function(e){
    var el=e.target.closest('#bSvc,[data-svc]');
    if(el&&el.id==='bSvc'){SV.pop=!SV.pop;pop.innerHTML=popHtml();renderPop();return}
    if(!el&&SV.pop&&!e.target.closest('#svcPop')){SV.pop=false;renderPop();return}
    if(!el)return;
    var a=el.dataset.svc;
    try{
      if(a==='orphan'){await api('/api/service/kill-orphan',{method:'POST',body:'{}'});toast('The earlier watcher was stopped.')}
      if(a==='quit'){
        var busy=S.active?(S.active+' is being processed. Quitting leaves it unfinished: its Jira label stays ai-locked until you set it back to ai-ready.'):'The watcher and the background service will stop. Nothing is lost; start the app again at any time.';
        var ok=await confirmBox('Quit the background service?',busy,'Quit service');if(!ok)return;
        await api('/api/service/shutdown',{method:'POST',body:'{}'});SV.gone=true;SV.pop=false;renderPop();
        var o=document.createElement('div');o.className='svcgone';o.innerHTML='<div class="box"><h3>The background service has stopped</h3><p>The watcher is no longer running. Open AI Delivery Console from your desktop shortcut, or run <code>python console_service.py</code>, to start it again.</p></div>';document.body.appendChild(o)}
    }catch(er){toast(er.message)}
  });
  document.addEventListener('change',async function(e){
    var k=e.target.dataset&&e.target.dataset.svcset;if(!k)return;
    var body={};body[k]=e.target.checked;
    try{await api('/api/service/settings',{method:'POST',body:JSON.stringify(body)})}catch(er){toast(er.message)}
  });
  schedule();
}catch(e){console.error('service panel not loaded',e)}
})();
"""


# --------------------------------------------------------------------------- http
class ServiceHandler(rc.Handler):
    server_version = "AIConsole"

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            if not self._guard(need_token=False):
                return
            f = self.hub.agent_dir / "run_console.html"
            if not f.exists():
                return self._send(404, {"error": "run_console.html must be in the same folder as console_service.py."})
            html = f.read_text(encoding="utf-8")
            inject = f"<style>{SERVICE_CSS}</style><script>{SERVICE_JS}</script>"
            i = html.lower().rfind("</body>")
            html = html[:i] + inject + html[i:] if i >= 0 else html + inject
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'self'"})
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/service/"):
            return super().do_POST()
        if not self._guard(post=True):
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        try:
            if u.path == "/api/service/settings":
                self.hub.update_settings(body)
            elif u.path == "/api/service/kill-orphan":
                self.hub.kill_orphan()
            elif u.path == "/api/service/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.request_shutdown, daemon=True).start()
                return
            else:
                return self._send(404, {"error": "Not found"})
            return self._send(200, {"ok": True})
        except (RuntimeError, ValueError) as e:
            return self._send(409, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    @staticmethod
    def _peer_closed(sock):
        """True when the window has gone away (the socket is readable and reads end-of-stream)."""
        try:
            r, _, _ = select.select([sock], [], [], 0)
            if not r:
                return False
            return sock.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _stream(self, after):
        """Same stream as run_console, but a closed window is noticed within ~2 s (the window count drives the alerts)."""
        self.hub.client_connected()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = after
            while not self.server.stopping:
                evs, last = self.hub.wait_events(last, timeout=2.0)
                if self._peer_closed(self.connection):
                    break
                if evs:
                    for ev in evs:
                        self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": hb\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.client_disconnected()


class ServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, hub):
        self.hub = hub
        self.stopping = False
        self.allowed_hosts = {f"127.0.0.1:{addr[1]}", f"localhost:{addr[1]}"}
        ThreadingHTTPServer.__init__(self, addr, ServiceHandler)

    def request_shutdown(self):
        self.stopping = True
        self.shutdown()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError, TimeoutError)):
            return          # a window closed mid-reply: normal, not an error
        super().handle_error(request, client_address)


def make_server(agent_dir, port, token, background):
    hub = ServiceHub(agent_dir, token, python=console_python(), background=background)
    for p in range(port, port + 30):
        try:
            return ServiceServer(("127.0.0.1", p), hub), hub
        except OSError:
            continue
    raise SystemExit(f"No free port between {port} and {port + 29}.")


# --------------------------------------------------------------------------- daemon lifecycle
def info_path(agent_dir):
    return state_dir(agent_dir) / "daemon.json"


def read_info(agent_dir):
    try:
        d = json.loads(info_path(agent_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and "pid" in d and "port" in d and "token" in d else None
    except (OSError, ValueError):
        return None


def call(info, path, method="GET", body=None, timeout=4):
    c = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=timeout)
    try:
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Host": f"127.0.0.1:{info['port']}", "X-Console-Token": info["token"], "Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data or b"{}")
        except ValueError:
            return r.status, {}
    finally:
        c.close()


def responsive(info):
    try:
        return call(info, "/api/state")[0] == 200
    except (OSError, http.client.HTTPException):
        return False


def existing_daemon(agent_dir):
    """The running service's info, or None. A leftover daemon.json from a dead service is removed."""
    info = read_info(agent_dir)
    if not info:
        return None
    if pid_alive(info.get("pid"), "python"):
        return info
    try:
        info_path(agent_dir).unlink()
    except OSError:
        pass
    return None


def window_url(info):
    return f"http://127.0.0.1:{info['port']}/#t={info['token']}"


def spawn_daemon(agent_dir, port=None):
    cmd = [pythonw_python(), str(Path(__file__).resolve()), "--daemon", "--agent-dir", str(agent_dir)]
    if port:
        cmd += ["--port", str(port)]
    kw = dict(cwd=str(agent_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        base = DETACHED_PROCESS | NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(cmd, creationflags=base | BREAKAWAY, **kw)
        except OSError:      # the parent job does not allow breakaway
            return subprocess.Popen(cmd, creationflags=base, **kw)
    return subprocess.Popen(cmd, start_new_session=True, **kw)


def ensure_daemon(agent_dir, port=None, wait=25):
    """Return (info, started_now). Starts the background service if it is not running."""
    info = existing_daemon(agent_dir)
    if info:
        if not responsive(info):
            time.sleep(1.5)
            if not responsive(info):
                raise RuntimeError(f"The background service (PID {info['pid']}) is running but not answering. "
                                   f"Run: python console_service.py --quit   (or end PID {info['pid']} in Task Manager).")
        return info, False
    spawn_daemon(agent_dir, port)
    deadline = time.time() + wait
    while time.time() < deadline:
        info = existing_daemon(agent_dir)
        if info and responsive(info):
            return info, True
        time.sleep(0.25)
    raise RuntimeError(f"The background service did not start. See {state_dir(agent_dir) / 'daemon.log'}")


def acquire_start_lock(sd):
    lock = Path(sd) / "daemon.starting"
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 999
            if age < START_LOCK_SECONDS:
                return None
            try:
                lock.unlink()
            except OSError:
                pass
    return None


def run_service(agent_dir, port, detached):
    """The service itself. detached=True when started by the launcher: stdio goes to daemon.log."""
    agent_dir = Path(agent_dir).resolve()
    sd = state_dir(agent_dir)
    sd.mkdir(parents=True, exist_ok=True)
    lock = acquire_start_lock(sd)
    if lock is None:
        return 0                                  # another service is starting right now
    try:
        if existing_daemon(agent_dir):
            return 0                              # one service at a time
        if detached:
            log = sd / "daemon.log"
            try:
                if log.exists() and log.stat().st_size > 2_000_000:
                    log.replace(sd / "daemon.old.log")
            except OSError:
                pass
            sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
            print(f"--- service starting {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}")
        rc.load_env(agent_dir.parent / ".env")
        token = rc.secrets.token_urlsafe(16)
        srv, hub = make_server(agent_dir, port, token, background=detached)
        info = {"pid": os.getpid(), "port": srv.server_address[1], "token": token, "started": rc.now_ms(), "agent_dir": str(agent_dir), "mode": "background" if detached else "foreground"}
        hub.window_url = window_url(info)
        info_path(agent_dir).write_text(json.dumps(info), encoding="utf-8")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass

    def on_signal(signum, frame):
        threading.Thread(target=srv.request_shutdown, daemon=True).start()
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass
    if hub.settings.get("autoStartWatcher") and not hub.orphan:
        try:
            hub.start_watcher()
            print("watcher auto-started (setting autoStartWatcher)")
        except Exception as e:
            print(f"auto-start failed: {e}")
    print(f"service ready on 127.0.0.1:{info['port']}")
    try:
        srv.serve_forever(poll_interval=0.3)
    finally:
        srv.stopping = True
        try:
            if hub.proc is not None:
                rc.kill_tree(hub.proc.pid)
        finally:
            hub._clear_pid()
            try:
                cur = read_info(agent_dir)
                if cur and cur.get("pid") == os.getpid():
                    info_path(agent_dir).unlink()
            except OSError:
                pass
            srv.server_close()
            print("service stopped")
    return 0


# --------------------------------------------------------------------------- command line
def say(msg, error=False):
    """Print, and on Windows without a console (pythonw) show a message box so errors are not invisible."""
    if sys.stdout is not None:
        print(msg)
        return
    if error and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(msg), "AI Delivery Console", 0x10)
        except Exception:
            pass


def cmd_quit(agent_dir):
    info = existing_daemon(agent_dir)
    if not info:
        say("The background service is not running.")
        return 0
    try:
        call(info, "/api/service/shutdown", "POST", {})
    except (OSError, http.client.HTTPException):
        rc.kill_tree(info["pid"])
    deadline = time.time() + 20
    while time.time() < deadline and pid_alive(info["pid"], "python"):
        time.sleep(0.25)
    if pid_alive(info["pid"], "python"):
        rc.kill_tree(info["pid"])
    say("The background service has stopped (the watcher it started was stopped too).")
    return 0


def cmd_status(agent_dir):
    info = existing_daemon(agent_dir)
    if not info:
        say("The background service is not running.")
        return 1
    state = {}
    try:
        state = call(info, "/api/state")[1].get("state", {})
    except (OSError, http.client.HTTPException):
        pass
    say(f"Service PID {info['pid']} on port {info['port']} ({info.get('mode', '?')}). Watcher: {state.get('status', 'unknown')}"
        + (f", processing {state['active']}" if state.get("active") else "") + (", waiting for your approval" if state.get("prompt") else "")
        + f". Windows open: {state.get('clients', '?')}.")
    say(f"Window address: {window_url(info)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the pipeline console as a background application")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--app", action="store_true", help="open the app window (default)")
    g.add_argument("--background", action="store_true", help="start the service without opening a window")
    g.add_argument("--daemon", action="store_true", help="(internal) run the service in this process, detached")
    g.add_argument("--foreground", action="store_true", help="run the service in this terminal (for debugging)")
    g.add_argument("--quit", action="store_true", help="stop the service and the watcher")
    g.add_argument("--status", action="store_true", help="show whether the service is running")
    ap.add_argument("--no-open", action="store_true", help="do not open a window")
    ap.add_argument("--port", type=int, default=rc.DEFAULT_PORT)
    ap.add_argument("--agent-dir", default=str(HERE))
    a = ap.parse_args(argv)
    agent_dir = Path(a.agent_dir).resolve()
    try:
        if a.quit:
            return cmd_quit(agent_dir)
        if a.status:
            return cmd_status(agent_dir)
        if a.daemon:
            return run_service(agent_dir, a.port, detached=True)
        if a.foreground:
            return run_service(agent_dir, a.port, detached=False)
        info, started = ensure_daemon(agent_dir, a.port)
        say(("Started the background service" if started else "The background service is already running") + f" (PID {info['pid']}).")
        if not a.background and not a.no_open:
            open_window(window_url(info))
        return 0
    except (RuntimeError, SystemExit) as e:
        say(str(e), error=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
