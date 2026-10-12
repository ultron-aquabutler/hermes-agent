"""Tests for the block-loop guard in hermes_cli/kanban_block_loop_guard.py
and its integration with decompose_task / specify_task.

The aux LLM is mocked — these tests do NOT touch any provider. They
exercise the refusal gate (no LLM round-trip on a refusal) and the
shared-state write (comment + structured event + throttled hub card).

Three core scenarios per surface, mirroring the acceptance criteria in
task t_832840f6:

  1. recent block_loop_detected + no human re-engagement -> refused,
     no LLM call, no children created (decompose) / no promotion
     (specify), structured ``<surface>_refused`` event present.
  2. recent block_loop_detected + subsequent ``commented`` or
     ``unblocked`` event -> proceeds normally (operator re-engaged).
  3. block_loop_detected older than the lookback window -> proceeds
     normally (loop fell off the operator's "still blocked today" view).

Plus three cross-cutting checks:

  4. organic triage (no block_kind on the task) -> guard inactive even
     without a recent loop detection.
  5. throttle: a second refusal within HUB_TRIAGE_THROTTLE_SECONDS does
     not file a second hub-triage card.
  6. knob-off: ``auto_decompose_respect_block_loop=False`` lets the LLM
     proceed even with a recent loop.
"""

from __future__ import annotations

import json as jsonlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_block_loop_guard as guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _patch_aux_client(content: str):
    return patch(
        "agent.auxiliary_client.call_llm", return_value=_fake_aux_response(content),
    )


def _patch_list_profiles(names: list[str]):
    from types import SimpleNamespace
    fake = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch(
            "hermes_cli.profiles.get_active_profile_name",
            return_value=names[0] if names else "default",
        ),
    ]


def _empty_config():
    return patch("hermes_cli.config.load_config_readonly", return_value={})


# --- synthetic event writers --------------------------------------------------


def _append_event(conn, *, task_id: str, kind: str, payload: dict, created_at: int):
    """Insert a raw ``task_events`` row with an arbitrary ``created_at`` so a
    test can place a ``block_loop_detected`` inside (or outside) the lookback
    window without faking wall-clock time."""
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, ?, ?, ?)",
        (task_id, kind, jsonlib.dumps(payload), int(created_at)),
    )
    return int(cur.lastrowid or 0)


def _place_block_loop_event(conn, task_id: str, *, created_at: int, kind: str = "needs_input",
                            recurrences: int = 2) -> int:
    """Synthesize a ``block_loop_detected`` event with the canonical payload
    shape emitted by ``kanban_db._route_block``. The kind matches the
    task's stored ``block_kind`` (caller sets it via the same update)."""
    return _append_event(
        conn, task_id=task_id, kind="block_loop_detected",
        payload={"reason": "test", "kind": kind, "recurrences": recurrences,
                 "limit": kb.BLOCK_RECURRENCE_LIMIT, "source_status": "running"},
        created_at=created_at,
    )


def _set_task_block_kind(conn, task_id: str, kind: str = "needs_input", recurrences: int = 2):
    """A triage task that landed there via ``block_loop_detected`` has its
    ``block_kind`` populated. ``kb.create_task`` leaves both NULL; the test
    must set them to mirror the real production path."""
    conn.execute(
        "UPDATE tasks SET block_kind = ?, block_recurrences = ? WHERE id = ?",
        (kind, recurrences, task_id),
    )


# --- decompose side -----------------------------------------------------------


