# Solution Report — CLAUDE-20

| Field | Value |
|---|---|
| Ticket | CLAUDE-20 — Registration with zero guests is accepted |
| Type | Bug fix |
| Branch | fix/CLAUDE-20-registration-with-zero-guests-is-accepted |
| Pull Request | Pending |
| Date | 2026-10-07 |
| Agent | GitHub Copilot (CLI) |
| Human Reviewer | Pending |

## 1. Ticket Interpretation
Registration currently accepts zero guests even though only guest counts from 1 through 10 should be valid. Reject zero with the existing guest-count validation message and retain acceptance of both boundary values.

**Acceptance Criteria**
- [x] Guest counts from 1 to 10 are accepted.
- [x] A guest count of 0 is rejected with a clear message.
- [x] Apex tests cover the boundary values 0, 1, 10, and 11.

**Assumptions**
- The shared Apex validator should enforce the same minimum for both new and updated registrations.
- The existing server-side error message is the required user-facing message; no metadata or UI change is needed.

**Classification:** Apex / Validation

## 2. Investigation
**Files Inspected**

| File | Reason |
|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Locate guest validation and registration submission/update paths. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Confirm existing regression and boundary test coverage. |
| `force-app/main/default/objects/Event_Registration__c/fields/Number_of_Guests__c.field-meta.xml` | Check whether field metadata enforces a minimum. |
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.js` | Confirm submission flow and server error display. |
| `force-app/main/default/lwc/eventRegistrationForm/__tests__/eventRegistrationForm.test.js` | Review existing component test coverage. |
| `.github/copilot-instructions.md` | Confirm scope, test, validation, and pipeline requirements. |

**Execution Path**
`eventRegistrationForm` → `EventRegistrationController.submitRegistration` → `validateInputs` → `validateRegistrationDetails` → registration insert. The form displays the Apex error message on failure.

## 3. Root Cause (bug) / Design (feature)
- **Location:** `force-app/main/default/classes/EventRegistrationController.cls` → `validateRegistrationDetails` (line 99)
- **Cause / design:** The minimum comparison used `MIN_GUESTS - 1`, making zero the effective lower bound instead of one.
- **Evidence:** `MIN_GUESTS` is 1 and the conditional compared `numberOfGuests` to `MIN_GUESTS-1`; zero therefore passed validation and reached the insert.
- **Confidence:** High

## 4. Options Considered

| Option | Pros | Cons | Selected |
|---|---|---|---|
| Compare directly against `MIN_GUESTS` in the shared validator | Corrects root cause for both submit and update; keeps current error handling | None material | ✅ |
| Add a field-metadata constraint or duplicate client-side validation | Could add another guard | Requires out-of-scope metadata or duplicates server validation | ❌ |

**Reason for selection:** Correct the off-by-one error in the existing server-side validation without changing scope or duplicating logic.

## 5. Changes Made

| File | Change Summary | Lines +/- |
|---|---|---|
| `force-app/main/default/classes/EventRegistrationController.cls` | Compare guest count with the configured minimum directly. | +1 / -1 |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Associate zero, one, ten, and eleven guest boundary assertions with CLAUDE-20. | +4 / -4 |

## 6. Tests

| Test | Scenario | Result |
|---|---|---|
| `EventRegistrationControllerTest.testZeroGuestsIsRejected` | Zero guests returns the validation message. | Not run locally; Apex tests require org access. |
| `EventRegistrationControllerTest.testMinimumBoundaryGuestsIsAccepted` | Exactly one guest is accepted. | Not run locally; Apex tests require org access. |
| `EventRegistrationControllerTest.testMaxBoundaryGuestsIsAccepted` | Exactly ten guests is accepted. | Not run locally; Apex tests require org access. |
| `EventRegistrationControllerTest.testElevenGuestsIsRejected` | Eleven guests returns the validation message. | Not run locally; Apex tests require org access. |

## 7. Validation Results

| Check | Result |
|---|---|
| Lint | Not available (no `package.json` or configured npm script). |
| Prettier | Not available (no `package.json` or configured npm script). |
| Unit tests (Jest) | Not available (no `package.json` or configured npm script). |
| Apex tests | Not run locally; org access/test commands are prohibited by task constraints. |
| `git diff --check` | Pass |

## 8. Security & Compliance Review
- Secrets introduced: No
- PII introduced: No
- Sharing/CRUD/FLS impact: None
- Restricted paths modified: No

## 9. Risks and Limitations
- Apex unit tests were not executable locally under the task constraints and must be run by the pipeline or reviewer.

## 10. Rollback Plan
Revert the CLAUDE-20 change. No data or metadata migration involved.

## 11. Next Steps for Reviewer
- Review the diff.
- Run the Apex tests in the approved validation environment.
- Approve or request changes.
