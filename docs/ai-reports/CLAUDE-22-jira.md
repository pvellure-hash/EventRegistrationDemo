h3. 🤖 AI-Assisted Change — Ready for Review
*Status:* Draft PR created — awaiting human review
*Branch:* fix/CLAUDE-22-registration-with-the-maximum-of-10
*Pull Request:* <PR link>
*Root Cause / Design:*
The shared Apex guest-count validator rejected values greater than or equal to the maximum, incorrectly rejecting exactly 10.
*Change Summary:*
Changed the upper-bound check to reject only values above 10. Strengthened the maximum-boundary Apex test to verify the saved value while retaining tests for lower and upper invalid values.
*Files Changed:* 5 files
*Tests Added/Updated:* 1
*Validation:*
* Lint: Not available (no `package.json`)
* Unit tests: Apex tests not run locally; org access is prohibited by the task constraints. Jest is not available (no `package.json`).
*Solution Report:* docs/ai-reports/CLAUDE-22.md (attached)
_No merge or deployment has been performed._
