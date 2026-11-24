"""Regression tests: gateway terminal/admission tombstones must be reclaimed.

The gateway writes one ``gateway.terminal_worker.v1.*`` admission proof plus a
``gateway.admission.result.v1.*`` result per admitted request, and each proof carries an
unbounded ``receipts`` array copied out of ``worker_receipts``. Those rows live in ``state_meta``
and are NOT reachable from ``prune_sessions`` (that path deletes ``messages``/``sessions`` rows
only), so before this fix they grew without bound — 10,650 of them (63 MB) in a single sweep
burst on one profile.

The contract this suite pins down:

  * A terminal admission/worker tombstone is EVIDENCE of a settled request, not the deletion
    fence. It ages out at the exact-retry horizon, whoever retired the session.
  * The FENCE is the ``gateway.retired_session.v1.*`` marker. A user delete's marker carries no
    ``expires`` and must therefore survive forever: without it a deleted id could be recreated,
    re-admitted or reissued as a Bot Chat generation. A housekeeping sweep's marker does carry
    ``expires`` and ages out with the horizon.
  * The sweep must never touch live session history.
"""
import json
import os
import sys
import tempfile
import time

# ``tests/hermes_state/`` is a package, so a bare ``import hermes_state`` from inside
# ``tests/`` can resolve to it instead of the top-level module. Put the repo root first.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hermes_state import SessionDB  # noqa: E402
from hermes_state_mutation_retirement import RETIRED_PREFIX  # noqa: E402
from hermes_state_terminal import (  # noqa: E402
    ADMISSION_PREFIX, RESULT_PREFIX, WORKER_PREFIX,
)


def _make_db(tmpdir):
    from pathlib import Path
    return SessionDB(Path(tmpdir) / "state.db")


def _seed_worker_tombstone(conn, key, session_id, retired_at):
    payload = {
        "adoption_digest": "d" * 64,
        "execution_id": key[len(WORKER_PREFIX):],
        "generation": 1,
        "kind": "compute",
        "last_sequence": 3,
        "owner_epoch": 1,
        "receipts": [{"payload_digest": "c" * 64, "result_json": None, "sequence": i}
                     for i in range(1, 4)],
        "retired_at": retired_at,
        "session_id": session_id,
        "status": "terminal",
    }
    conn.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                 (key, json.dumps(payload)))


def _seed_admission_tombstone(conn, key, session_id, retired_at):
    payload = {
        "admission_id": key[len(ADMISSION_PREFIX):],
        "generation": 1,
        "intent": "queue",
        "lineage_json": "[]",
        "outcome": "settled",
        "owner_epoch": 1,
        "payload_digest": "p" * 64,
        "payload_json": "{}",
        "principal_id": "api",
        "request_id": "req-1",
        "retired_at": retired_at,
        "seq": 1,
        "status": "terminal",
        "target_session_id": session_id,
    }
    conn.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                 (key, json.dumps(payload)))


def _seed_swept_owner(conn, session_id, retired_at):
    """The owning session's retired marker for a HOUSEKEEPING SWEEP: carries ``expires``."""
    conn.execute(
        "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
        (RETIRED_PREFIX + session_id,
         json.dumps({"retired_at": retired_at, "own": True, "expires": True})))


def _seed_deleted_owner(conn, session_id, retired_at):
    """The owning session's retired marker for a USER DELETE: NO ``expires`` — it is the fence."""
    conn.execute(
        "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
        (RETIRED_PREFIX + session_id,
         json.dumps({"retired_at": retired_at, "own": True})))


def _count(db, *globs):
    def _q(conn):
        where = " OR ".join("key GLOB ?" for _ in globs)
        return conn.execute(f"SELECT count(*) FROM state_meta WHERE {where}", globs).fetchone()[0]
    return db._execute_write(_q)


def test_prune_reclaims_an_aged_worker_tombstone():
    """A terminal worker tombstone past the horizon is reclaimed, ``expires`` or not.

    The tombstone is EVIDENCE, so it ages out for every owner. Requiring an ``expires`` marker
    of its own would leave the legacy rows — the ones the 2026-11-18 burst produced, none of
    which carry the marker — unreachable forever.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)

        def seed(conn):
            _seed_worker_tombstone(conn, WORKER_PREFIX + "worker-1", "sess-a", old)

        db._execute_write(seed)
        assert _count(db, WORKER_PREFIX + "*") == 1

        reclaimed = db.prune_retired_tombstones()

        assert _count(db, WORKER_PREFIX + "*") == 0, "aged worker tombstone should be gone"
        assert reclaimed >= 1, f"expected >=1 reclaimed, got {reclaimed}"
        db.close()


def test_prune_reclaims_an_aged_admission_tombstone_and_its_result_row():
    """An aged admission tombstone goes with its result row and its identity pointer."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)
        adm_id = "adm-legacy"

        def seed(conn):
            _seed_swept_owner(conn, "sess-b", old)
            _seed_admission_tombstone(conn, ADMISSION_PREFIX + adm_id, "sess-b", old)
            conn.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                         (RESULT_PREFIX + adm_id, json.dumps({"result": {"final_response": "x"}})))

        db._execute_write(seed)
        assert _count(db, ADMISSION_PREFIX + "*") == 1
        assert _count(db, RESULT_PREFIX + "*") == 1

        reclaimed = db.prune_retired_tombstones()

        assert _count(db, ADMISSION_PREFIX + "*") == 0, "aged admission tombstone should be gone"
        assert _count(db, RESULT_PREFIX + "*") == 0, "its result row should go with it"
        assert reclaimed >= 2, f"expected >=2 reclaimed, got {reclaimed}"
        db.close()


