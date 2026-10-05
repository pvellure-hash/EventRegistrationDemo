h3. 🤖 AI-Assisted Fix — Ready for Review

*Status:* Draft PR pending pipeline validation
*Branch:* fix/CLAUDE-16-guest-count-of-zero-or-negative
*Pull Request:* <PR link>

*Root Cause:*
`EventRegistrationController.validateInputs` rejected null or above-maximum guest counts but did not enforce the minimum. Zero and negative counts therefore passed validation.

*Fix Summary:*
Added the missing lower-bound check using `MIN_GUESTS`. Updated regression assertions for zero and negative counts to verify the exact error message, and added a test for accepting the minimum count of 1.

*Files Changed:* 5 files
*Tests Added/Updated:* 3

*Validation:*
* Lint: Not available (no package.json)
* Unit tests: Apex tests not run locally; run in the approved pipeline

*Solution Report:* docs/ai-reports/CLAUDE-16.md (attached)

_No merge or deployment has been performed._
