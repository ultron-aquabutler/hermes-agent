# Hermes Kanban Workspace Cleanup Safety

> Status: source fix prepared on `fix/kanban-workspace-cleanup-t_3c0ca734-friday`.
> Trigger: `t_3c0ca734`.
> Scope: classify non-board sibling database files during the workspace-sharing check.

## Overview

Workspace cleanup must not remove data still referenced by another nonterminal task. The checker therefore scans the current board and sibling board databases before allowing removal. A legacy directory such as `kanban/boards/_archived/` can contain a file named `kanban.db` without being a board. Querying that file for the required `tasks` table raises SQLite's `no such table` error. Previously, every SQLite error became `unknown`, and `unknown` correctly deferred cleanup. The false positive was that a non-board file could therefore defer unrelated workspace cleanup across boards.

The fix ignores that specific missing-table error only when the sibling directory name is not already the canonical valid board slug. This leaves real, canonical board directories fail-closed. It does not change the reaper cadence, retention policy, workspace containment checks, or cleanup eligibility; it only corrects the sibling-board classification.

## Architecture

`hermes_cli/kanban_db_workspace.py::_workspace_in_use_by_other()` scans the default board DB and files under `kanban/boards/<directory>/kanban.db`. `_other_board_uses_path()` checks whether another task uses the resolved workspace path. A match returns `shared`; a file that cannot be trusted returns `unknown`; no sharer returns `None`.

When a sibling query raises `sqlite3.OperationalError` for a missing `tasks` table, `_is_board_slug_dir()` checks the directory against Hermes' canonical slug normalization. A noncanonical directory (for example `_archived` or `Archive`) cannot be a board created through the board API and is skipped. A canonical directory such as `other` remains a board candidate; its missing table still returns `unknown`. Other SQLite and filesystem errors remain fail-closed.

## Operation

The existing workspace lifecycle invokes this check before removing an eligible scratch workspace. No operator action or new cleanup command is introduced. Do not manually delete a workspace to work around a deferred cleanup; first determine whether a valid board or task still references it.

Regression validation, from the repository root with its test environment available:

```bash
HERMES_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python" scripts/run_tests.sh tests/hermes_cli/test_kanban_gc_retention.py -q
```

The regression fixture covers non-board sibling files with invalid and noncanonical directory names. A separate fixture confirms a canonical board directory with a missing schema still defers cleanup. Existing shared-workspace cases continue to verify that a live task preserves the workspace and that an unknown board listing fails closed.

## Configuration

No new configuration is added. Board storage continues to follow the existing Hermes home and Kanban path resolution. The classification relies on the canonical board-slug rules in `hermes_cli.kanban_db::_normalize_board_slug()`; it does not introduce an allowlist or silently repair board databases.

## Troubleshooting

- `Deferring workspace cleanup ... another non-terminal task`: the path is shared. Keep it and identify the referencing task/board.
- `Deferring workspace cleanup ... cannot tell whether another task still uses ...`: a canonical board DB, the board listing, or a required query is unreadable. Keep the workspace; repair/inspect the real board rather than bypassing the guard.
- A missing `tasks` table under a noncanonical sibling directory is treated as a non-board file. A missing `tasks` table under a canonical board slug remains `unknown` and fail-closed.
- Errors other than the specific missing-table case are not ignored. Disk I/O, permissions, malformed databases, and board-listing failures continue to defer cleanup.

## Dependencies

- Python standard-library `sqlite3` and `pathlib`.
- Hermes Kanban schema and board-slug normalization in `hermes_cli.kanban_db`.
- Workspace lifecycle and containment checks in `hermes_cli.kanban_db_workspace`.

## See Also

- [[Homelab/20-Infrastructure/Services/Hermes-Kanban-Dispatcher-Per-Profile-Cap|Hermes Kanban Dispatcher]] — dispatcher operation and cross-board behavior.
- `hermes_cli/kanban_db_workspace.py::_workspace_in_use_by_other()` — sharing decision.
- `tests/hermes_cli/test_kanban_gc_retention.py` — regression and fail-closed fixtures.

## Change History

- 2026-10-31 — `t_3c0ca734`: narrow the non-board sibling-DB exception while preserving fail-closed behavior for canonical board directories. Source branch: `fix/kanban-workspace-cleanup-t_3c0ca734-friday`.
