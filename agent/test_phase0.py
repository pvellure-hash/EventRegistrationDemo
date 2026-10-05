"""agent/test_phase0.py - offline tests for Phase 0 modules.

No network, no Salesforce, no Copilot, no Jira writes. Uses a temp log dir.
Run:  python test_phase0.py
"""
import os
import sys
import json
import time
import tempfile
import unittest
import importlib
import datetime

TMP = tempfile.mkdtemp(prefix="phase0-")
os.environ["AGENT_LOG_DIR"] = TMP
os.environ["MONTHLY_BUDGET_USD"] = "1.00"
os.environ["MAX_TICKETS_PER_DAY"] = "2"
os.environ["TICKET_CREDIT_CEILING"] = "30"
os.environ.pop("AGENT_RUN_ID", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact, events, guards, security_checks  # noqa: E402


def reset():
    for f in os.listdir(TMP):
        p = os.path.join(TMP, f)
        if os.path.isfile(p):
            os.remove(p)
    lk = os.path.join(TMP, "locks")
    if os.path.isdir(lk):
        for f in os.listdir(lk):
            os.remove(os.path.join(lk, f))
    os.environ.pop("AGENT_RUN_ID", None)


class Redaction(unittest.TestCase):
    def test_secrets(self):
        s = ("token ghp_" + "a" * 36 + " and github_pat_" + "B" * 50 + " jira ATATT" + "x" * 30 +
             " sf force://PlatformCLI::abc@test.my.salesforce.com password=Hunter22 key AKIAABCDEFGHIJKLMNOP")
        r = redact.redact(s)
        for leaked in ("ghp_aaaa", "github_pat_BBB", "ATATTxxx", "force://", "Hunter22", "AKIAABCD"):
            self.assertNotIn(leaked, r)
        self.assertIn("password=[REDACTED:SECRET]", r)

    def test_pii(self):
        r = redact.redact("Call 250-555-0199 or mail jane.doe@example.com, SIN 123 456 789")
        self.assertNotIn("jane.doe@example.com", r)
        self.assertNotIn("250-555-0199", r)
        self.assertNotIn("123 456 789", r)

    def test_harmless_text_untouched(self):
        t = "Fixed off-by-one in EventRegistrationService.cls line 42"
        self.assertEqual(redact.redact(t), t)

    def test_nested(self):
        o = redact.redact_obj({"a": ["pwd: s3cretValue"], "b": 5})
        self.assertNotIn("s3cretValue", json.dumps(o))
        self.assertEqual(o["b"], 5)


class Events(unittest.TestCase):
    def setUp(self):
        reset()

    def test_append_and_redact(self):
        events.emit_event("CLAUDE-1", "agent", "ok", ai_credits=10, detail="token ghp_" + "z" * 36)
        events.emit_event("CLAUDE-1", "review", "ok")
        evs = events.read_events()
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["est_cost_usd"], 0.1)
        self.assertNotIn("ghp_zzz", json.dumps(evs))
        self.assertEqual(evs[0]["run_id"], evs[1]["run_id"])  # same run

    def test_never_raises(self):
        blocker = os.path.join(TMP, "not-a-dir")
        open(blocker, "w").close()                      # a FILE where a folder is expected
        os.environ["AGENT_LOG_DIR"] = os.path.join(blocker, "logs")
        try:
            self.assertIsNone(events.emit_event("CLAUDE-1", "x", "ok"))
        finally:
            os.environ["AGENT_LOG_DIR"] = TMP

    def test_aggregates(self):
        events.emit_event("CLAUDE-1", "agent", "ok", ai_credits=20)
        events.emit_event("CLAUDE-1", "agent", "ok", ai_credits=5)
        events.emit_event("CLAUDE-2", "agent", "ok", ai_credits=10)
        self.assertEqual(events.credits_for_ticket("CLAUDE-1"), 25)
        self.assertAlmostEqual(events.spend_this_month_usd(), 0.35)
        old = {"time": "2020-01-01T00:00:00+00:00", "ticket": "X", "ai_credits": 999}
        self.assertAlmostEqual(events.spend_this_month_usd(events.read_events() + [old]), 0.35)


class Guards(unittest.TestCase):
    def setUp(self):
        reset()

    def test_budget(self):
        self.assertTrue(guards.check_budget()[0])
        events.emit_event("CLAUDE-1", "agent", "ok", ai_credits=85)      # $0.85 of $1.00
        ok, why = guards.check_budget()
        self.assertTrue(ok); self.assertTrue(why.startswith("WARN"))
        events.emit_event("CLAUDE-2", "agent", "ok", ai_credits=20)      # $1.05
        self.assertFalse(guards.check_budget()[0])

    def test_daily_cap(self):
        for k in ("CLAUDE-1", "CLAUDE-2"):
            os.environ.pop("AGENT_RUN_ID", None)
            events.emit_event(k, "run", "started")
        self.assertFalse(guards.check_daily_cap()[0])

    def test_ticket_ceiling(self):
        events.emit_event("CLAUDE-9", "agent", "ok", ai_credits=29.9)
        self.assertTrue(guards.check_ticket_ceiling("CLAUDE-9")[0])
        events.emit_event("CLAUDE-9", "agent", "ok", ai_credits=0.2)
        self.assertFalse(guards.check_ticket_ceiling("CLAUDE-9")[0])
        self.assertTrue(guards.check_ticket_ceiling("CLAUDE-10")[0])

    def test_lock(self):
        self.assertTrue(guards.acquire_lock("CLAUDE-5")[0])
        ok, why = guards.acquire_lock("CLAUDE-5")
        self.assertFalse(ok); self.assertIn("already being processed", why)
        guards.release_lock("CLAUDE-5")
        self.assertTrue(guards.acquire_lock("CLAUDE-5")[0])
        guards.release_lock("CLAUDE-5")

    def test_stale_lock_recovered(self):
        guards.acquire_lock("CLAUDE-6")
        p = os.path.join(TMP, "locks", "CLAUDE-6.lock")
        old = time.time() - (guards.LOCK_STALE_MINUTES + 1) * 60
        os.utime(p, (old, old))
        self.assertTrue(guards.acquire_lock("CLAUDE-6")[0])
        guards.release_lock("CLAUDE-6")