def test_prune_reclaims_an_orphaned_result_row():
    """A result row whose admission tombstone is already gone must still age out.

    The triple-delete that takes an admission tombstone with it only reaches a result row
    through its admission id. Once the admission tombstone is gone the result row is an ORPHAN:
    it has no parent to key on, so without a dedicated path it would sit in ``state_meta``
    forever (293 such rows on one profile).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)

        def seed(conn):
            # No admission tombstone at all -> the result row is an orphan.
            conn.execute(
                "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                (RESULT_PREFIX + "adm-orphan",
                 json.dumps({"result": {"final_response": "x"}, "retired_at": old})))

        db._execute_write(seed)
        assert _count(db, RESULT_PREFIX + "*") == 1

        reclaimed = db.prune_retired_tombstones()

        assert _count(db, RESULT_PREFIX + "*") == 0, "an orphaned result row must age out"
        assert reclaimed >= 1, f"expected >=1 reclaimed, got {reclaimed}"
        db.close()


def test_prune_stamps_an_undated_result_row():
    """A result row written before it learned to carry ``retired_at`` is stamped and ages out.

    A result row is otherwise undated, so an age-based reclaim can never reach it. The stamp
    makes it reachable; it ages from the first sweep, never instantly.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)

        def seed(conn):
            conn.execute(
                "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                (RESULT_PREFIX + "adm-undated", json.dumps({"result": {"final_response": "x"}})))

        db._execute_write(seed)
        db.prune_retired_tombstones()

        rows = db._execute_write(
            lambda conn: conn.execute(
                "SELECT json_extract(value,'$.retired_at') FROM state_meta WHERE key=?",
                (RESULT_PREFIX + "adm-undated",)).fetchone())
        assert rows[0] is not None, "an undated result row must be stamped"
        db.close()


def test_prune_never_reclaims_a_user_deletes_fence():
    """A USER DELETE's fence must survive the sweep — forever.

    The fence holds only ``retired_at`` and no ``expires``, so it is never reclaimable. Without
    it a deleted id could be recreated, re-admitted or reissued as a Bot Chat generation.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)

        def seed(conn):
            _seed_deleted_owner(conn, "sess-deleted", old)

        db._execute_write(seed)
        db.prune_retired_tombstones()
        db.prune_retired_tombstones()

        assert _count(db, RETIRED_PREFIX + "sess-deleted") == 1, "the delete fence must survive"
        db.close()


def test_prune_reclaims_a_swept_sessions_fence():
    """A HOUSEKEEPING SWEEP's own marker ages out — it is not a user delete."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)

        def seed(conn):
            _seed_swept_owner(conn, "sess-swept", old)

        db._execute_write(seed)
        db.prune_retired_tombstones()

        assert _count(db, RETIRED_PREFIX + "sess-swept") == 0, "a swept marker must age out"
        db.close()


def test_prune_keeps_tombstones_inside_the_retry_horizon():
    """A tombstone younger than the horizon is inside the retry window."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        recent = time.time() - 60

        def seed(conn):
            _seed_swept_owner(conn, "sess-recent", recent)
            _seed_worker_tombstone(conn, WORKER_PREFIX + "worker-recent", "sess-recent", recent)

        db._execute_write(seed)
        reclaimed = db.prune_retired_tombstones()

        assert _count(db, WORKER_PREFIX + "*") == 1, "a tombstone inside the horizon must be kept"
        assert reclaimed == 0, "nothing should be reclaimed inside the horizon"
        db.close()


def test_prune_leaves_live_sessions_and_messages_alone():
    """The sweep is a state_meta cleanup; session history must be untouched."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)

        def seed(conn):
            conn.execute(
                "INSERT INTO sessions (id, started_at, source) VALUES (?, ?, ?)",
                ("sess-live", time.time() - 86400, "cli"))
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                ("sess-live", "user", "hello", time.time()))
            _seed_swept_owner(conn, "sess-live", old)
            _seed_worker_tombstone(conn, WORKER_PREFIX + "worker-live", "sess-live", old)

        db._execute_write(seed)
        db.prune_retired_tombstones()

        rows = db._execute_write(
            lambda conn: (
                conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
                conn.execute("SELECT count(*) FROM messages").fetchone()[0]))
        assert rows[0] == 1, "live session must survive the tombstone sweep"
        assert rows[1] == 1, "live message must survive the tombstone sweep"
        db.close()


def test_prune_is_reachable_from_production_housekeeping():
    """The sweep must be callable on a live SessionDB and report what it reclaimed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_db(tmpdir)
        old = time.time() - (30 * 86400)

        def seed(conn):
            for i in range(3):
                _seed_worker_tombstone(conn, WORKER_PREFIX + f"worker-{i}", "sess-z", old)

        db._execute_write(seed)
        reclaimed = db.prune_retired_tombstones()

        assert reclaimed == 3, f"expected 3 reclaimed, got {reclaimed}"
        assert _count(db, WORKER_PREFIX + "*") == 0
        db.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    print(f"\n  {'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
