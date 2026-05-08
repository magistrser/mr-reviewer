## Review Scope

Use these rules only for issues visible in changed code or deterministic
context included with the task.

Do not raise findings for:

- style or formatting that an autoformatter would handle
- dependency or refactoring concerns outside the diff
- subjective naming preferences when the surrounding module is consistent
- performance speculation without a concrete trigger
- non-functional preferences not encoded in the review resources

If project conventions differ, update the review resources or
`review-profile.md` instead of inventing one-off findings.