def fake_sf(record):
    def _cli(args, timeout=90):
        return 0, json.dumps({"status": 0, "result": {"records": [record]}}), ""
    return _cli


class OrgGuard(unittest.TestCase):
    def setUp(self):
        os.environ["SF_TARGET_ORG"] = "my-org"
        os.environ.pop("ALLOWED_ORG_IDS", None)
        self._orig = security_checks._cli

    def tearDown(self):
        security_checks._cli = self._orig

    def run_with(self, rec):
        security_checks._cli = fake_sf(rec)
        return security_checks.check_salesforce_org()[0]

    def test_sandbox_allowed(self):
        self.assertTrue(self.run_with({"Id": "00D000000000001AAA", "Name": "UAT", "IsSandbox": True, "OrganizationType": "Enterprise Edition"})[1])

    def test_dev_edition_allowed(self):
        self.assertTrue(self.run_with({"Id": "00D000000000002AAA", "Name": "Dev", "IsSandbox": False, "OrganizationType": "Developer Edition"})[1])

    def test_production_refused(self):
        name, ok, why = self.run_with({"Id": "00D000000000003AAA", "Name": "PROD", "IsSandbox": False, "OrganizationType": "Enterprise Edition"})
        self.assertFalse(ok); self.assertIn("PRODUCTION", why)

    def test_allow_list(self):
        os.environ["ALLOWED_ORG_IDS"] = "00D000000000009"
        self.assertFalse(self.run_with({"Id": "00D000000000001AAA", "Name": "UAT", "IsSandbox": True, "OrganizationType": "x"})[1])

    def test_query_failure_fails_closed(self):
        security_checks._cli = lambda a, timeout=90: (1, "", "No authorization information found")
        self.assertFalse(security_checks.check_salesforce_org()[0][1])


class GitHubChecks(unittest.TestCase):
    def setUp(self):
        os.environ["GITHUB_TOKEN"] = "github_pat_" + "x" * 50
        os.environ["GITHUB_REPOSITORY"] = "me/EventRegistrationDemo"
        self._gh = security_checks._gh

    def tearDown(self):
        security_checks._gh = self._gh

    def fake(self, table):
        def _gh(path, token):
            for prefix, resp in table:
                if path.startswith(prefix):
                    return resp
            return 404, None, {}
        security_checks._gh = _gh

    def test_unprotected_fails(self):
        self.fake([("/repos/me/EventRegistrationDemo/branches/main", (200, {"protected": False}, {}))])
        self.assertFalse(security_checks.check_branch_protection()[0][1])

    def test_ruleset_pr_passes(self):
        self.fake([("/repos/me/EventRegistrationDemo/branches/main/protection", (403, None, {})),
                   ("/repos/me/EventRegistrationDemo/branches/main", (200, {"protected": True}, {})),
                   ("/repos/me/EventRegistrationDemo/rules/branches/main", (200, [{"type": "pull_request"}], {}))])
        r = security_checks.check_branch_protection()[0]
        self.assertTrue(r[1]); self.assertFalse(r[2].startswith("WARN"))

    def test_protected_unreadable_warns(self):
        self.fake([("/repos/me/EventRegistrationDemo/branches/main/protection", (403, None, {})),
                   ("/repos/me/EventRegistrationDemo/branches/main", (200, {"protected": True}, {})),
                   ("/repos/me/EventRegistrationDemo/rules/branches/main", (200, [], {}))])
        r = security_checks.check_branch_protection()[0]
        self.assertTrue(r[1]); self.assertTrue(r[2].startswith("WARN"))

    def test_classic_token_fails(self):
        os.environ["GITHUB_TOKEN"] = "ghp_" + "y" * 36
        self.fake([("/user", (200, {}, {"X-OAuth-Scopes": "repo, admin:org"}))])
        self.assertFalse(security_checks.check_github_token_scope()[0][1])

    def test_single_repo_token_passes(self):
        self.fake([("/user/repos", (200, [{"full_name": "me/EventRegistrationDemo", "permissions": {"admin": False}}], {}))])
        r = security_checks.check_github_token_scope()[0]
        self.assertTrue(r[1]); self.assertFalse(r[2].startswith("WARN"))

    def test_multi_repo_token_warns(self):
        self.fake([("/user/repos", (200, [{"full_name": "me/EventRegistrationDemo"}, {"full_name": "me/Other"}], {}))])
        self.assertTrue(security_checks.check_github_token_scope()[0][2].startswith("WARN"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
