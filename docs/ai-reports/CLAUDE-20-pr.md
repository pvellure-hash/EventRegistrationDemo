## [CLAUDE-20] Registration with zero guests is accepted
**Jira:** Not provided
**Type:** Bug fix
**Status:** Draft — awaiting human review
### Problem
Registration accepts zero guests even though valid guest counts must be between 1 and 10. The expected behavior is rejection with a clear guest-count validation message.
### Root Cause / Design
`EventRegistrationController.validateRegistrationDetails` compared the guest count with `MIN_GUESTS - 1`; because `MIN_GUESTS` is 1, zero passed the validation.
### Solution
Changed the lower-bound comparison to use `MIN_GUESTS` directly. Updated the Apex boundary assertions to identify coverage for CLAUDE-20; the existing tests cover 0, 1, 10, and 11 guests.
### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Reject counts below the minimum of one. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Ticket-tagged assertions for all four boundary values. |
| `docs/ai-reports/CLAUDE-20.md` | Solution report. |
| `docs/ai-reports/CLAUDE-20-pr.md` | PR description. |
| `docs/ai-reports/CLAUDE-20-jira.md` | Jira comment draft. |
### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testZeroGuestsIsRejected` | Regression coverage for zero guests and the clear error message. | Not run locally; Apex tests require org access. |
| `EventRegistrationControllerTest.testMinimumBoundaryGuestsIsAccepted` | Confirm one guest remains accepted. | Not run locally; Apex tests require org access. |
| `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` | Confirm ten guests remains accepted. | Not run locally; Apex tests require org access. |
| `EventRegistrationControllerTest.testElevenGuestsIsRejected` | Confirm values above the maximum remain rejected. | Not run locally; Apex tests require org access. |
### Acceptance Criteria
- [x] Guest counts from 1 to 10 are accepted.
- [x] A guest count of 0 is rejected with a clear message.
- [x] Apex tests cover boundary values 0, 1, 10, and 11.
### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Registration submission and update server-side guest validation.
- **Rollback:** Revert this PR.
### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes
> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-20.md`
