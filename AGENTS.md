# Agent Instructions

These rules apply to Claude Code, OpenCode, Codex and any other coding agent in this repository.

## Planning

- Always use the `planning-phases` skill by default to plan, execute and review work.
- If the skill cannot be loaded, stop and tell the user. Do not continue with a substitute process.

## One active plan

- Only one active plan may exist at a time: one `docs/PLAN_*.md` file.
- Plans in `docs/done/` are finished or archived. Plans in `docs/todo/` are not active.
- Before creating a plan, check for an existing active plan and apply the rules below.

## Source of truth

- All work must follow the current active plan.
- If anything conflicts with it, the active plan's setting wins. This includes older plans, migrated
  phases, README text, other docs, existing code behavior and earlier decisions.
- Within the active plan, its newest explicit owner decision and current design win over older or
  migrated text.
- When a conflict is found, follow the current setting and mark the older requirement as superseded
  in the active plan. Never reintroduce superseded behavior. The user's latest explicit instruction
  can change the plan, and the plan must then be updated.
- This precedence never weakens security rules. Provider terms stay in force unless the plan records
  an explicit, informed owner decision about that provider, and agents must not hide that risk.

## Plan consistency

Always check the whole active plan for contradictions when creating or changing it, and before
executing or reviewing a phase. Check goals, non-goals, constraints, strategy, dependencies, scope,
steps, verification, security checks and especially **every acceptance criterion**.

- Every acceptance criterion must be achievable and must agree with the current owner decisions.
- Every required behavior must belong to a phase, and no phase may depend on later work.
- Fix a contradiction by applying the source-of-truth rules and updating the plan. If the correct
  decision is unclear, stop and ask the owner.
- Never mark a criterion passed unless it was verified. An owner-waived criterion must say so.

## Completing a plan

When every phase of the active plan is `Done — Passed Review` or `Reverted`, automatically move it
to `docs/done/` in the same task, then update any references to its old path, including
`deploy/release.py`.

## Replacing an unfinished plan

When a new plan replaces an unfinished active plan:

1. Migrate every phase that is not `Done — Passed Review` or `Reverted` to the end of the new plan.
   This includes `Not Started`, `In Progress`, `Partial`, `Blocked`, `Ready for Review` and
   `Review Failed` phases.
2. Preserve each migrated phase's status, blockers and evidence. Keep its criteria unless they
   conflict with the new plan; then mark them superseded and apply the new plan's setting.
3. Update its dependencies so they point to the correct plan and phase.
4. Mark it in the old plan as migrated, with the new plan's path.
5. Move the old plan to `docs/done/` and update any references to its old path.

Never mark a phase complete just to move a plan. Moving plans does not authorize a commit or push.