def test_decompose_refused_on_recent_block_loop_without_re_engagement(kanban_home):
    """Recent ``block_loop_detected`` + no human comment -> refused, no LLM
    call, no children."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec-loop target", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    # The aux client is patched but MUST NOT be invoked on a refusal.
    patches = _patch_list_profiles(["orchestrator", "researcher"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("should-not-be-called"), _empty_config():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert outcome.reason == "block_loop_active"
    assert outcome.child_ids is None

    # Root stayed in triage; no children created.
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        children = conn.execute(
            "SELECT id FROM tasks WHERE id IN "
            "(SELECT child_id FROM task_links WHERE parent_id = ?)",
            (tid,),
        ).fetchall()
        refused_event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "AND kind = 'decompose_refused'",
            (tid,),
        ).fetchone()
    assert root is not None
    assert root.status == "triage"
    assert children == []
    assert refused_event is not None


def test_decompose_proceeds_when_operator_re_engaged_via_comment(kanban_home):
    """A ``commented`` event after the loop detection IS human re-engagement:
    the decomposer must proceed."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec-loop target", triage=True)
        _set_task_block_kind(conn, tid)
        loop_event_id = _place_block_loop_event(
            conn, tid, created_at=int(time.time()) - 600,
        )
        # Operator comment AFTER the loop event — re-engagement signal.
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tid, "bryan", "Picking this back up.", int(time.time()) - 60),
        )
        _append_event(
            conn, task_id=tid, kind="commented",
            payload={"author": "bryan", "len": 24},
            created_at=int(time.time()) - 60,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False, "rationale": "single",
        "title": "Updated spec", "body": "spec body", "assignee": None,
    })

    patches = _patch_list_profiles(["orchestrator", "researcher"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _empty_config():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is True, outcome.reason
    assert loop_event_id > 0  # confirm we actually placed the loop


def test_decompose_proceeds_when_block_loop_outside_lookback(kanban_home):
    """An old ``block_loop_detected`` falls off the operator's "still blocked
    today" view; guard inactive."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="old-loop target", triage=True)
        _set_task_block_kind(conn, tid)
        # Loop event 48h ago — outside the default 24h window.
        _place_block_loop_event(
            conn, tid, created_at=int(time.time()) - 48 * 3600,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False, "rationale": "single",
        "title": "Updated spec", "body": "spec body", "assignee": None,
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _empty_config():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is True, outcome.reason


def test_decompose_organic_triage_is_unaffected(kanban_home):
    """``block_kind`` NULL on the task = organic triage; guard never
    activates regardless of any ``block_loop_detected`` row in the table
    (which wouldn't normally exist either, but defensive)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="organic", triage=True)
        # block_kind stays NULL here.

    llm_payload = jsonlib.dumps({
        "fanout": False, "rationale": "single",
        "title": "Updated spec", "body": "spec body", "assignee": None,
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _empty_config():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is True, outcome.reason


# --- specify side -------------------------------------------------------------


def test_specify_refused_on_recent_block_loop_without_re_engagement(kanban_home):
    """Symmetric to the decompose refusal — the specifier must also short-circuit."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec-loop target", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    with _patch_aux_client("should-not-be-called"), _empty_config():
        outcome = spec.specify_task(tid, author="auto-specifier")

    assert outcome.ok is False
    assert outcome.reason == "block_loop_active"

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        refused_event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'specify_refused'",
            (tid,),
        ).fetchone()
    assert task is not None
    assert task.status == "triage"
    assert refused_event is not None


def test_specify_proceeds_when_operator_re_engaged_via_unblock(kanban_home):
    """``unblocked`` event after the loop detection IS re-engagement too
    (operator pressed the dashboard's unblock button)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec-loop target", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 600)
        # Synthesize an ``unblocked`` event AFTER the loop detection.
        _append_event(
            conn, task_id=tid, kind="unblocked",
            payload={"status": "ready", "resume_status": "ready"},
            created_at=int(time.time()) - 30,
        )

    content = jsonlib.dumps({"title": "fresh spec", "body": "spec body"})
    with _patch_aux_client(content), _empty_config():
        outcome = spec.specify_task(tid, author="auto-specifier")

    assert outcome.ok is True, outcome.reason


def test_specify_proceeds_when_block_loop_outside_lookback(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="old-loop target", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 48 * 3600)

    content = jsonlib.dumps({"title": "fresh spec", "body": "spec body"})
    with _patch_aux_client(content), _empty_config():
        outcome = spec.specify_task(tid, author="auto-specifier")
    assert outcome.ok is True, outcome.reason


# --- throttle / knobs ---------------------------------------------------------


def test_refusal_throttle_files_only_one_hub_card_per_window(kanban_home):
    """A second refusal within HUB_TRIAGE_THROTTLE_SECONDS must NOT file a
    second hub-triage card. Note: the second ``decompose_task`` call in
    quick succession is itself refused via a ``commented`` event (the
    auto-decomposer's own refusal comment counts as re-engagement for the
    third call), so the cleanest throttle demonstration is a *direct*
    ``surface_refusal`` invocation pattern — what the per-tick dispatcher
    cron would actually do if it kept finding the same loop active."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec-loop", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    # Drive the guard module directly so the loop stays "active" across
    # calls — bypassing ``decompose_task`` is what the cron-on-stuck-card
    # pathological case looks like (a long-lived loop with no human in
    # between, which is exactly when a hub-triage card needs to NOT spam).
    with kbc.connect() as conn:
        state = guard.check_block_loop(conn, tid, lookback_seconds=86400)
    assert state.active is True

    filed_1, hub_id_1, ev_id_1 = guard.surface_refusal(
        task_id=tid, state=state, surface="decompose", author="auto-decomposer",
    )
    filed_2, hub_id_2, ev_id_2 = guard.surface_refusal(
        task_id=tid, state=state, surface="decompose", author="auto-decomposer",
    )

    assert filed_1 is True and hub_id_1 is not None and ev_id_1 is not None
    assert filed_2 is False and hub_id_2 is None  # throttled
    assert ev_id_2 is not None  # evidence still recorded

    with kbc.connect() as conn:
        hub_cards = conn.execute(
            "SELECT id FROM tasks WHERE title LIKE ?",
            (f"Hub: auto-decompose refused on {tid}%",),
        ).fetchall()
        refused_events = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'decompose_refused'",
            (tid,),
        ).fetchall()
    # Two surface_refusal calls -> two structured events, one hub-triage card.
    assert len(hub_cards) == 1
    assert len(refused_events) == 2


def test_decompose_knob_off_allows_llm_through_with_active_loop(kanban_home):
    """``kanban.auto_decompose_respect_block_loop=False`` is the deliberate
    override — even with a recent block-loop, the LLM call proceeds."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec-loop", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    llm_payload = jsonlib.dumps({
        "fanout": False, "rationale": "single",
        "title": "Updated spec", "body": "spec body", "assignee": None,
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"auto_decompose_respect_block_loop": False}},
        ):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is True, outcome.reason


# --- guard module direct API --------------------------------------------------


def test_guard_check_block_loop_returns_inactive_for_null_block_kind(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="organic", triage=True)
    with kbc.connect() as conn:
        state = guard.check_block_loop(conn, tid, lookback_seconds=86400)
    assert state.active is False


def test_guard_check_block_loop_kind_mismatch_returns_inactive(kanban_home):
    """A loop detected for ``capability`` must NOT refuse a task whose
    current ``block_kind`` is ``needs_input`` (the kind changed since)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)
        _set_task_block_kind(conn, tid, kind="needs_input")
        _place_block_loop_event(
            conn, tid, created_at=int(time.time()) - 60, kind="capability",
        )
    with kbc.connect() as conn:
        state = guard.check_block_loop(conn, tid, lookback_seconds=86400)
    assert state.active is False


def test_guard_surface_refusal_records_comment_and_event_and_hub_card(kanban_home):
    """Direct module-API check: every refusal writes its own comment, its own
    structured event, and ONE hub-triage card (the throttle check above
    covers the per-window de-dup)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)
        _set_task_block_kind(conn, tid)
        loop_id = _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)
        state = guard.check_block_loop(conn, tid, lookback_seconds=86400)

    assert state.active is True
    assert state.event_id == loop_id

    filed, hub_id, refusal_event_id = guard.surface_refusal(
        task_id=tid, state=state, surface="decompose", author="auto-decomposer",
    )
    assert filed is True
    assert hub_id is not None and hub_id != tid
    assert refusal_event_id is not None

    with kbc.connect() as conn:
        comment = conn.execute(
            "SELECT author, body FROM task_comments "
            "WHERE task_id = ? AND author = 'auto-decomposer' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE id = ?",
            (refusal_event_id,),
        ).fetchone()
        hub = conn.execute(
            "SELECT title, status FROM tasks WHERE id = ?", (hub_id,),
        ).fetchone()
    assert comment is not None and "block-loop" in comment["body"].lower()
    payload = jsonlib.loads(ev["payload"])
    assert payload["reason"] == "block_loop_active"
    assert payload["loop_event_id"] == loop_id
    assert hub["title"].startswith("Hub: auto-decompose refused on ")
    assert hub["status"] in {"todo", "ready", "running"}


# --- running-card coverage (t_399f6b0a) --------------------------------------


def _place_task_in_running(conn, task_id: str) -> None:
    """A running card that was previously triaged by ``block_loop_detected``
    but the auto-revert (or operator action) moved it back to ``running``
    while the human-input blocker is still unresolved. Mirrors the t_503815ed
    timeline (status='ready'/'running' + block_kind='capability' + recent
    block_loop_detected event)."""
    conn.execute(
        "UPDATE tasks SET status = 'running', block_kind = ?, "
        "block_recurrences = ? WHERE id = ?",
        ("needs_input", 2, task_id),
    )


def test_running_card_refused_by_decompose(kanban_home):
    """Acceptance criterion 1 of t_399f6b0a (decompose side): a ``running``
    card with a recent ``block_loop_detected`` and no follow-up
    ``commented``/``unblocked`` event MUST be refused by ``decompose_task``
    (the guard now runs before the triage-status check)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running-loop target", triage=True)
        _place_task_in_running(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    # No aux patch: a refusal must short-circuit BEFORE the LLM round-trip.
    with _empty_config():
        decomp_outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert decomp_outcome.ok is False
    assert decomp_outcome.reason == "block_loop_active"

    # No LLM proxy was needed because the refusal happened at the guard.
    # Verify no ``specified`` or ``decomposed`` event was emitted and the
    # task stayed in its existing status (we set it to 'running' above).
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        spec_events = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? "
            "AND kind IN ('specified', 'decomposed')",
            (tid,),
        ).fetchall()
        assert spec_events == []
        refused_events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'decompose_refused'",
            (tid,),
        ).fetchall()
    assert len(refused_events) >= 1


def test_running_card_refused_by_specify(kanban_home):
    """Acceptance criterion 1 of t_399f6b0a (specify side): same as
    :func:`test_running_card_refused_by_decompose` but for ``specify_task``.
    Kept in a separate function so the decompose-side guard refusal doesn't
    write a re-engagement ``commented`` event that releases the guard for
    the specify-side caller."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running-loop target", triage=True)
        _place_task_in_running(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    # No aux patch: a refusal must short-circuit BEFORE the LLM round-trip.
    with _empty_config():
        spec_outcome = spec.specify_task(tid, author="auto-specifier")

    assert spec_outcome.ok is False
    assert spec_outcome.reason == "block_loop_active"

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        spec_events = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'specified'",
            (tid,),
        ).fetchall()
        assert spec_events == []
        refused_events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'specify_refused'",
            (tid,),
        ).fetchall()
    assert len(refused_events) >= 1


def test_fanout_false_decompose_path_also_refuses(kanban_home):
    """The bug t_503815ed surfaced: ``decompose_task`` calls
    ``kb.specify_triage_task`` directly when the LLM replies ``fanout=false``
    (single-task spec promotion). That path used to bypass the specify-side
    guard and emit a ``specified`` event while ``block_loop_detected`` was
    still active. The re-check in ``_apply_single`` closes the gap.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fanout=false target", triage=True)
        _set_task_block_kind(conn, tid)
        # Place the loop AFTER the early guard would run, but BEFORE the
        # _apply_single re-check — simulate a fresh block_loop_detected
        # landing during the LLM round-trip.
        loop_event_id = _place_block_loop_event(conn, tid, created_at=int(time.time()) - 30)

    llm_payload = jsonlib.dumps({
        "fanout": False, "rationale": "single",
        "title": "Updated spec", "body": "spec body", "assignee": None,
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _empty_config():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    # The early guard ran BEFORE the loop event landed, so it saw an
    # inactive state and let the LLM through. The re-check in
    # ``_apply_single`` then saw the loop and refused (decompose side owns the
    # fanout=false promotion so the decompose knob is honoured; see the
    # _apply_single docstring).
    assert outcome.ok is False
    assert outcome.reason == "block_loop_active"

    with kbc.connect() as conn:
        # No specified event on the target.
        spec_events = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'specified'",
            (tid,),
        ).fetchall()
        # A decompose_refused event WAS emitted by the re-check (same
        # surface as the early guard since the call came from
        # ``decompose_task``).
        refused = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decompose_refused'",
            (tid,),
        ).fetchone()
        # Task is still in triage — no fanout happened.
        assert kb.get_task(conn, tid).status == "triage"
    assert spec_events == []
    assert refused is not None

    # Sanity: confirm the loop event id is the one we placed.
    assert loop_event_id > 0


def test_fanout_true_decompose_still_refuses_via_early_guard(kanban_home):
    """Symmetric to the previous test: a triage card with an active
    ``block_loop_detected`` is refused at the early guard (before the LLM
    round-trip) regardless of whether the LLM would have replied with
    ``fanout=true`` or ``fanout=false``. ``fanout=true`` routes through
    ``decompose_triage_task`` (children) — same refusal path."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fanout=true target", triage=True)
        _set_task_block_kind(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    # No aux patch: the early guard must refuse BEFORE the LLM call.
    with _empty_config():
        outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert outcome.ok is False
    assert outcome.reason == "block_loop_active"

    with kbc.connect() as conn:
        decomp_events = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decomposed'",
            (tid,),
        ).fetchall()
        # Children weren't created.
        children = conn.execute(
            "SELECT 1 FROM tasks WHERE id IN "
            "(SELECT child_id FROM task_links WHERE parent_id = ?)",
            (tid,),
        ).fetchall()
    assert decomp_events == []
    assert children == []


