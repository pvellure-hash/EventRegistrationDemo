h3. 🤖 AI-Assisted Change — Ready for Review
*Status:* Draft PR pending pipeline creation — awaiting human review
*Branch:* fix/CLAUDE-21-recent-registrations-table-shows-the-confirmation
*Pull Request:* <PR link>
*Root Cause / Design:*
The Recent Registrations table bound both the Confirmation # and Name columns to `reg.Name`. The Name cell now uses the attendee-name field already returned by Apex.
*Change Summary:*
Corrected the Name column while preserving the confirmation number column, and added an LWC regression test for both displayed values.
*Files Changed:* 5 files
*Tests Added/Updated:* 1
*Validation:*
* Lint: Not available (no package manifest or configured npm script)
* Unit tests: Not run locally (no package manifest or local Jest executable)
*Solution Report:* docs/ai-reports/CLAUDE-21.md (attached)
_No merge or deployment has been performed._
