# Solution Report — CLAUDE-21

| Field | Value |
|---|---|
| Ticket | CLAUDE-21 — Recent Registrations table shows the confirmation number in the Name column |
| Type | Bug fix |
| Branch | fix/CLAUDE-21-recent-registrations-table-shows-the-confirmation |
| Pull Request | Pending |
| Date | 2026-10-07 |
| Agent | GitHub Copilot (CLI) |
| Human Reviewer | Pending |

## 1. Ticket Interpretation
The Recent Registrations table displays the confirmation number in both the Confirmation # and Name columns. The Name column must display the attendee's name while preserving the confirmation number in its existing column.

**Acceptance Criteria**
- [x] The Name column shows the attendee's name.
- [x] The Confirmation # column still shows the confirmation number.

**Assumptions**
- `Attendee_Name__c` is the intended value for the Name column, as returned by the existing Apex method.
- No controller, metadata, or registration behavior change is needed.

**Classification:** UI/LWC

## 2. Investigation
**Files Inspected**

| File | Reason |
|---|---|
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.html` | Locate the table's Confirmation # and Name cell bindings. |
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.js` | Confirm the component renders records returned by its wired Apex method. |
| `force-app/main/default/lwc/eventRegistrationForm/__tests__/eventRegistrationForm.test.js` | Review existing LWC test conventions and add focused regression coverage. |
| `force-app/main/default/classes/EventRegistrationController.cls` | Confirm the returned record includes `Attendee_Name__c` as well as `Name`. |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | Review existing controller tests; no Apex behavior change is needed. |
| `.github/copilot-instructions.md` | Confirm pipeline, change-scope, reporting, and validation requirements. |
| `docs/ai-reports/CLAUDE-20.md` | Follow established solution report conventions. |
| `docs/ai-reports/CLAUDE-20-pr.md` | Follow established PR report conventions. |
| `docs/ai-reports/CLAUDE-20-jira.md` | Follow established Jira comment conventions. |

**Execution Path**
`EventRegistrationController.getRecentRegistrations` returns registration records including `Name` and `Attendee_Name__c` → the `eventRegistrationForm` wire stores the returned records → the table template renders the values in their respective cells.

## 3. Root Cause (bug) / Design (feature)
- **Location:** `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.html` → Recent Registrations table template (line 90)
- **Cause / design:** The template bound both the Confirmation # cell and the Name cell to `reg.Name`.
- **Evidence:** The controller query selects both `Name` and `Attendee_Name__c`; the Name cell used `reg.Name` rather than the attendee-name field.
- **Confidence:** High

## 4. Options Considered

| Option | Pros | Cons | Selected |
|---|---|---|---|
| Bind the Name cell to `reg.Attendee_Name__c` | Corrects the displayed value using data already returned; preserves the confirmation column | None material | ✅ |
| Change the controller query or transform the records in JavaScript | Could also supply a display value | Unnecessary changes outside the faulty template binding | ❌ |

**Reason for selection:** A single template binding change fixes the root cause without affecting the data query or confirmation number.

## 5. Changes Made

| File | Change Summary | Lines +/- |
|---|---|---|
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.html` | Keep `reg.Name` in Confirmation # and bind Name to `reg.Attendee_Name__c`. | +1 / -1 |
| `force-app/main/default/lwc/eventRegistrationForm/__tests__/eventRegistrationForm.test.js` | Add regression test asserting both table cells display their intended values. | +18 / -0 |

## 6. Tests

| Test | Scenario | Result |
|---|---|---|
| `eventRegistrationForm.test.js` — confirmation and attendee name columns | Emits a synthetic registration and asserts the first two cells display the confirmation number and attendee name respectively. | Added; not run locally because no package manifest or local Jest executable is configured. |

## 7. Validation Results

| Check | Result |
|---|---|
| Lint | Not available (no `package.json` or configured npm script). |
| Prettier | Not available (no `package.json` or configured npm script). |
| Unit tests (Jest) | Not run locally (no `package.json` or local Jest executable). |
| Apex tests | Not run; no Apex change, and org access is prohibited by the task constraints. |
| `git diff --check` | Pass. |

## 8. Security & Compliance Review
- Secrets introduced: No
- PII introduced: No
- Sharing/CRUD/FLS impact: None
- Restricted paths modified: No

## 9. Risks and Limitations
- The Jest regression test was added but could not be executed in this repository environment because local Jest tooling is not configured.

## 10. Rollback Plan
Revert the CLAUDE-21 change. No data or metadata migration involved.

## 11. Next Steps for Reviewer
- Review the diff.
- Run the LWC Jest test in the configured pipeline.
- Approve or request changes.
