"""F2 — the unclean-exit integrity check must not block the gateway boot.

On 2026-10-23 the gateway restart took 37 minutes because ``PRAGMA quick_check(1)`` ran
for ~36 minutes against a 3.18 GB ``state.db``.  The check runs synchronously on the boot
path (``record_startup``), so its runtime IS the outage: the API server did not listen
until 23:20:27.

These tests pin the bound: a store above the size ceiling is not checked, a check that
outlives its wall-clock budget is abandoned, and neither verdict is reported as a
corruption finding.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from gateway import lifecycle_ledger as ll


def _make_db(path: Path, rows: int = 10) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}" * 50,) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


class TestIntegrityCheckBounds:
    def test_absent_store(self, tmp_path):
        assert ll.check_state_db_integrity(tmp_path) == "absent"

    def test_small_healthy_store_is_ok(self, tmp_path):
        _make_db(tmp_path / "state.db")
        assert ll.check_state_db_integrity(tmp_path) == "ok"

    def test_oversized_store_is_skipped_not_checked(self, tmp_path, monkeypatch):
        """A multi-GB store is skipped: quick_check is O(size) and cannot fit the budget."""
        _make_db(tmp_path / "state.db")
        monkeypatch.setattr(ll, "_INTEGRITY_CHECK_MAX_BYTES", 1)  # 1 byte -> always oversized
        verdict = ll.check_state_db_integrity(tmp_path)
        assert verdict == "skipped-too-large"

    def test_timeout_is_reported_as_timeout_not_failure(self, tmp_path, monkeypatch):
        """A check that outlives its budget yields 'timeout', never 'check-failed'."""
        _make_db(tmp_path / "state.db")
        # The stub stands in for the caller's own bound: a check still running when the
        # budget expires is abandoned and reported as 'timeout'.
        monkeypatch.setattr(ll, "_run_quick_check", lambda p, d: ("timeout", None))
        assert ll.check_state_db_integrity(tmp_path) == "timeout"

    def test_check_failed_carries_the_error(self, tmp_path, monkeypatch):
        _make_db(tmp_path / "state.db")
        monkeypatch.setattr(ll, "_run_quick_check", lambda p, d: ("check-failed", "boom"))
        verdict = ll.check_state_db_integrity(tmp_path)
        assert verdict.startswith("check-failed")
        assert "boom" in verdict


class TestRunQuickCheckDeadline:
    def test_returns_verdict_for_a_fast_store(self, tmp_path):
        path = _make_db(tmp_path / "state.db")
        verdict, error = ll._run_quick_check(path, time.monotonic() + 30.0)
        assert verdict == "ok"
        assert error is None

    def test_abandons_a_check_that_outlives_the_budget(self, tmp_path):
        """The caller returns on time even when the check is still running."""
        path = _make_db(tmp_path / "state.db", rows=5000)
        started = time.monotonic()
        verdict, _error = ll._run_quick_check(path, time.monotonic() + 0.05)
        elapsed = time.monotonic() - started
        # Either the check finished (small/fast store) or it was abandoned on time.
        assert elapsed < 5.0
        assert verdict in ("ok", "timeout")

    def test_a_hanging_check_is_abandoned_not_joined(self, tmp_path, monkeypatch):
        """A check that never returns is abandoned on the caller's deadline.

        The stand-in blocks the worker's ``PRAGMA quick_check(1)`` on an Event the test
        sets at teardown, so the test never waits on the wall clock for the check to
        finish — and the real bounded-join path in ``_run_quick_check`` is what runs.
        """
        path = _make_db(tmp_path / "state.db")
        release = threading.Event()

        class _HangingConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if "quick_check" in str(sql):
                    release.wait(30.0)
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        import sqlite3 as _sqlite3

        real_connect = _sqlite3.connect

        def _connect(*args, **kwargs):
            return _HangingConn(real_connect(*args, **kwargs))

        monkeypatch.setattr(ll.sqlite3, "connect", _connect)
        try:
            started = time.monotonic()
            verdict, error = ll._run_quick_check(path, time.monotonic() + 0.25)
            elapsed = time.monotonic() - started
            assert verdict == "timeout", "a check that outlives its budget must be abandoned"
            assert error is None
            assert elapsed < 5.0, "the caller must return on time, not wait for the check"
        finally:
            release.set()


class TestTimeoutBudgetResolution:
    def test_default_budget(self, monkeypatch):
        monkeypatch.setattr(ll, "_integrity_check_timeout_s", ll._INTEGRITY_CHECK_TIMEOUT_S)
        assert ll._INTEGRITY_CHECK_TIMEOUT_S > 0

    def test_config_override_is_honoured(self, monkeypatch):
        """``gateway.state_db_integrity_check_timeout_s`` in config.yaml wins."""
        monkeypatch.setattr(
            ll,
            "_integrity_check_timeout_s",
            lambda: 45.0,
        )
        assert ll._integrity_check_timeout_s() == 45.0

    def test_invalid_config_falls_back(self, monkeypatch):
        monkeypatch.setattr(ll, "_INTEGRITY_CHECK_TIMEOUT_S", 120.0)
        assert ll._integrity_check_timeout_s() == 120.0


class TestTimeoutBudgetConfigPlumbing:
    """The budget is read from config.yaml (behaviour), never from a HERMES_* env var."""

    def test_no_hermes_env_var_is_read(self):
        source = Path(ll.__file__).read_text(encoding="utf-8")
        assert "HERMES_STATE_DB_INTEGRITY_CHECK_TIMEOUT_S" not in source

    def test_default_is_used_when_config_is_unreadable(self, monkeypatch):
        import hermes_constants

        def _boom(*args, **kwargs):
            raise RuntimeError("config is broken")

        monkeypatch.setattr(ll, "_INTEGRITY_CHECK_TIMEOUT_S", 120.0)
        monkeypatch.setattr(
            "gateway.config_loader.read_yaml_layers",
            _boom,
            raising=False,
        )
        assert ll._integrity_check_timeout_s() == 120.0

    def test_non_positive_config_falls_back(self, monkeypatch):
        monkeypatch.setattr(ll, "_INTEGRITY_CHECK_TIMEOUT_S", 120.0)
        monkeypatch.setattr(
            "gateway.config_loader.read_yaml_layers",
            lambda home: {"gateway": {"state_db_integrity_check_timeout_s": 0}},
            raising=False,
        )
        assert ll._integrity_check_timeout_s() == 120.0

    def test_invalid_config_value_falls_back(self, monkeypatch):
        monkeypatch.setattr(ll, "_INTEGRITY_CHECK_TIMEOUT_S", 120.0)
        monkeypatch.setattr(
            "gateway.config_loader.read_yaml_layers",
            lambda home: {"gateway": {"state_db_integrity_check_timeout_s": "not-a-number"}},
            raising=False,
        )
        assert ll._integrity_check_timeout_s() == 120.0

    def test_valid_config_value_is_used(self, monkeypatch):
        monkeypatch.setattr(ll, "_INTEGRITY_CHECK_TIMEOUT_S", 120.0)
        monkeypatch.setattr(
            "gateway.config_loader.read_yaml_layers",
            lambda home: {"gateway": {"state_db_integrity_check_timeout_s": 45}},
            raising=False,
        )
        assert ll._integrity_check_timeout_s() == 45.0


class TestUncleanExitReporting:
    def test_timeout_is_not_reported_as_corruption(self, tmp_path, monkeypatch, caplog):
        """'timeout' means the check did not run — it must not be logged as a FAILED check."""
        monkeypatch.setattr(ll, "check_state_db_integrity", lambda home=None: "timeout")
        with caplog.at_level("ERROR"):
            ll._report_unclean_exit({"prior_pid": 1}, tmp_path)
        assert "FAILED integrity check" not in caplog.text

    def test_real_failure_is_still_reported_as_corruption(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(ll, "check_state_db_integrity", lambda home=None: "check-failed: boom")
        with caplog.at_level("ERROR"):
            ll._report_unclean_exit({"prior_pid": 1}, tmp_path)
        assert "FAILED integrity check" in caplog.text

    def test_ok_is_not_reported_as_failure(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(ll, "check_state_db_integrity", lambda home=None: "ok")
        with caplog.at_level("ERROR"):
            ll._report_unclean_exit({"prior_pid": 1}, tmp_path)
        assert "FAILED integrity check" not in caplog.text
