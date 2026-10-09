# Solution Report — CLAUDE-22

| Field | Value |
|---|---|
| Ticket | CLAUDE-22 — Registration with the maximum of 10 guests is rejected |
| Type | Bug fix |
| Branch | fix/CLAUDE-22-registration-with-the-maximum-of-10 |
| Pull Request | Pending |
| Date | 2026-10-09 |
| Agent | GitHub Copilot (CLI) |
| Human Reviewer | Pending |

## 1. Ticket Interpretation
New registrations with exactly 10 guests are incorrectly rejected even though 10 is the configured maximum. Correct the server-side boundary check while continuing to accept 1 and reject 0 or fewer and 11 or more.

**Acceptance Criteria**
- [x] 10 guests is accepted.
- [x] 11 or more guests is rejected.
- [x] 1 guest is accepted, and 0 or fewer is rejected.
- [x] `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` verifies the maximum value is saved.
- [x] All other existing tests are preserved.

**Assumptions**
- The shared server-side guest-count validator should enforce the same inclusive 1-to-10 range for both submission and update.
- The existing validation message remains appropriate; no UI or metadata changes are required.

**Classification:** Apex / Validation

## 2. Investigation
**Files Inspected**

| File | Reason |
|---|---|
| `.github/copilot-instructions.md` | Confirm pipeline restrictions, scope, and validation requirements. |
| `force-app/main/default/classes/EventRegistrationController.cls` | Locate shared guest-count validation and its submission/update callers. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Check existing guest-count boundary tests and update coverage. |
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.js` | Confirm form submission delegates validation to Apex and displays server errors. |
| `force-app/main/default/objects/Event_Registration__c/fields/Number_of_Guests__c.field-meta.xml` | Verify the field's numeric metadata does not define the 1-to-10 application boundary. |

**Execution Path**
`eventRegistrationForm.handleSubmit` → `EventRegistrationController.submitRegistration` → `validateInputs` → `validateRegistrationDetails` → insert. Updates also call `validateRegistrationDetails` via `updateRegistration`.

## 3. Root Cause (bug) / Design (feature)
- **Location:** `force-app/main/default/classes/EventRegistrationController.cls` → `validateRegistrationDetails` (guest-count conditional)
- **Cause / design:** The upper-bound condition used `numberOfGuests >= MAX_GUESTS`, rejecting the configured maximum itself.
- **Evidence:** `MAX_GUESTS` is 10, while the conditional rejected any value greater than or equal to 10. The existing test expected an ID for exactly 10, but did not verify the saved field value.
- **Confidence:** High

## 4. Options Considered

| Option | Pros | Cons | Selected |
|---|---|---|---|
| Change the shared upper-bound comparison to `> MAX_GUESTS` and assert the saved maximum in the existing test | Corrects the root cause for submission and update, preserves the configured limit, and adds focused persisted-value coverage | None material | ✅ |
| Add a separate check in the form or field metadata | Could add another guard | Duplicates server-side validation or changes out-of-scope metadata without fixing the shared validator | ❌ |

**Reason for selection:** An inclusive upper bound in the existing shared validator is the smallest complete correction and keeps new and updated registrations consistent.

## 5. Changes Made

| File | Change Summary | Lines +/- |
|---|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Allow the maximum by rejecting only counts greater than `MAX_GUESTS`. | +1 / -1 |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Verify exactly 10 is persisted by the max-boundary regression test. | +3 / -0 |

## 6. Tests

| Test | Scenario | Result |
|---|---|---|
| `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` | Submit 10 guests and assert the saved guest count is 10. | Added; not run locally because Salesforce org access is prohibited by the task constraints. |
| `EventRegistrationControllerTest.testElevenGuestsIsRejected` | Confirm 11 is rejected with the guest-count validation message. | Existing test retained; not run locally. |
| `EventRegistrationControllerTest.testMinimumBoundaryGuestsIsAccepted` | Confirm 1 remains accepted. | Existing test retained; not run locally. |
| `EventRegistrationControllerTest.testZeroGuestsIsRejected` | Confirm 0 remains rejected. | Existing test retained; not run locally. |
| `EventRegistrationControllerTest.testNegativeGuestsIsRejected` | Confirm a negative value remains rejected. | Existing test retained; not run locally. |
| `EventRegistrationControllerTest.testUpdateRegistrationAndValidation` | Confirm update accepts 10 and rejects 11 and invalid values. | Existing test retained; not run locally. |

## 7. Validation Results

| Check | Result |
|---|---|
| Lint | Not available; no `package.json` or configured npm script. |
| Prettier | Not available; no `package.json` or configured npm script. |
| Unit tests (Jest) | Not available; no `package.json` or configured npm script. |
| Apex tests | Not run locally; Salesforce org access is prohibited by the task constraints. |
| `git diff --check` | Pass. |

## 8. Security & Compliance Review
- Secrets introduced: No
- PII introduced: No
- Sharing/CRUD/FLS impact: None
- Restricted paths modified: No

## 9. Risks and Limitations
- The corrected validator is shared by create and update paths; both now permit exactly 10.
- Apex tests were not run locally and must be executed by the approved pipeline or reviewer.

## 10. Rollback Plan
Revert the CLAUDE-22 change. No data or metadata migration involved.

## 11. Next Steps for Reviewer
- Review the diff.
- Run the Apex tests in the approved validation environment.
- Approve or request changes.