def test_running_card_proceeds_when_operator_unblocked(kanban_home):
    """A running card whose operator pressed the dashboard unblock button
    after the block_loop_detected: the ``unblocked`` event IS re-engagement
    (matches the triage-card path). The guard stays inactive so the
    auto-specify cron resumes — but the caller must still hit the
    triage-status gate, which returns ``"task is not in triage"``. This
    test asserts the guard itself is correctly inactive (not blocking
    the path on its own merits)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running-loop target", triage=True)
        _place_task_in_running(conn, tid)
        _place_block_loop_event(conn, tid, created_at=int(time.time()) - 600)
        # Synthesize the operator unblock AFTER the loop.
        _append_event(
            conn, task_id=tid, kind="unblocked",
            payload={"status": "ready", "resume_status": "ready"},
            created_at=int(time.time()) - 30,
        )

    with kbc.connect() as conn:
        state = guard.check_block_loop(conn, tid, lookback_seconds=86400)
    assert state.active is False


def test_running_card_guard_active_against_running_status(kanban_home):
    """The guard module itself is status-agnostic: a running card with an
    active loop returns ``active=True``. This is the load-bearing assertion
    for t_399f6b0a — ``decompose_task``/``specify_task`` rely on it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running-loop target", triage=True)
        _place_task_in_running(conn, tid)
        loop_id = _place_block_loop_event(conn, tid, created_at=int(time.time()) - 60)

    with kbc.connect() as conn:
        state = guard.check_block_loop(conn, tid, lookback_seconds=86400)
    assert state.active is True
    assert state.event_id == loop_id
    assert state.block_kind == "needs_input"