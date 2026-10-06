## [CLAUDE-18] Allow editing an existing event registration
**Jira:** <Jira link>
**Type:** Feature
**Status:** Draft — awaiting human review

### Problem
Existing event registrations can be listed but not edited. Users need to update phone, number of guests, and event date without changing attendee name or email.

### Root Cause / Design
The existing `EventRegistrationController` only supported insertion and read-only retrieval, and the existing LWC registration list had no edit action. The implementation extends these existing surfaces.

### Solution
Added a secured `updateRegistration` Apex method that reuses guest/date validation, checks object and field update permissions, and queries the target record in user mode. Added per-row Edit actions to the LWC; edit pre-fills the allowed fields, disables name and email, updates through Apex, and refreshes the list with visible success/error feedback.

### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Add permission-checked registration update and shared validation. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Add update success, invalid-input, and missing-record tests. |
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.js` | Implement edit state and Apex update flow. |
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.html` | Add edit controls, read-only fields, and row actions. |
| `force-app/main/default/lwc/eventRegistrationForm/__tests__/eventRegistrationForm.test.js` | Test prefilled edit state and read-only fields. |
| `docs/ai-reports/CLAUDE-18.md` | Add the solution report. |
| `docs/ai-reports/CLAUDE-18-pr.md` | Add this PR description. |
| `docs/ai-reports/CLAUDE-18-jira.md` | Add Jira comment draft. |

### Tests
| Test | Purpose | Result |
|---|---|---|
| `EventRegistrationControllerTest.testUpdateRegistrationAndValidation` | Verify only allowed fields update and invalid counts, dates, and missing records are rejected. | Added; not run locally |
| `eventRegistrationForm` Jest test | Verify edit prefill and read-only attendee/email inputs. | Added; not run locally |

### Acceptance Criteria
- [x] Update method validates guests and future event date before saving.
- [x] Update permission checks are enforced.
- [x] LWC exposes Edit, prefills editable fields, saves, and refreshes the list.
- [x] Attendee name and email are not editable.
- [x] Success and error feedback is shown after save attempts.
- [x] Existing create path remains available.

### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Event registration controller and form/list LWC.
- **Rollback:** Revert this PR.

### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes

> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-18.md`
