## [CLAUDE-17] Same-day event registration is incorrectly accepted

**Jira:** <Jira link>
**Type:** Bug fix
**Status:** Draft — awaiting human review

### Problem
The server-side registration controller accepted event dates equal to today, despite the event date needing to be strictly in the future. This allowed a same-day registration record to be created.

### Root Cause
`EventRegistrationController.validateInputs` compared the event date with `Date.today()` using `<`, which rejected past dates but not today.

### Solution
Changed the server-side date comparison to reject event dates on or before today. Strengthened the existing regression tests to assert the exact validation message for today and past dates; the existing successful-registration test continues to cover a future date.

### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Reject event dates on or before today. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Assert the exact validation message for today's and past dates. |
| `docs/ai-reports/CLAUDE-17.md` | Add the solution report. |
| `docs/ai-reports/CLAUDE-17-pr.md` | Add this PR description. |
| `docs/ai-reports/CLAUDE-17-jira.md` | Add the Jira comment draft. |

### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testTodayEventDateIsRejected` | Reject today with the expected validation message. | Not run locally |
| `EventRegistrationControllerTest.testPastEventDateIsRejected` | Preserve rejection of past dates and expected message. | Not run locally |
| `EventRegistrationControllerTest.testSuccessfulRegistration` | Preserve acceptance of future dates. | Not run locally |

### Acceptance Criteria
- [x] An event date of today is rejected with `Event date must be in the future.`
- [x] Past event dates remain rejected.
- [x] Future event dates remain accepted.

### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Event registration controller date validation and its Apex tests.
- **Rollback:** Revert this PR.

### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes

> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-17.md`
