h3. 🤖 AI-Assisted Change — Ready for Review
*Status:* Draft PR created — awaiting human review
*Branch:* fix/CLAUDE-20-registration-with-zero-guests-is-accepted
*Pull Request:* <PR link>
*Root Cause / Design:*
`EventRegistrationController.validateRegistrationDetails` used `MIN_GUESTS - 1` as its lower bound, allowing zero. The comparison now uses `MIN_GUESTS` directly.
*Change Summary:*
Guest counts below one are rejected by the shared server-side validator. Apex boundary assertions cover 0, 1, 10, and 11.
*Files Changed:* 5 files
*Tests Added/Updated:* 1
*Validation:*
* Lint: Not available (no npm package manifest/script)
* Unit tests: Not run locally (Apex tests require org access)
*Solution Report:* docs/ai-reports/CLAUDE-20.md (attached)
_No merge or deployment has been performed._
