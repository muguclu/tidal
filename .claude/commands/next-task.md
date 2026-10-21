---
description: Pick the next ready task from TASKS.md and prepare its brief
---
Read docs/tasks/TASKS.md and docs/PARALLELISM.md.
List tasks with status `todo` whose Deps are all `done` and whose phase gate is open.
For the top candidate (lowest ID unless $ARGUMENTS names one), produce the task brief from
PARALLELISM.md §5, filling every field from TASKS.md and the referenced spec/contract sections.
Do not start implementing. Ask me to confirm or pick another.
