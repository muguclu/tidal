# ADR-0005: Trunk-based development
- Status: Proposed · Related: SPEC-08 §1
## Context
Small team (one human + agents), continuous delivery to a single dev environment.
## Options
1. Trunk-based with short-lived branches, squash merge, release tags.
2. GitFlow (develop/release/hotfix branches).
## Decision
Option 1. Fewer long-lived branches → fewer conflicts between parallel agent worktrees.
## Consequences
Requires strong CI gates and feature flags. GitFlow would fit a regulated release train with multiple
supported versions — explained in the README for interview discussion.
