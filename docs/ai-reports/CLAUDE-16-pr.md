## [CLAUDE-16] Guest count of zero or negative is accepted

**Jira:** <Jira link>
**Type:** Bug fix
**Status:** Draft — awaiting human review

### Problem
The server-side registration controller accepted guest counts of zero or less, despite the intended range being 1–10. This allowed invalid registrations to be created through direct controller calls.

### Root Cause
`EventRegistrationController.validateInputs` rejected null and above-maximum guest counts but omitted the lower-bound check against `MIN_GUESTS`.

### Solution
Added the missing lower-bound check to server-side validation. Strengthened regression tests to assert the exact rejection message for zero and negative values, and added coverage for accepting the minimum valid count.

### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Reject guest counts below `MIN_GUESTS`. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Assert exact messages for invalid low counts and verify acceptance at 1. |
| `docs/ai-reports/CLAUDE-16.md` | Add the solution report. |
| `docs/ai-reports/CLAUDE-16-pr.md` | Add this PR description. |
| `docs/ai-reports/CLAUDE-16-jira.md` | Add the Jira comment draft. |

### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testZeroGuestsIsRejected` | Reject zero with the expected message. | Not run locally |
| `EventRegistrationControllerTest.testNegativeGuestsIsRejected` | Reject negative values with the expected message. | Not run locally |
| `EventRegistrationControllerTest.testMinimumBoundaryGuestsIsAccepted` | Accept exactly one guest. | Not run locally |
| Existing upper-bound rejection and acceptance tests | Preserve behavior at and above the maximum. | Not run locally |

### Acceptance Criteria
- [x] Guest counts below 1 are rejected with `Number of guests must be between 1 and 10.`
- [x] Guest counts above 10 remain rejected.
- [x] Guest counts from 1 through 10 remain valid.
- [x] Zero and negative regression tests assert the required message.

### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Event registration controller validation and its Apex tests.
- **Rollback:** Revert this PR.

### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes

> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-16.md`
