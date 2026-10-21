---
description: Run code-reviewer (and security-reviewer if needed) on the current branch
---
Determine the task ID from the current branch name. Spawn `code-reviewer` on `main...HEAD`.
If the diff touches infra/, deploy/, .github/, src/tidal/api/auth.py, or any Dockerfile, also spawn
`security-reviewer` in the same message. Merge both outputs into one PR review comment.
