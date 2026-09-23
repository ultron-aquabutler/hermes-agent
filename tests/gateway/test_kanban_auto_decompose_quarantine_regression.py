"""Regression test for t_9f3c5d4a — `auto_decompose_tick` must not re-promote
hub_escalation=1 cards.

Original symptom (t_f59a0d0d, 12-day worker loop, 17 runs): the dispatcher's
auto_decompose_tick listed ALL triage rows via
``hermes_cli.kanban_decompose.list_triage_ids``, and that listing returned
quarantined cards. The auto-decomposer then re-specified them, they
re-promoted, workers respawned, blocked again, and the loop armed every
~10s.

Acceptance criteria from t_9f3c5d4a:
  - A `triage` task with `hub_escalation=1` is never picked up by
    `auto_decompose_tick`.
  - Fresh `triage` tasks (no `hub_escalation`) still decompose as today.
  - Backlog test: re-create the conditions on a test card (block with
    recurrences >= 2, then run auto_decompose_tick); confirm
    `hub_escalation=1` card stays in `triage`.

The pre-existing unit coverage in ``tests/hermes_cli/test_kanban_decompose.py``
already proves the ``list_triage_ids`` filter alone. This file proves the
*dispatcher tick* honors that filter end-to-end against the live SQLite DB.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as kwd


def _dispatcher(board_slug: str) -> kwd._KanbanDispatcher:
    """Build a dispatcher backed by the real kanban_db module.

    `_board_slugs` calls `kb.list_boards` and falls back to `kb.read_board_metadata`;
    the real module exposes both. The dispatcher never mutates kb itself; only
    the auto_decompose_tick call path matters here, and that's been patched
    via `hermes_cli.kanban_decompose` (sys.modules + hermes_cli attrs).
    """
    from hermes_cli import kanban_db as real_kb

    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(real_kb, settings)


@pytest.fixture
def board(monkeypatch, tmp_path):
    """Materialize a fresh board under a fresh HERMES_HOME."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db as kb

    board_slug = "t9f3-test-board"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("HERMES_KANBAN_BOARDS=default\n", encoding="utf-8")
    prev_board = os.environ.get("HERMES_KANBAN_BOARD")
    os.environ["HERMES_KANBAN_BOARD"] = board_slug
    try:
        # Touch the DB so the board registers in metadata.
        with kbc.connect_closing() as conn:
            conn.execute("SELECT 1").fetchone()
        # Ensure board metadata exists.
        kb.read_board_metadata(board_slug)
        yield board_slug
    finally:
        if prev_board is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_board


def _quarantine_card(board_slug: str, title: str) -> str:
    """Park a fresh card in `triage` with `hub_escalation=1` via block_task."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    prev_board = os.environ.get("HERMES_KANBAN_BOARD")
    os.environ["HERMES_KANBAN_BOARD"] = board_slug
    try:
        with kbc.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title=title,
                body="regression test card — auto-archived at teardown",
                assignee="friday",
                initial_status="blocked",
            )
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            conn.execute(
                "UPDATE tasks SET status='ready', block_kind=NULL, block_recurrences=0 "
                "WHERE id=?",
                (tid,),
            )
            conn.commit()

        with kbc.connect_closing() as conn:
            kb.block_task(conn, tid, kind="needs_input", reason="test block 1")
        with kbc.connect_closing() as conn:
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            conn.commit()
        with kbc.connect_closing() as conn:
            kb.block_task(conn, tid, kind="needs_input", reason="test block 2 — trip breaker")

        with kbc.connect_closing() as conn:
            row = conn.execute(
                "SELECT status, block_kind, block_recurrences, hub_escalation "
                "FROM tasks WHERE id=?",
                (tid,),
            ).fetchone()
        assert dict(row) == {
            "status": "triage",
            "block_kind": "needs_input",
            "block_recurrences": 2,
            "hub_escalation": 1,
        }, f"breaker did not park card as expected: {dict(row)}"
        return tid
    finally:
        if prev_board is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_board


def _fresh_triage_card(board_slug: str, title: str) -> str:
    """Park a fresh card in `triage` WITHOUT hub_escalation."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    prev_board = os.environ.get("HERMES_KANBAN_BOARD")
    os.environ["HERMES_KANBAN_BOARD"] = board_slug
    try:
        with kbc.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title=title,
                body="fresh triage card — no quarantine",
                assignee="friday",
                initial_status="blocked",
            )
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            conn.commit()
        return tid
    finally:
        if prev_board is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_board


