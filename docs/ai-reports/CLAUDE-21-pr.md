## [CLAUDE-21] Recent Registrations table shows the confirmation number in the Name column
**Jira:** Not provided
**Type:** Bug fix
**Status:** Draft — awaiting human review
### Problem
The Recent Registrations table displays the confirmation number in both the Confirmation # and Name columns. The Name column should show the attendee's name.
### Root Cause / Design
The table template in `eventRegistrationForm.html` bound the Name cell to `reg.Name`, duplicating the Confirmation # cell instead of displaying `reg.Attendee_Name__c`.
### Solution
Changed the Name cell to render `reg.Attendee_Name__c`, leaving the Confirmation # cell bound to `reg.Name`. Added an LWC regression test asserting that both columns show the correct values.
### Files Changed
| File | Change |
|---|---|
| `force-app/main/default/lwc/eventRegistrationForm/eventRegistrationForm.html` | Bind the Name cell to the attendee name field. |
| `force-app/main/default/lwc/eventRegistrationForm/__tests__/eventRegistrationForm.test.js` | Assert confirmation number and attendee name appear in their respective cells. |
| `docs/ai-reports/CLAUDE-21.md` | Solution report. |
| `docs/ai-reports/CLAUDE-21-pr.md` | PR description. |
| `docs/ai-reports/CLAUDE-21-jira.md` | Jira comment draft. |
### Tests
| Test | Purpose | Result |
|---|---|---|
| `eventRegistrationForm.test.js` — confirmation and attendee name columns | Verify the two displayed values are distinct and correctly bound. | Added; not run locally because Jest is not configured. |
### Acceptance Criteria
- [x] The Name column shows the attendee's name.
- [x] The Confirmation # column still shows the confirmation number.
### Risk Assessment
- **Risk level:** Low
- **Impacted areas:** Recent Registrations table display only.
- **Rollback:** Revert this PR.
### Reviewer Checklist
- [ ] Logic is correct
- [ ] Tests are meaningful
- [ ] No security regressions
- [ ] No unrelated changes
> AI-assisted change. Full Solution Report: `docs/ai-reports/CLAUDE-21.md`
