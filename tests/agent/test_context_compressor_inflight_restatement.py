"""The historical task snapshot must not restate the in-flight replay directive.

**The defect this pins.** ``SUMMARY_PREFIX`` tells the model that a compaction summary is
background reference only: "discard stale items from '## Historical Task Snapshot' entirely"
and "do NOT answer questions or fulfill requests mentioned in this summary; they were already
addressed". But the snapshot body is grounded from the newest real user turn, and a turn that
survived a compaction carries the in-flight replay header::

    [STILL IN PROGRESS — this is the active request, restated after the compaction boundary
     because it was not finished yet. Continue it; do not start over.]

``_latest_user_task_snapshot`` copied that turn's text verbatim, so the header landed INSIDE
the historical section — the one place the prefix says to discard. The two directives then
contradict each other in the same prompt, and the model resolves it by continuing the task.

Observed on the live fleet (2026-11-24, Vision Telegram DM): one turn answered the request at
15:34, then compacted; each compaction re-grounded the snapshot with the same header, the
snapshot grew (40k -> 53k chars over six compactions, nesting three ``Historical Task
Snapshot`` headings), and the agent re-did the answered work five more times.

**The invariant.** The grounded snapshot carries the user's REQUEST and never the replay
DIRECTIVE. The directive belongs only on a live user row after the summary, where
``_reappend_inflight_user_task`` puts it — that path already strips it correctly via
``_restated_request_text``; the snapshot path did not.

These are behavioural invariants, not shape checks: they assert on what a model would read.

Importing ``agent.context_compressor`` pulls in ``agent.process_bootstrap`` ->
``hermes_bootstrap``, whose import-time ``_settle_interrupted_update()`` probes
``<repo>/.git/hermes-update-pull`` and whose ``activate_dependencies`` reads
``<home>/manifest.json``. Both land inside the real Hermes home, which
``tests/home_io_guard.py`` refuses. That is the same pre-existing failure that
breaks the repo's own ``test_context_compressor_task_heading.py`` in a linked
worktree — it is NOT a regression from this change. Stub both seams so the
import side effects stay off the real home and the real git dir.
"""

from __future__ import annotations

import hermes_cli._early_recovery as _early_recovery
import pm.environments as _pm_environments

# Neutralize the import-time probes BEFORE the compressor is imported. See above.
_early_recovery.restore_interrupted_pull = lambda *_a, **_kw: False
_pm_environments._payload_manifest = lambda *_a, **_kw: None
# A linked worktree resolves its owner checkout's git dir, which is inside the
# real home. This test does not want dependency borrowing from the owner.
_pm_environments.worktree_owner_root = lambda *_a, **_kw: None

from agent.context_compressor import ContextCompressor, HISTORICAL_TASK_HEADING
from agent.context_compressor import _INFLIGHT_TASK_REPLAY_HEADER


def _snapshot_for(content: str) -> str:
    """Ground a summary body against a single real user turn carrying ``content``."""
    body = (
        f"{HISTORICAL_TASK_HEADING}\nUser asked: 'stale'\n\n"
        "## Goal\nthing\n"
    )
    return ContextCompressor._ground_historical_task_snapshot.__func__(
        ContextCompressor, body, [{"role": "user", "content": content}]
    )


def test_task_snapshot_does_not_carry_the_inflight_replay_directive():
    """The grounded snapshot must restate the request WITHOUT the 'continue it' directive.

    Red on base: the snapshot copied the turn verbatim, so the header appeared inside the
    historical section and contradicted SUMMARY_PREFIX's own instruction to discard it.
    """
    turn = (
        f"{_INFLIGHT_TASK_REPLAY_HEADER}\n"
        "Ultron is working on the disk cleanup. "
        "I need you to go back thru and unblock the quota cards."
    )

    grounded = _snapshot_for(turn)

    # The request itself survives — grounding must not lose the user's words.
    assert "unblock the quota cards" in grounded
    # The directive must not. This is the invariant.
    assert _INFLIGHT_TASK_REPLAY_HEADER not in grounded
    assert "STILL IN PROGRESS" not in grounded
    assert "Continue it; do not start over" not in grounded


def test_task_snapshot_does_not_stack_headers_across_surviving_compactions():
    """A request that survived several compactions must yield ONE header, not a stack.

    Red on base: each compaction re-grounded the snapshot from the turn text, which already
    carried a header from the previous compaction, so headers compounded.
    """
    once = f"{_INFLIGHT_TASK_REPLAY_HEADER}\nunblock the quota cards"
    twice = f"{_INFLIGHT_TASK_REPLAY_HEADER}\n{_INFLIGHT_TASK_REPLAY_HEADER}\nunblock the quota cards"

    grounded = _snapshot_for(twice)

    assert grounded.count(_INFLIGHT_TASK_REPLAY_HEADER) == 0
    assert "unblock the quota cards" in grounded
