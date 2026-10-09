"""Checks that the v12 wiring really works in YOUR patched run_console.py, console_service.py and preflight.py.
It starts the real servers on a free port, in a temporary folder, with no watcher and no network, and talks
to them over HTTP. Nothing is started, changed or sent to Jira, GitHub or Salesforce.

Run from agent\\:   python test_wiring_v12.py
"""
import http.client
import inspect
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import console_auth as ca  # noqa: E402
import secret_store as ss  # noqa: E402
import run_console as rc  # noqa: E402
import console_service as cs  # noqa: E402

TOKEN = "test-console-token"
OLD, NEW = "jira-token-old-123456", "jira-token-new-654321"
PAGE = "<html><body><header class='topbar'><button id='bTheme'>t</button></header><main></main></body></html>"


class FakeBackend:
    label = "Fake store"

    def __init__(self, data=None, fail=False):
        self.data, self.fail = dict(data or {}), fail

    def get(self, name):
        if self.fail:
            raise ss.SecretStoreError("store locked")
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value


class FakeProc:
    pid = 4242
    prompt_open = True

    def __init__(self):
        self.stdin = io.BytesIO()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(self, port):
        self.port, self.cookies = port, {}

    def req(self, method, path, body=None, token=TOKEN, accept="application/json", headers=None):
        h = {"Host": f"127.0.0.1:{self.port}", "Accept": accept}
        if token is not None:
            h["X-Console-Token"] = token
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        for sc in r.msg.get_all("Set-Cookie") or []:
            k, v = sc.split(";")[0].split("=", 1)
            if v:
                self.cookies[k] = v
            else:
                self.cookies.pop(k, None)
        c.close()
        try:
            js = json.loads(raw)
        except ValueError:
            js = {}
        return r.status, js, raw.decode("utf-8", "replace"), r

    def sign_in(self):
        st, js, _, _ = self.req("POST", "/auth/login")
        assert st == 202, (st, js)
        for _ in range(100):
            st, js, _, _ = self.req("GET", "/auth/poll")
            if js.get("state") != "running":
                return st, js
            time.sleep(0.05)
        return 0, {}


