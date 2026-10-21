---
description: Plan and launch a parallel wave of tasks (orchestrator only)
argument-hint: <wave number, e.g. 1>
---
You are the orchestrator. Wave: $ARGUMENTS.
1. From docs/PARALLELISM.md §2 and docs/tasks/TASKS.md, list the ready tasks of this wave per lane.
2. Verify the Owns columns of tasks you would run concurrently are disjoint. If not, sequence them.
3. Propose at most 3 implementing tasks to run now, one per lane, with their agent and worktree name.
4. After I confirm, spawn each lane's subagent in the same message (parallel), each in its own
   worktree (`isolation: "worktree"`), passing the task brief.
5. When each finishes, spawn `code-reviewer` (and `security-reviewer` for infra/auth/CI/Docker diffs).
6. Send blocking findings back to the implementing agent (max 2 rounds), then summarise for me:
   per task — verdict, files, tests, open questions, suggested PR title.
Never merge, push to main, or apply infrastructure.
