# CrawlScope Project Instructions

## Development Workflow
- I am a beginner learning web crawling and software development.
- Implement requested features incrementally.
- Keep code beginner-friendly, readable, and maintainable.
- Avoid unnecessary refactoring or adding dependencies.
- Explain code concepts only when I explicitly ask for explanations. Otherwise, focus on implementation.

## Git and GitHub Workflow
After completing each requested feature or code modification:

1. Review the changes using `git diff`.
2. Run relevant tests or checks.
3. If tests fail, investigate and fix them. If they cannot be fixed, stop and report the issue.
4. Check `git status` before staging.
5. Stage only relevant project files. Never stage `.venv`, secrets, API keys, `.env` files, or unrelated files.
6. Create a descriptive commit message using conventional commit style, such as `feat:`, `fix:`, or `docs:`.
7. Push the commit to `origin/main`.
8. Verify that the push succeeded.
9. Report a concise summary of changes, tests, commit hash, and push status.

## Safety
- Never force push.
- Never use destructive Git commands such as `git reset --hard` without my explicit approval.
- Never overwrite unrelated user changes.
- Never push secrets, credentials, or sensitive data.
- If there is a merge conflict, authentication issue, or any other problem that requires my intervention, stop and report it instead of proceeding.
