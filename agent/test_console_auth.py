"""Offline tests for console_auth.py v12 (fake Microsoft sign-in, fake HTTP handler).
Run from agent\\:  python test_console_auth.py"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_auth as ca  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OID = "99999999-8888-7777-6666-555555555555"
TOK = "console-token"


class Headers(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


class FakeHandler:
    def __init__(self, method, path, cookies=None, accept="application/json", token=None, control=None):
        self.command, self.path = method, path
        h = Headers({"Accept": accept})
        if cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if token is not None:
            h["X-Console-Token"] = token
        if control is not None:
            h["X-Console-Control"] = control
        self.headers, self.wfile, self.code, self.sent = h, io.BytesIO(), None, []

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue() or b"{}")

    def header(self, k):
        return [v for kk, v in self.sent if kk == k]


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


def claims(**over):
    c = {"tid": TENANT, "aud": CLIENT, "exp": 1_900_000_000, "oid": OID,
         "preferred_username": "pvellure@deloitte.ca", "name": "Pavan Vellure", "amr": ["pwd", "mfa"]}
    c.update(over)
    return c


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.access, self.audit = d / "access.json", d / "auth-audit.jsonl"
        self.write_access([
            {"upn": "pvellure@deloitte.ca", "oid": OID, "role": "approver"},
            {"upn": "ops@deloitte.ca", "role": "operator"},
            {"upn": "viewer@deloitte.ca", "role": "viewer"},
            {"windows": "CORP\\pvellure", "role": "approver"},
            {"upn": "bad@deloitte.ca", "role": "superuser"},
        ])
        self.clock = Clock()
        self.env = {"CONSOLE_AUTH_MODE": "entra", "ENTRA_TENANT_ID": TENANT, "AZURE_CLIENT_ID": CLIENT,
                    "CONSOLE_ACCESS_FILE": str(self.access)}

    def tearDown(self):
        self.tmp.cleanup()

    def write_access(self, users):
        self.access.write_text(json.dumps({"users": users}))
        self._n = getattr(self, "_n", 0) + 1
        t = time.time() + 10 * self._n                      # force a new mtime
        os.utime(self.access, (t, t))

    def gate(self, env_over=None, the_claims=None, signer=None):
        cfg = ca.AuthConfig(dict(self.env, **(env_over or {})))
        return ca.AuthGate(cfg, self.audit, port=8765, clock=self.clock, console_token=TOK,
                           signer=signer or (lambda: the_claims or claims()))

    def sign_in(self, g):
        h = FakeHandler("POST", "/auth/login", token=TOK)
        g.handle(h)
        self.assertEqual(h.code, 202, h.json())
        name, val = h.header("Set-Cookie")[0].split(";")[0].split("=", 1)
        for _ in range(300):
            if not g.login_running:
                break
            time.sleep(0.01)
        p = FakeHandler("GET", "/auth/poll", cookies={name: val}, token=TOK)
        g.handle(p)
        sess = None
        for c in p.header("Set-Cookie"):
            n, v = c.split(";")[0].split("=", 1)
            if n == g.cookie and v:
                sess = v
        return p, sess

    def req(self, g, method, path, sess, accept="application/json", **kw):
        h = FakeHandler(method, path, cookies={g.cookie: sess} if sess else None, accept=accept, **kw)
        return h, g.require(h)

    def events(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()] if self.audit.exists() else []

    # ---------------- config
    def test_tenant_must_be_guid(self):
        for bad in ("common", "organizations", ""):
            with self.assertRaises(ca.AuthConfigError):
                ca.AuthConfig(dict(self.env, ENTRA_TENANT_ID=bad))

    def test_no_off_mode_and_local_needs_flag(self):
        with self.assertRaises(ca.AuthConfigError):
            ca.AuthConfig(dict(self.env, CONSOLE_AUTH_MODE="off"))
        with self.assertRaises(ca.AuthConfigError):
            ca.AuthConfig({"CONSOLE_AUTH_MODE": "local", "CONSOLE_ACCESS_FILE": str(self.access)})

    # ---------------- routes (the real ones from run_console.py / console_service.py)
    def test_route_roles(self):
        r = ca.AuthGate.required_role
        for path in ("/", "/api/state?t=x", "/api/stream", "/api/queue", "/api/sessions", "/api/session",
                     "/api/transcript", "/dashboard"):
            self.assertEqual(r("GET", path), "viewer", path)
        for path in ("/api/start", "/api/stop", "/api/util", "/api/service/settings",
                     "/api/service/kill-orphan", "/api/service/shutdown"):
            self.assertEqual(r("POST", path), "operator", path)
        self.assertEqual(r("POST", "/api/answer"), "approver")
        self.assertEqual(r("POST", "/api/brand-new"), "approver")       # deny by default
        self.assertEqual(r("DELETE", "/api/x"), "approver")

    # ---------------- not signed in
    def test_page_redirects_to_signin_and_keeps_target(self):
        h, u = self.req(self.gate(), "GET", "/dashboard?t=abc", None, accept="text/html,*/*")
        self.assertEqual((h.code, u), (302, None))
        self.assertEqual(h.header("Location")[0], "/auth/signin?next=%2Fdashboard%3Ft%3Dabc")

    def test_api_without_session_is_401(self):
        for m, p in (("GET", "/api/state"), ("POST", "/api/start"), ("POST", "/api/answer")):
            h, u = self.req(self.gate(), m, p, None)
            self.assertEqual((h.code, u), (401, None))

    def test_forged_cookie_rejected(self):
        self.assertEqual(self.req(self.gate(), "GET", "/api/state", "made-up")[0].code, 401)

    # ---------------- /auth/* needs the console token (except the page)
    def test_signin_page_is_public_but_calls_need_token(self):
        g = self.gate()
        h = FakeHandler("GET", "/auth/signin")
        self.assertTrue(g.handle(h))
        self.assertEqual(h.code, 200)
        for m, p in (("POST", "/auth/login"), ("GET", "/auth/poll"), ("POST", "/auth/logout"), ("GET", "/auth/me")):
            h = FakeHandler(m, p)
            g.handle(h)
            self.assertEqual((m, p, h.code), (m, p, 401))
            h = FakeHandler(m, p, token="wrong")
            g.handle(h)
            self.assertEqual(h.code, 401)

    def test_empty_console_token_never_matches(self):
        cfg = ca.AuthConfig(self.env)
        g = ca.AuthGate(cfg, self.audit, console_token="")
        h = FakeHandler("POST", "/auth/login", token="")
        g.handle(h)
        self.assertEqual(h.code, 401)

    def test_signin_page_csp_no_open_redirect_no_script_break(self):
        g = self.gate()
        for nxt, want in (("https://evil.example", '"/"'), ("//evil.example", '"/"'), ("/\\evil.example", '"/"'),
                          ("/dashboard?t=1", '"/dashboard?t=1"')):
            h = FakeHandler("GET", "/auth/signin?next=" + ca.quote(nxt, safe=""))
            g.handle(h)
            self.assertIn("const next=" + want, h.wfile.getvalue().decode(), nxt)
        h = FakeHandler("GET", "/auth/signin?next=" + ca.quote("/x</script><script>alert(1)", safe=""))
        g.handle(h)
        self.assertNotIn("</script><script>alert", h.wfile.getvalue().decode())
        csp = h.header("Content-Security-Policy")[0]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)

    # ---------------- sign in (Entra claims)
    def test_sign_in_and_cookie_flags(self):
        g = self.gate()
        p, sess = self.sign_in(g)
        self.assertEqual(p.json()["state"], "done")
        c = [x for x in p.header("Set-Cookie") if x.startswith(g.cookie + "=")][0]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, c)
        self.assertGreaterEqual(len(sess), 40)
        h, u = self.req(g, "POST", "/api/answer", sess)
        self.assertEqual(u["user"], "pvellure@deloitte.ca")

    def test_denied_sign_ins(self):
        cases = {"other tenant": claims(tid="00000000-0000-0000-0000-000000000000"), "wrong app": claims(aud="x"),
                 "expired": claims(exp=1), "not listed": claims(preferred_username="stranger@deloitte.ca"),
                 "oid mismatch": claims(oid="11111111-0000-0000-0000-000000000000"),
                 "invalid role in file": claims(preferred_username="bad@deloitte.ca", oid="")}
        for label, c in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.sign_in(self.gate(the_claims=c))[1])

    def test_mfa_required(self):
        self.assertIsNone(self.sign_in(self.gate({"CONSOLE_REQUIRE_MFA": "true"}, claims(amr=["pwd"])))[1])
        self.assertIsNotNone(self.sign_in(self.gate({"CONSOLE_REQUIRE_MFA": "true"}))[1])

    def test_signer_exception_is_a_clean_error(self):
        def boom():
            raise PermissionError("AADSTS50105 user not assigned")
        g = self.gate(signer=boom)
        p, sess = self.sign_in(g)
        self.assertEqual(p.code, 403)
        self.assertIn("AADSTS50105", p.json()["reason"])
        self.assertFalse(g.login_running)

    def test_poll_needs_its_own_pending_cookie(self):
        g = self.gate()
        g.handle(FakeHandler("POST", "/auth/login", token=TOK))
        for _ in range(300):
            if not g.login_running:
                break
            time.sleep(0.01)
        p = FakeHandler("GET", "/auth/poll", cookies={g.pending_cookie: "stolen"}, token=TOK)
        g.handle(p)
        self.assertEqual(p.code, 400)

    def test_missing_access_file_blocks_sign_in(self):
        self.access.unlink()
        h = FakeHandler("POST", "/auth/login", token=TOK)
        self.gate().handle(h)
        self.assertEqual(h.code, 503)

    # ---------------- roles
    def test_viewer_operator_approver(self):
        v = self.gate(the_claims=claims(preferred_username="viewer@deloitte.ca", oid=""))
        sv = self.sign_in(v)[1]
        self.assertIsNotNone(self.req(v, "GET", "/api/state", sv)[1])
        self.assertEqual(self.req(v, "POST", "/api/start", sv)[0].code, 403)
        self.assertEqual(self.req(v, "POST", "/api/answer", sv)[0].code, 403)
        o = self.gate(the_claims=claims(preferred_username="ops@deloitte.ca", oid=""))
        so = self.sign_in(o)[1]
        self.assertIsNotNone(self.req(o, "POST", "/api/start", so)[1])
        self.assertIsNotNone(self.req(o, "POST", "/api/service/shutdown", so)[1])
        h = self.req(o, "POST", "/api/answer", so)[0]
        self.assertEqual((h.code, h.json()["need"]), (403, "approver"))
        self.assertIn("denied", [e["event"] for e in self.events()])

    def test_removing_or_downgrading_a_user_takes_effect_at_once(self):
        g = self.gate()
        sess = self.sign_in(g)[1]
        self.write_access([{"upn": "pvellure@deloitte.ca", "oid": OID, "role": "viewer"}])
        self.assertEqual(self.req(g, "POST", "/api/answer", sess)[0].code, 403)
        self.write_access([{"upn": "ops@deloitte.ca", "role": "operator"}])
        self.assertEqual(self.req(g, "GET", "/api/state", sess)[0].code, 401)
        self.assertIn("access_revoked", [e["event"] for e in self.events()])

    # ---------------- time limits
    def test_idle_timeout_and_max_session(self):
        g = self.gate()
        sess = self.sign_in(g)[1]
        for _ in range(15):                                 # 15 x 29 min of activity = 7.25 h
            self.clock.t += 29 * 60
            self.assertIsNotNone(self.req(g, "GET", "/api/state", sess)[1])
        self.clock.t += 29 * 60 * 2                         # past 8 h
        self.assertEqual(self.req(g, "GET", "/api/state", sess)[0].code, 401)
        g2 = self.gate()
        s2 = self.sign_in(g2)[1]
        self.clock.t += 31 * 60
        self.assertEqual(self.req(g2, "GET", "/api/state", s2)[0].code, 401)

    def test_approve_needs_a_recent_sign_in(self):
        g = self.gate()
        sess = self.sign_in(g)[1]
        for _ in range(3):
            self.clock.t += 25 * 60
            self.req(g, "GET", "/api/state", sess)
        h, u = self.req(g, "POST", "/api/answer", sess)
        self.assertEqual((h.code, h.json()["error"]), (401, "reauth_required"))
        self.assertIsNotNone(self.req(g, "POST", "/api/start", sess)[1])         # operator action still fine
        self.assertIsNotNone(self.req(g, "POST", "/api/answer", self.sign_in(g)[1])[1])

    # ---------------- launcher control token
    def test_control_token_works_for_two_routes_only(self):
        g = self.gate()
        ok = FakeHandler("GET", "/api/state", control=g.control_token)
        self.assertIsNotNone(g.require(ok))
        ok = FakeHandler("POST", "/api/service/shutdown", control=g.control_token)
        self.assertIsNotNone(g.require(ok))
        for m, p in (("POST", "/api/start"), ("POST", "/api/answer"), ("GET", "/api/transcript"), ("POST", "/api/util")):
            h = FakeHandler(m, p, control=g.control_token)
            self.assertIsNone(g.require(h))
            self.assertEqual(h.code, 401)
        h = FakeHandler("GET", "/api/state", control="guess")
        self.assertIsNone(g.require(h))

    # ---------------- logout, me, audit, page additions
    def test_logout_and_me(self):
        g = self.gate()
        sess = self.sign_in(g)[1]
        h = FakeHandler("GET", "/auth/me", cookies={g.cookie: sess}, token=TOK)
        g.handle(h)
        self.assertEqual((h.code, h.json()["role"]), (200, "approver"))
        h = FakeHandler("POST", "/auth/logout", cookies={g.cookie: sess}, token=TOK)
        g.handle(h)
        self.assertIn("Max-Age=0", h.header("Set-Cookie")[0])
        self.assertEqual(self.req(g, "GET", "/api/state", sess)[0].code, 401)

    def test_audit_records_actions_and_no_secrets(self):
        g = self.gate()
        sess = self.sign_in(g)[1]
        self.req(g, "POST", "/api/answer", sess)
        handler = FakeHandler("POST", "/api/answer")
        handler.server = type("S", (), {"auth": g})()
        handler.user = {"user": "pvellure@deloitte.ca", "role": "approver"}
        ca.record(handler, "review_decision", ticket="CLAUDE-22", decision="approved")
        text = self.audit.read_text()
        for secret in (sess, g.control_token, TOK, g.console_token):
            self.assertNotIn(secret, text)
        ev = [e for e in self.events() if e["event"] == "review_decision"][0]
        self.assertEqual((ev["user"], ev["ticket"], ev["decision"]), ("pvellure@deloitte.ca", "CLAUDE-22", "approved"))

    def test_record_is_a_noop_without_auth(self):
        ca.record(FakeHandler("POST", "/api/answer"), "review_decision")     # no server attribute: must not raise

    def test_inject_ui(self):
        out = ca.inject_ui(b"<html><body><main></main></body></html>").decode()
        self.assertIn("auChip", out)
        self.assertLess(out.index("auChip"), out.rindex("</body>"))
        self.assertIn("window.fetch=", out)

    # ---------------- local mode
    def test_local_mode(self):
        old = os.environ.get("USERDOMAIN")
        os.environ["USERDOMAIN"] = "CORP"
        real = ca.getpass.getuser
        try:
            g = self.gate({"CONSOLE_AUTH_MODE": "local", "CONSOLE_AUTH_ALLOW_LOCAL": "true"})
            ca.getpass.getuser = lambda: "pvellure"
            self.assertIsNotNone(self.req(g, "POST", "/api/answer", self.sign_in(g)[1])[1])
            ca.getpass.getuser = lambda: "intruder"
            self.assertIsNone(self.sign_in(g)[1])
        finally:
            ca.getpass.getuser = real
            os.environ.pop("USERDOMAIN", None) if old is None else os.environ.__setitem__("USERDOMAIN", old)

    # ---------------- start-up check and CLI
    def with_env(self, **kv):
        saved = {k: os.environ.get(k) for k in kv}
        os.environ.update({k: v for k, v in kv.items() if v is not None})
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])

    def agent_dir(self):
        d = Path(self.tmp.name) / "repo" / "agent"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_startup_check_messages(self):
        self.with_env(JIRA_API_TOKEN="t1" * 10, GITHUB_TOKEN="t2" * 10, CONSOLE_AUTH_MODE="local",
                      CONSOLE_AUTH_ALLOW_LOCAL="true", CONSOLE_ACCESS_FILE=str(self.access))
        self.assertEqual(ca.startup_check(self.agent_dir()).mode, "local")
        self.access.unlink()
        with self.assertRaises(RuntimeError) as cm:
            ca.startup_check(self.agent_dir())
        self.assertIn("console_auth.py init", str(cm.exception))
        self.write_access([{"upn": "x@y.com", "role": "viewer"}])
        with self.assertRaises(RuntimeError) as cm:
            ca.startup_check(self.agent_dir())
        self.assertIn("no approver", str(cm.exception))

    def test_startup_check_missing_settings_suggests_local(self):
        self.with_env(JIRA_API_TOKEN="t1" * 10, GITHUB_TOKEN="t2" * 10, CONSOLE_AUTH_MODE="entra",
                      ENTRA_TENANT_ID="", ENTRA_CLIENT_ID="", AZURE_CLIENT_ID="", CONSOLE_ACCESS_FILE=str(self.access))
        with self.assertRaises(RuntimeError) as cm:
            ca.startup_check(self.agent_dir())
        self.assertIn("CONSOLE_AUTH_MODE=local", str(cm.exception))

    def test_cli_init_add_remove_list(self):
        target = Path(self.tmp.name) / "cli" / "access.json"
        self.with_env(CONSOLE_ACCESS_FILE=str(target), USERDOMAIN="CORP")
        self.assertEqual(ca._cli(["init", "--upn", "me@x.com"]), 0)
        users = json.loads(target.read_text())["users"]
        self.assertEqual((users[0]["role"], users[0]["upn"]), ("approver", "me@x.com"))
        self.assertEqual(ca._cli(["init"]), 1)                                   # never overwrites
        self.assertEqual(ca._cli(["add", "--upn", "a@x.com", "--role", "viewer"]), 0)
        self.assertEqual(ca._cli(["add", "--upn", "a@x.com", "--role", "operator"]), 0)   # updates, no duplicate
        users = json.loads(target.read_text())["users"]
        self.assertEqual([u["role"] for u in users if u.get("upn") == "a@x.com"], ["operator"])
        self.assertEqual(ca._cli(["remove", "--upn", "a@x.com"]), 0)
        self.assertEqual(len(json.loads(target.read_text())["users"]), 1)
        self.assertEqual(ca._cli(["list"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
