# Turn todo implementation plan

Goal: Track the main Agent's current-request checklist and prevent ending with forgotten pending items.

Scope: In-memory only, no new Agent, persistence, UI or cross-turn task changes. Preserve existing working-tree changes.

- [x] Add offline runtime tests for checklist updates, terminal guards, model visibility and per-run isolation; confirm failures first.
- [x] Add a bounded checklist tool, include fresh state in each main decision, and guard structured/plain endings.
- [x] Describe checklist use and candidate-versus-commit semantics in the main prompt.
- [x] Run focused and full automated tests, inspect the incremental diff.

The model identifies items and supplies result/reason summaries. This checklist does not independently prove semantic completeness or replace server commit receipts. On the final model call, remaining pending items are safely recorded as unfinished rather than silently dropped.
