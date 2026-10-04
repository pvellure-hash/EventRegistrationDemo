h3. 🤖 AI-Assisted Fix — Ready for Review

*Status:* Draft PR created — awaiting human review
*Branch:* fix/CLAUDE-14-guest-count-validation-allows-more-than
*Pull Request:* <PR link>

*Root Cause:*
`EventRegistrationController.validateInputs()` checked for guest counts below 1 but did not reject counts above 10, allowing values such as 500.

*Fix Summary:*
Added the missing server-side upper-bound validation, set the LWC input maximum to 10, and updated the regression test to use 500 as the overflow value.

*Files Changed:* 7 files
*Tests Added/Updated:* 1

*Validation:*
* Lint: Not run
* Unit tests: Apex tests not run locally (requires org)

*Solution Report:* docs/ai-reports/CLAUDE-14.md (attached)

_No merge or deployment has been performed._
