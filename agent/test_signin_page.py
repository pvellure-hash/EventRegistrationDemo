"""Offline tests for the sign-in page (signin_page.py, v14). No browser, no network.
Run from agent\\:  python test_signin_page.py"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import signin_page as sp  # noqa: E402


def page(mode="accounts", **kw):
    kw.setdefault("registration", True)
    return sp.render(mode, nonce="NONCE123", **kw)


def visible_text(html):
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


class RenderTests(unittest.TestCase):
    def test_every_mode_renders_with_no_placeholder_left(self):
        for mode in sp.MODES:
            for reg in (True, False):
                with self.subTest(mode=mode, reg=reg):
                    html, _ = page(mode, registration=reg)
                    self.assertNotIn("__", html.replace("__proto__", ""))
                    self.assertIn(f"const MODE={json.dumps(mode)}", html)

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            sp.render("off")

    def test_button_text_matches_the_mode(self):
        want = {"accounts": ">Sign in<", "windows": "Sign in with Windows", "entra": "Sign in with Microsoft",
                "local": ">Continue<"}
        for mode, text in want.items():
            with self.subTest(mode=mode):
                self.assertIn(text, page(mode)[0])
        self.assertNotIn("Sign in with Microsoft", page("accounts")[0])

    def test_the_page_is_plain(self):
        """A sign-in page, not a brochure: no marketing panel, no pipeline, no feature tiles."""
        text = visible_text(page("accounts", registration=False)[0])
        self.assertLess(len(text.split()), 30, text)
        for word in ("proof of concept", "poc", "governed", "pipeline", "automated", "human approval", "audit trail",
                     "role-based", "demo", "prototype", "beta", "enterprise-grade", "certified", "compliant"):
            self.assertNotIn(word, text.lower())
        self.assertIn("Authorized users only. Activity on this console is logged.", text)

    def test_windows_page(self):
        html = page("windows")[0]
        self.assertIn("PIN, fingerprint, face or password", html)
        self.assertNotIn('type="password"', html)
        self.assertNotIn("Request access", html)

    def test_accounts_page_has_the_two_step_request(self):
        html = page("accounts", min_password=16, code_minutes=10)[0]
        for needle in ("Request access", "Step 1 of 2", "Step 2 of 2", "Verify your email", "Send a new code",
                       'autocomplete="one-time-code"', 'inputmode="numeric"', "At least 16 characters",
                       "const MINPW=16", "const MINUTES=10", "/auth/register/start", "/auth/register/verify",
                       "/auth/register/resend", "Request sent"):
            self.assertIn(needle, html)
        self.assertNotIn("'/auth/register'", html)            # the old single-step route is gone from the page

    def test_request_access_can_be_switched_off(self):
        html = page("accounts", registration=False)[0]
        self.assertIn("const REG=false", html)
        for gone in ("Request access", 'id="fReg"', 'id="fCode"'):
            self.assertNotIn(gone, html)
        self.assertIn('type="password"', html)

    def test_registration_is_never_offered_outside_accounts_mode(self):
        for mode in ("windows", "entra", "local"):
            html = page(mode, registration=True)[0]
            self.assertIn("const REG=false", html)
            self.assertNotIn('id="fReg"', html)

    def test_allowed_domains_are_not_listed_on_the_page(self):
        self.assertNotIn("deloitte.ca", page()[0])
        self.assertNotIn("gov.bc.ca", page()[0])


class SafetyTests(unittest.TestCase):
    def test_csp_is_strict_and_matches_the_page(self):
        html, csp = page()
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        for d in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'", "connect-src 'self'"):
            self.assertIn(d, csp)
        self.assertEqual(html.count("<style"), 1)
        self.assertEqual(html.count("<script"), 1)
        self.assertIn('<style nonce="NONCE123">', html)
        self.assertIn('<script nonce="NONCE123">', html)

    def test_nothing_the_csp_would_block(self):
        for mode in sp.MODES:
            html = page(mode)[0]
            with self.subTest(mode=mode):
                self.assertIsNone(re.search(r"<[^>]+\sstyle=", html))
                self.assertIsNone(re.search(r"\son[a-z]+=", html, re.I))
                for bad in ("javascript:", "http://", "https://", "data:", "<link", "<img"):
                    self.assertNotIn(bad, html.lower() if bad == "javascript:" else html)

    def test_no_open_redirect_and_no_script_breakout(self):
        for nxt, want in (("https://evil.example", '"/"'), ("//evil.example", '"/"'), ("/\\evil.example", '"/"'),
                          ("javascript:alert(1)", '"/"'), ("", '"/"'), ("/dashboard?t=1", '"/dashboard?t=1"')):
            with self.subTest(nxt=nxt):
                self.assertIn("const next=" + want, page(nxt=nxt)[0])
        evil = page(nxt="/x</script><script>alert(1)</script>")[0]
        self.assertEqual(evil.count("</script>"), 1)

    def test_nonce_is_fresh_when_not_given(self):
        self.assertNotEqual(sp.render("accounts")[1], sp.render("accounts")[1])

    def test_passwords_and_codes_are_cleared_and_never_stored(self):
        html = page()[0]
        self.assertIn("$('p').value=''", html)
        self.assertIn("rp.value='';rp2.value=''", html)
        self.assertIn("rc.value=''", html)
        self.assertNotIn("localStorage", html)
        self.assertEqual(len(re.findall(r"sessionStorage\.setItem\(", html)), 1)      # only the console token

    def test_token_handling_matches_the_console(self):
        html = page()[0]
        self.assertIn("new URLSearchParams(location.hash.slice(1)).get('t')", html)
        self.assertIn("'X-Console-Token':TOKEN", html)


class AccessibilityTests(unittest.TestCase):
    def test_labels_headings_and_live_regions(self):
        html = page()[0]
        for field in ("u", "p", "rn", "re", "ru", "rp", "rp2", "rr", "rc"):
            self.assertIn(f'<label for="{field}">', html, field)
            self.assertIn(f'id="{field}"', html, field)
        self.assertEqual(html.count('aria-live="polite"'), 4)       # one message area per view
        self.assertGreaterEqual(html.count("<h1"), 4)
        self.assertIn('lang="en"', html)
        self.assertIn("prefers-reduced-motion", html)

    def test_ids_are_unique(self):
        ids = re.findall(r'\sid="([^"]+)"', page()[0])
        self.assertEqual(len(ids), len(set(ids)), [i for i in ids if ids.count(i) > 1])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ScriptTests(unittest.TestCase):
    def test_the_script_parses_in_every_mode(self):
        for mode in sp.MODES:
            for reg in (True, False):
                js = re.search(r'<script nonce="NONCE123">(.*)</script>', page(mode, registration=reg)[0], re.S).group(1)
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                    fh.write(js)
                r = subprocess.run(["node", "--check", fh.name], capture_output=True, text=True)
                Path(fh.name).unlink()
                self.assertEqual(r.returncode, 0, f"{mode}/{reg}: {r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
