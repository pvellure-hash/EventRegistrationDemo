h3. 🤖 AI-Assisted Change — Ready for Review
*Status:* Draft PR pending pipeline validation — awaiting human review
*Branch:* fix/CLAUDE-19-guest-count-above-10-is-accepted
*Pull Request:* <PR link>
*Root Cause / Design:*
The controller compared the guest count against `MAX_GUESTS + 1`, which let 11 guests through although the maximum is 10. The shared validation now rejects values greater than `MAX_GUESTS`.
*Change Summary:*
Updated the controller bound and strengthened the Apex regression test to reject exactly 11 with the expected message. Existing tests cover the 0, 1, and 10 boundaries.
*Files Changed:* 5 files
*Tests Added/Updated:* 1
*Validation:*
* Lint: Not available (no package.json)
* Unit tests: Apex tests not run locally; no Jest setup
*Solution Report:* docs/ai-reports/CLAUDE-19.md (attached)
_No merge or deployment has been performed._
