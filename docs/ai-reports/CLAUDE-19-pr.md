## [CLAUDE-19] Guest count above 10 is accepted on event registration
**Jira:** <Jira link>
**Type:** Bug fix
**Status:** Draft — awaiting human review

### Problem
The registration form currently saves registrations with 11 guests, although the supported range is 1 to 10.

### Root Cause / Design
`EventRegistrationController.validateRegistrationDetails` compared the count to `MAX_GUESTS + 1`, allowing 11 when `MAX_GUESTS` is 10.

### Solution
Changed the shared server-side validation to reject counts greater than `MAX_GUESTS`. Updated the regression test to verify that exactly 11 guests is rejected with the expected clear message; existing tests cover 0, 1, and 10.

### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Enforce the configured maximum of 10 guests. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Assert the 11-guest rejection and clear validation message. |
| `docs/ai-reports/CLAUDE-19.md` | Add the solution report. |
| `docs/ai-reports/CLAUDE-19-pr.md` | Add this PR description. |
| `docs/ai-reports/CLAUDE-19-jira.md` | Add Jira comment draft with PR link placeholder. |

### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testZeroGuestsIsRejected` | Verify the lower invalid boundary. | Existing; not run locally |
| `EventRegistrationControllerTest.testMinimumBoundaryGuestsIsAccepted` | Verify minimum valid count. | Existing; not run locally |
| `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` | Verify maximum valid count. | Existing; not run locally |
| `EventRegistrationControllerTest.testElevenGuestsIsRejected` | Reproduce and prevent the defect; assert validation message. | Updated; not run locally |

### Acceptance Criteria
- [x] Guest counts from 1 to 10 are accepted.
- [x] Guest counts above 10 are rejected with a clear message.
- [x] Apex tests cover 0, 1, 10, and 11.

### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Event registration Apex validation for create and update.
- **Rollback:** Revert this PR.

### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes

> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-19.md`
