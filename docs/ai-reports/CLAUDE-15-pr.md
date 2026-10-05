## [CLAUDE-15] Email format validation is not enforced

**Jira:** CLAUDE-15
**Type:** Bug fix
**Status:** Draft — awaiting human review

### Problem
The registration form must reject malformed email addresses and must continue accepting valid addresses.

### Root Cause
In this checkout, `EventRegistrationController.validateInputs()` already rejects blank or malformed email addresses using the existing regex helper. The regression test did not use the exact reported value (`notanemail`) or assert the requested error text.

### Solution
Strengthened the Apex regression test to submit `notanemail` and assert the exact clear validation message. Existing server-side validation remains authoritative, with the LWC check providing early feedback.

### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Assert rejection and error text for the exact reported invalid input |
| `docs/ai-reports/CLAUDE-15.md` | Solution Report |
| `docs/ai-reports/CLAUDE-15-pr.md` | PR description |
| `docs/ai-reports/CLAUDE-15-jira.md` | Jira comment text |

### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testInvalidEmailIsRejected` | Reject `notanemail` with the required message | Not run locally |
| `EventRegistrationControllerTest.testSuccessfulRegistration` | Verify valid email input still creates a registration | Not run locally |

### Acceptance Criteria
- [x] Submitting an invalid email format is rejected with a clear error message.
- [x] Submitting a valid email format still works correctly.
- [ ] Existing tests pass (not run locally; CI must run the Apex suite).

### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Regression coverage for registration email validation
- **Rollback:** Revert this PR

### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes

> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-15.md`