class Mixin:
    kind = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.agent = root / "agent"
        self.agent.mkdir()
        (self.agent / "run_console.html").write_text(PAGE, encoding="utf-8")
        self.access = root / "access.json"
        self.access.write_text(json.dumps({"users": [
            {"windows": "T\\viewer", "role": "viewer"}, {"windows": "T\\operator", "role": "operator"},
            {"windows": "T\\approver", "role": "approver"}]}))
        self.saved = {k: os.environ.get(k) for k in (
            "CONSOLE_AUTH_MODE", "CONSOLE_AUTH_ALLOW_LOCAL", "CONSOLE_ACCESS_FILE", "JIRA_API_TOKEN", "GITHUB_TOKEN")}
        os.environ.update({"CONSOLE_AUTH_MODE": "local", "CONSOLE_AUTH_ALLOW_LOCAL": "true",
                           "CONSOLE_ACCESS_FILE": str(self.access), "JIRA_API_TOKEN": OLD, "GITHUB_TOKEN": "gh-token-old-123456"})
        self.store = FakeBackend({"JIRA_API_TOKEN": OLD, "GITHUB_TOKEN": "gh-token-old-123456"})
        ss._STORE = ss.SecretStore([self.store], ttl=300)
        self.ident = "T\\approver"
        self._real_identity = ca.local_identity
        ca.local_identity = lambda: self.ident
        port = free_port()
        self.srv, self.hub = self.make(port)
        self.port = self.srv.server_address[1]
        self.srv.auth = ca.install(self.hub, self.port)           # exactly what the wiring does
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.audit = self.hub.logs / "run-console" / "auth-audit.jsonl"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        ca.local_identity = self._real_identity
        ss._STORE = None
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def as_user(self, ident):
        self.ident = ident
        c = Client(self.port)
        st, js = c.sign_in()
        self.assertEqual(st, 200, js)
        return c

    def events(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()] if self.audit.exists() else []

    # ---------------------------------------------------------------- not signed in
    def test_page_redirects_to_signin(self):
        st, _, _, r = Client(self.port).req("GET", "/", token=None, accept="text/html,*/*")
        self.assertEqual(st, 302)
        self.assertTrue(r.getheader("Location").startswith("/auth/signin?next="))

    def test_api_needs_a_session_even_with_the_console_token(self):
        c = Client(self.port)
        for m, p in (("GET", "/api/state"), ("GET", "/api/queue"), ("GET", "/api/transcript"),
                     ("POST", "/api/start"), ("POST", "/api/answer"), ("POST", "/api/util")):
            self.assertEqual(c.req(m, p)[0], 401, (m, p))

    def test_signin_page_public_but_login_needs_console_token(self):
        c = Client(self.port)
        st, _, body, _ = c.req("GET", "/auth/signin", token=None, accept="text/html")
        self.assertEqual(st, 200)
        self.assertIn("Sign in", body)
        self.assertEqual(c.req("POST", "/auth/login", token=None)[0], 401)
        self.assertEqual(c.req("POST", "/auth/login", token="wrong")[0], 401)

    def test_host_check_still_applies(self):
        st, _, _, _ = Client(self.port).req("GET", "/auth/signin", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 403)

    # ---------------------------------------------------------------- signed in
    def test_signed_in_page_has_the_sign_in_panel(self):
        c = self.as_user("T\\viewer")
        st, _, body, _ = c.req("GET", "/", token=None, accept="text/html")
        self.assertEqual(st, 200)
        self.assertIn("auChip", body)
        self.assertEqual(c.req("GET", "/api/state")[0], 200)
        st, js, _, _ = c.req("GET", "/auth/me")
        self.assertEqual((st, js["role"]), (200, "viewer"))

    def test_viewer_cannot_start_stop_util_or_answer(self):
        c = self.as_user("T\\viewer")
        for m, p, b in (("POST", "/api/start", {}), ("POST", "/api/stop", {}), ("POST", "/api/util", {"name": "preflight"}),
                        ("POST", "/api/answer", {"answer": "y"})):
            st, js, _, _ = c.req(m, p, b)
            self.assertEqual((p, st, js.get("error")), (p, 403, "forbidden"))
        self.assertIsNone(self.hub.proc)

    def test_operator_can_operate_but_not_approve(self):
        c = self.as_user("T\\operator")
        st, js, _, _ = c.req("POST", "/api/stop", {})
        self.assertEqual((st, "not running" in js["error"]), (409, True))      # allowed, nothing to stop
        st, js, _, _ = c.req("POST", "/api/answer", {"answer": "y"})
        self.assertEqual((st, js["need"]), (403, "approver"))

    def test_approver_decision_reaches_the_watcher_and_is_audited(self):
        for ans, word in (("y", "approved"), ("n", "rejected")):
            proc = FakeProc()
            self.hub.parser, self.hub.proc = rc.Parser(), proc
            self.hub.prompt = {"text": "Approve? [y/N]:", "job": None, "key": "CLAUDE-9"}
            c = self.as_user("T\\approver")
            st, js, _, _ = c.req("POST", "/api/answer", {"answer": ans})
            self.assertEqual(st, 200, js)
            self.assertEqual(proc.stdin.getvalue(), (ans + "\n").encode())
            ev = [e for e in self.events() if e["event"] == "review_decision"][-1]
            self.assertEqual((ev["user"], ev["role"], ev["ticket"], ev["decision"]), ("T\\approver", "approver", "CLAUDE-9", word))

    def test_failed_answer_is_not_recorded_as_a_decision(self):
        c = self.as_user("T\\approver")
        st, js, _, _ = c.req("POST", "/api/answer", {"answer": "y"})
        self.assertEqual(st, 409)
        self.assertFalse([e for e in self.events() if e["event"] == "review_decision"])

    # ---------------------------------------------------------------- secrets at watcher start
    def test_start_rereads_a_rotated_token(self):
        c = self.as_user("T\\operator")
        self.store.data["JIRA_API_TOKEN"] = NEW
        st, js, _, _ = c.req("POST", "/api/start", {})
        self.assertEqual(st, 409)                                          # no watch_queue.py in the temp folder
        self.assertIn("watch_queue.py", js["error"])
        self.assertEqual(os.environ["JIRA_API_TOKEN"], NEW)
        self.assertIn(NEW, self.hub.secrets)                               # and it is redacted from logs
        self.assertNotIn(OLD, self.hub.secrets)

    def test_start_stops_with_a_clear_message_if_the_store_is_locked(self):
        c = self.as_user("T\\operator")
        self.store.fail = True
        st, js, _, _ = c.req("POST", "/api/start", {})
        self.assertEqual(st, 409)
        self.assertIn("Secrets could not be read", js["error"])

    # ---------------------------------------------------------------- audit hygiene
    def test_audit_has_who_and_never_a_credential(self):
        c = self.as_user("T\\operator")
        c.req("POST", "/api/stop", {})
        c.req("POST", "/api/answer", {"answer": "y"})
        text = self.audit.read_text()
        for secret in list(c.cookies.values()) + [TOKEN, self.srv.auth.control_token, OLD]:
            self.assertNotIn(secret, text)
        kinds = [e["event"] for e in self.events()]
        for k in ("sign_in", "request", "denied"):
            self.assertIn(k, kinds)

    def test_rejected_post_does_not_break_the_next_request_on_the_same_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        hdr = {"Host": f"127.0.0.1:{self.port}", "X-Console-Token": TOKEN, "Content-Type": "application/json"}
        body = json.dumps({"answer": "y"})
        for _ in range(3):
            c.request("POST", "/api/answer", body=body, headers=hdr)       # no session: rejected with 401
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 401)
            c.request("GET", "/auth/signin", headers={"Host": hdr["Host"], "Accept": "text/html"})
            r = c.getresponse()                                             # must still be a clean 200
            r.read()
            self.assertEqual(r.status, 200)
        c.close()

    def test_cookie_name_includes_the_port(self):
        c = self.as_user("T\\viewer")
        self.assertEqual(list(c.cookies), [f"aidc_session_{self.port}"])

    def test_sign_out_ends_the_session(self):
        c = self.as_user("T\\viewer")
        self.assertEqual(c.req("POST", "/auth/logout")[0], 200)
        self.assertEqual(c.req("GET", "/api/state")[0], 401)

    def test_unlisted_windows_account_is_refused(self):
        self.ident = "T\\stranger"
        c = Client(self.port)
        st, js = c.sign_in()
        self.assertEqual(st, 403)
        self.assertIn("not on the access list", js["reason"])
        self.assertEqual(c.req("GET", "/api/state")[0], 401)


