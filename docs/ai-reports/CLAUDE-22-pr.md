## [CLAUDE-22] Registration with the maximum of 10 guests is rejected
**Jira:** Not provided
**Type:** Bug fix
**Status:** Draft — awaiting human review
### Problem
The registration form rejects exactly 10 guests, although 10 is the maximum allowed value. The existing server-side error prevents a valid registration from being saved.
### Root Cause / Design
`EventRegistrationController.validateRegistrationDetails` rejected counts greater than or equal to `MAX_GUESTS`, making the configured upper boundary exclusive.
### Solution
Changed the validation to reject only counts greater than the maximum. Strengthened the max-boundary Apex test to confirm that 10 is persisted; existing tests continue to cover 1, 0, negative values, and 11.
### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Permit a guest count equal to `MAX_GUESTS`. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Assert the saved count is 10 in the maximum-boundary test. |
| `docs/ai-reports/CLAUDE-22.md` | Solution report. |
| `docs/ai-reports/CLAUDE-22-pr.md` | PR description. |
| `docs/ai-reports/CLAUDE-22-jira.md` | Jira comment draft. |
### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` | Confirm 10 is accepted and persisted. | Not run locally; Apex tests require approved org validation. |
| `EventRegistrationControllerTest.testElevenGuestsIsRejected` | Confirm 11 remains rejected. | Existing test retained; not run locally. |
| `EventRegistrationControllerTest.testMinimumBoundaryGuestsIsAccepted` | Confirm 1 remains accepted. | Existing test retained; not run locally. |
| `EventRegistrationControllerTest.testZeroGuestsIsRejected` | Confirm 0 remains rejected. | Existing test retained; not run locally. |
### Acceptance Criteria
- [x] 10 guests is accepted.
- [x] 11 or more guests is still rejected.
- [x] 1 guest is still accepted, and 0 or fewer is still rejected.
- [x] `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` verifies the saved maximum.
- [x] Existing tests are retained.
### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Apex validation for registration submission and update.
- **Rollback:** Revert this PR.
### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes
> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-22.md`
