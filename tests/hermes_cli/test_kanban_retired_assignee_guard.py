"""Retired-assignee guard: a dead profile name must fail loudly (t_252663de).

The ``vision`` profile was retired on 2026-10-05 but the label survived as a
kanban assignee. The dispatcher bucketed an assignee whose profile does not
exist into ``skipped_nonspawnable`` and left the card in ``ready`` — silently,
forever — so 22 cards stranded over the following six weeks while the board
kept accepting new ones.

The guard separates a RETIRED profile (operator-declared in
``kanban.retired_assignees``) from an ordinary control-plane lane:

  * creation against a retired assignee is refused outright;
  * a card that still carries one is parked ``blocked``/``needs_input`` with a
    distinct ``assignee_retired`` event and a WARNING log line, instead of
    being silently skipped.

The registry is opt-in: an assignee that is absent from disk and NOT listed
keeps the historical lane behaviour, so existing multi-lane installs are
unaffected.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_retired_assignee as kra


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def no_such_profile(monkeypatch):
    """Pretend NO profile exists on disk (the post-retirement state)."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)


@pytest.fixture(autouse=True)
def _no_deploy_guard(monkeypatch):
    """Neutralize the deploy-branch guard for this module.

    ``dispatch_once`` runs ``check_deploy_branch()`` on every tick. That
    guard inspects the SHARED agent tree, so under a git worktree it reads
    the real home's ``.git`` — which the hermetic-test guard refuses
    ("TEST BUG: file I/O against the REAL hermes home"). The deploy guard is
    orthogonal to the retired-assignee guard; turning it off keeps this
    module hermetic instead of depending on an operator-set env var.
    """
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "off")


def _retire(kanban_home, *names):
    """Write a config that declares *names* retired."""
    listed = ", ".join(names)
    (kanban_home / "config.yaml").write_text(
        f"kanban:\n  retired_assignees:\n    - {listed}\n", encoding="utf-8",
    )


# --- registry semantics -----------------------------------------------------

def test_registry_absent_means_inert(kanban_home):
    """No key configured = no retired assignees (backward compatible)."""
    assert kra.retired_assignees() == frozenset()
    assert kra.is_retired_assignee("vision") is False


def test_registry_reads_list_and_string(kanban_home):
    """Both a YAML list and a comma-separated string are accepted."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  retired_assignees:\n    - vision\n    - oldprofile\n", encoding="utf-8",
    )
    assert kra.retired_assignees() == frozenset({"vision", "oldprofile"})

    (kanban_home / "config.yaml").write_text(
        "kanban:\n  retired_assignees: 'vision, oldprofile'\n", encoding="utf-8",
    )
    assert kra.retired_assignees() == frozenset({"vision", "oldprofile"})


def test_registry_ignores_non_retired_name(kanban_home):
    _retire(kanban_home, "vision")
    assert kra.is_retired_assignee("vision") is True
    assert kra.is_retired_assignee("ultron") is False
    assert kra.is_retired_assignee("") is False
    assert kra.is_retired_assignee(None) is False


# --- creation-time guard ----------------------------------------------------

def test_create_refuses_retired_assignee(kanban_home):
    """RED (t_252663de): creating a card against a retired profile must
    raise, so the card never exists to be stranded."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        with pytest.raises(kra.RetiredAssigneeError) as exc:
            kb.create_task(conn, title="stranded card", assignee="vision")
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert "vision" in str(exc.value)
    assert tasks == 0


def test_create_error_is_a_valueerror(kanban_home):
    """``RetiredAssigneeError`` subclasses ValueError so existing callers that
    funnel assignee validation errors keep working unchanged."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="stranded card", assignee="vision")


def test_create_allows_live_assignee(kanban_home):
    """The guard must not block normal creation."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="normal card", assignee="ultron")
    assert tid


def test_create_allows_unlisted_unknown_assignee(kanban_home):
    """An unknown-but-not-retired name is a control-plane lane; it must still
    be creatable (existing multi-lane installs keep working)."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="lane card", assignee="orion-cc")
    assert tid


# --- dispatcher: loud failure instead of silent skip ------------------------

def test_dispatcher_parks_retired_assignee_loudly(kanban_home, no_such_profile, caplog):
    """RED (t_252663de): a card on a retired profile must NOT land in the
    silent ``skipped_nonspawnable`` bucket. It gets a distinct event, a
    WARNING log, and is parked blocked/needs_input so a human sees it."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn, caplog.at_level(logging.WARNING):
        # Create directly in SQL: create_task refuses retired assignees now.
        tid = _force_create(conn, assignee="vision")
        res = kbd.dispatch_once(conn, dry_run=False)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert res.skipped_nonspawnable == []
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert "assignee_retired" in kinds
    assert any("RETIRED" in r.getMessage() for r in caplog.records)


def test_dispatcher_event_names_the_profile(kanban_home, no_such_profile):
    """The event payload must name the dead profile, like the
    ``skipped_nonspawnable`` event does (#122422)."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        tid = _force_create(conn, assignee="vision")
        kbd.dispatch_once(conn, dry_run=False)
        events = [(e.kind, e.payload) for e in kb.list_events(conn, tid)]
    matches = [p for (k, p) in events if k == "assignee_retired"]
    assert matches
    assert matches[0].get("assignee") == "vision"


def test_dispatcher_dry_run_writes_nothing(kanban_home, no_such_profile):
    """A dry-run tick must not block the card or write events."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        tid = _force_create(conn, assignee="vision")
        kbd.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert task is not None and task.status == "ready"
    assert "assignee_retired" not in kinds


def test_dispatcher_retired_event_written_once(kanban_home, no_such_profile):
    """Repeated ticks must not append a row every minute (the
    ``skipped_nonspawnable`` discipline, #101015)."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        tid = _force_create(conn, assignee="vision")
        for _ in range(3):
            kbd.dispatch_once(conn, dry_run=False)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("assignee_retired") == 1


def test_unlisted_unknown_assignee_still_silently_skipped(
    kanban_home, no_such_profile,
):
    """Unchanged upstream behaviour for a non-retired lane: the card is
    skipped, not blocked. The guard is opt-in per profile name."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        tid = _force_create(conn, assignee="orion-cc")
        res = kbd.dispatch_once(conn, dry_run=False)
        task = kb.get_task(conn, tid)
    assert res.skipped_nonspawnable == [tid]
    assert task is not None and task.status == "ready"


def test_retired_assignee_is_not_spawnable_telemetry(kanban_home, no_such_profile):
    """A retired-assignee card must not keep the gateway wake-up loop hot:
    it is parked blocked, so it is not spawnable-ready."""
    _retire(kanban_home, "vision")
    with kbc.connect() as conn:
        _force_create(conn, assignee="vision")
        kbd.dispatch_once(conn, dry_run=False)
        assert kbd.has_spawnable_ready(conn) is False


def _force_create(conn, *, assignee):
    """Create a card bypassing the creation guard (simulates a card created
    before the guard existed, or one written by another tool)."""
    import time
    now = int(time.time())
    tid = f"t_test_retired_{now}_{id(conn) % 100000}"
    cur = conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at) "
        "VALUES (?, ?, ?, ?, 'ready', ?)",
        (tid, "legacy card", "", assignee, now),
    )
    assert cur.rowcount == 1
    conn.commit()
    return tid