class RunConsoleServer(Mixin, unittest.TestCase):
    def make(self, port):
        return rc.make_server(self.agent, port, TOKEN)


class ServiceServer(Mixin, unittest.TestCase):
    def make(self, port):
        return cs.make_server(self.agent, port, TOKEN, False)

    def test_service_settings_need_operator(self):
        self.assertEqual(self.as_user("T\\viewer").req("POST", "/api/service/settings", {"notify": False})[0], 403)
        self.assertEqual(self.as_user("T\\operator").req("POST", "/api/service/settings", {"notify": False})[0], 200)
        self.assertFalse(self.hub.settings["notify"])

    def test_kill_orphan_needs_operator(self):
        self.assertEqual(self.as_user("T\\viewer").req("POST", "/api/service/kill-orphan", {})[0], 403)
        self.assertEqual(self.as_user("T\\operator").req("POST", "/api/service/kill-orphan", {})[0], 409)

    def test_service_page_has_both_panels(self):
        st, _, body, _ = self.as_user("T\\viewer").req("GET", "/", token=None, accept="text/html")
        self.assertEqual(st, 200)
        self.assertIn("auChip", body)

    def test_launcher_uses_the_control_token(self):
        info = {"port": self.port, "token": TOKEN, "control": self.srv.auth.control_token}
        self.assertEqual(cs.call(info, "/api/state")[0], 200)                       # --status and the health check
        self.assertEqual(cs.call(dict(info, control=""), "/api/state")[0], 401)
        self.assertEqual(cs.call(info, "/api/start", "POST", {})[0], 401)           # control token is NOT a master key
        self.assertTrue(cs.responsive(info))

    def test_quit_from_the_launcher_stops_the_service(self):
        info = {"port": self.port, "token": TOKEN, "control": self.srv.auth.control_token}
        self.assertEqual(cs.call(info, "/api/service/shutdown", "POST", {})[0], 200)
        for _ in range(60):
            if self.srv.stopping:
                break
            time.sleep(0.05)
        self.assertTrue(self.srv.stopping)


class Preflight(unittest.TestCase):
    def setUp(self):
        import preflight
        self.pf = preflight
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.saved = (preflight.REPO_ROOT, preflight.secret_store, preflight._SECRET_ERROR)
        preflight.REPO_ROOT = self.tmp.name
        preflight._SECRET_ERROR = None
        preflight.secret_store = ss
        ss._STORE = ss.SecretStore([FakeBackend({"JIRA_API_TOKEN": OLD, "GITHUB_TOKEN": "gh-token-old-123456"})])

    def tearDown(self):
        self.pf.REPO_ROOT, self.pf.secret_store, self.pf._SECRET_ERROR = self.saved
        ss._STORE = None
        self.tmp.cleanup()

    def results(self):
        return {n.split()[0]: (p, d) for n, p, d in self.pf.check_secrets()}

    def test_group_is_part_of_run_all(self):
        self.assertIn("check_secrets", inspect.getsource(self.pf.run_all))

    def test_passes_when_tokens_are_in_the_store_and_env_is_clean(self):
        (Path(self.tmp.name) / ".env").write_text("JIRA_BASE_URL=https://x.atlassian.net\n")
        r = self.results()
        self.assertEqual((r["E1"][0], r["E2"][0]), (True, True))

    def test_fails_while_tokens_are_still_in_env(self):
        (Path(self.tmp.name) / ".env").write_text("JIRA_API_TOKEN=abcdefghijklmnop\n")
        r = self.results()
        self.assertFalse(r["E2"][0])
        self.assertIn("migrate --yes", r["E2"][1])

    def test_fails_when_a_token_is_missing_from_the_store(self):
        ss._STORE = ss.SecretStore([FakeBackend({"JIRA_API_TOKEN": OLD})])
        r = self.results()
        self.assertFalse(r["E1"][0])
        self.assertIn("GITHUB_TOKEN", r["E1"][1])

    def test_fails_clearly_when_the_store_cannot_be_read(self):
        ss._STORE = ss.SecretStore([FakeBackend(fail=True)])
        res = self.pf.check_secrets()
        self.assertFalse(res[0][1])
        self.assertIn("locked", res[0][2])

    def test_values_never_appear_in_results(self):
        (Path(self.tmp.name) / ".env").write_text("JIRA_API_TOKEN=abcdefghijklmnop\n")
        self.assertNotIn("abcdefghijklmnop", str(self.pf.check_secrets()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