def _archive(card_id: str, board_slug: str) -> None:
    from hermes_cli import kanban_db_connect as kbc

    prev_board = os.environ.get("HERMES_KANBAN_BOARD")
    os.environ["HERMES_KANBAN_BOARD"] = board_slug
    try:
        with kbc.connect_closing() as conn:
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (card_id,))
            conn.commit()
    finally:
        if prev_board is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_board


def _stub_decomposer(monkeypatch, *, on_decompose):
    """Replace hermes_cli.kanban_decompose with a stub that records calls.

    The dispatcher tick does ``from hermes_cli import kanban_decompose as
    _decomp`` *inside* the function body, so patching the module via
    ``sys.modules`` is required. Patches the hermes_cli namespace reference
    too for callers that imported the symbol.
    """
    import hermes_cli
    from hermes_cli import kanban_decompose as real_kd

    fake = SimpleNamespace(
        list_triage_ids=real_kd.list_triage_ids,
        decompose_task=lambda task_id, author=None, timeout=None: on_decompose(task_id),
    )

    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)
    return fake


def test_auto_decompose_tick_skips_quarantined_cards(monkeypatch, board):
    """A hub_escalation=1 triage card must NOT be passed to decompose_task."""
    seen: list[str] = []

    def on_decompose(task_id):
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli.kanban_decompose import DecomposeOutcome

        seen.append(task_id)
        # Hard assertion: a quarantined card leaking here is the bug under test.
        prev_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = board
        try:
            with kbc.connect_closing() as conn:
                row = conn.execute(
                    "SELECT hub_escalation FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                assert not (row and row["hub_escalation"]), (
                    f"auto_decompose_tick decomposed a quarantined card: {task_id}"
                )
        finally:
            if prev_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = prev_board
        return DecomposeOutcome(task_id, True, "ok", fanout=False, child_ids=None)

    _stub_decomposer(monkeypatch, on_decompose=on_decompose)

    quarantined_id = _quarantine_card(board, "t_9f3c5d4a quarantined card")
    try:
        successes = _dispatcher(board).auto_decompose_tick(auto_decompose_per_tick=10)
        assert successes == 0, (
            f"auto_decompose_tick returned {successes}; expected 0 "
            f"(quarantined card {quarantined_id} should NOT be decomposed)"
        )
        assert quarantined_id not in seen, (
            f"decompose_task was called with quarantined card {quarantined_id}"
        )
    finally:
        _archive(quarantined_id, board)


def test_auto_decompose_tick_still_processes_fresh_triage(monkeypatch, board):
    """Sanity check: a non-quarantined triage card is still decomposed.

    Proves the gate is not over-broad — fresh triage rows still decompose.
    """
    from hermes_cli.kanban_decompose import DecomposeOutcome

    seen: list[str] = []

    def on_decompose(task_id):
        seen.append(task_id)
        return DecomposeOutcome(task_id, True, "ok", fanout=False, child_ids=None)

    _stub_decomposer(monkeypatch, on_decompose=on_decompose)

    fresh_id = _fresh_triage_card(board, "t_9f3c5d4a fresh triage card")
    try:
        successes = _dispatcher(board).auto_decompose_tick(auto_decompose_per_tick=10)
        assert successes == 1, (
            f"fresh triage card should be decomposed; got {successes} successes; "
            f"seen={seen}"
        )
        assert fresh_id in seen
    finally:
        _archive(fresh_id, board)


def test_quarantined_card_does_not_burn_aux_llm_turn(monkeypatch, board):
    """Negative control: confirm the quarantine gate runs BEFORE the LLM call.

    Decompose-task's gate short-circuits with `ok=False, reason='quarantined
    (hub_escalation=1); clear flag manually to resume'` — verify the real
    function returns this WITHOUT calling the auxiliary LLM (sentinel: any
    raise from the sentinel is the test's pass condition).
    """
    from hermes_cli import kanban_decompose as kd

    def fail_if_called(*a, **kw):
        raise AssertionError("auxiliary LLM must not be invoked on quarantined card")

    # Stub the aux LLM call inside kanban_decompose with a sentinel that
    # explodes if reached. The quarantine gate must short-circuit first.
    monkeypatch.setattr(kd, "_call_aux", fail_if_called)

    quarantined_id = _quarantine_card(board, "t_9f3c5d4a aux-not-called guard")
    try:
        out = kd.decompose_task(quarantined_id, author="t9f3-test")
        assert not out.ok
        assert "quarantined" in out.reason.lower(), f"got: {out.reason}"
    finally:
        _archive(quarantined_id, board)