"""Regression tests for the 2026-11-23 drain defect.

Two defects, both reproduced before the fix:

A. The shutdown log reported every counter as zero while the drain was still
   holding the gateway open, so the log could not explain the wait. The
   drain waits on `_running_agent_count() + managed_turn_count(self)`, but the
   log only printed `_running_agent_count()`.

B. A managed claim whose worker PROCESS had exited but whose `closed` event
   was never set (the owner never reaped it) counted as live work and held the
   drain open for the full timeout -- with every logged counter at zero.

The fix: `managed_worker_is_live()` requires the worker's process to still be
running, and the shutdown log now reports `managed_turns_now`.
"""
import asyncio
import subprocess
import threading

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from gateway.run_runtime import managed_turn_count


class _FakeProcess:
    """A subprocess.Popen stand-in whose liveness the test controls."""

    def __init__(self, alive: bool, pid: int = 4242):
        self._alive = alive
        self.pid = pid

    def poll(self):
        return None if self._alive else 0


def _runner(worker, *, managed_sessions=()):
    """A runner carrying one managed worker and any extra executing sessions."""
    live = None
    sessions = {}
    if managed_sessions:
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        live = type("Live", (), {})()
        live.task = task
        live.route = "route"
        live.event_stream = type("ES", (), {"execution": {"execution_generation": 2}})()
        sessions["s"] = live
    authority = type("Authority", (), {})()
    authority.sessions = sessions
    authority._managed_workers = {"s": worker}
    authority._mutation_tasks = set()
    authority._side_agents = {}
    authority.pending_stops = {}
    authority.adopted = {}
    runner = type("Runner", (), {})()
    runner.session_authority = authority
    runner._running_agents = {}
    runner._running_agent_count = lambda: 0
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._active_deferred_agent_worker_count = lambda: 0
    runner._snapshot_running_agents = dict
    runner._update_runtime_status = lambda *a: None
    runner._drain_work_counts = GatewayShutdownMixin._drain_work_counts.__get__(runner)
    authority.runner = runner
    return runner, sessions


def _worker(alive: bool, closed: bool):
    w = type("Worker", (), {})()
    w.closed = threading.Event()
    if closed:
        w.closed.set()
    w.process = _FakeProcess(alive)
    w.worker = None
    w.control = lambda frame: None
    return w


@pytest.mark.asyncio
async def test_stale_managed_claim_does_not_hold_the_drain():
    """A worker whose process exited but was never reaped must not hold the drain.

    Before the fix this drained for the full timeout with every logged counter
    at zero -- the exact 2026-11-23 15:44 signature.
    """
    worker = _worker(alive=False, closed=False)   # process gone, closed never set
    runner, _ = _runner(worker)

    assert managed_turn_count(runner) == 0, "a stale claim must not count as live work"

    _, timed_out = await GatewayShutdownMixin._drain_active_agents(runner, timeout=5)
    assert timed_out is False, "a stale claim must not make the drain time out"


@pytest.mark.asyncio
async def test_live_managed_claim_still_holds_the_drain():
    """A worker that is genuinely running must still hold the drain.

    This is the behaviour the existing suite pins: a live managed turn is a
    chat turn and the drain waits for it.
    """
    worker = _worker(alive=True, closed=False)
    runner, _ = _runner(worker)

    assert managed_turn_count(runner) == 1

    _, timed_out = await GatewayShutdownMixin._drain_active_agents(runner, timeout=0)
    assert timed_out is True


@pytest.mark.asyncio
async def test_closed_managed_claim_does_not_count():
    """The already-reaped case (closed set) must keep behaving as before."""
    worker = _worker(alive=False, closed=True)
    runner, _ = _runner(worker)

    assert managed_turn_count(runner) == 0
    _, timed_out = await GatewayShutdownMixin._drain_active_agents(runner, timeout=5)
    assert timed_out is False


@pytest.mark.asyncio
async def test_drain_short_circuits_when_a_stale_claim_is_all_there_is():
    """The full-timeout burn must not happen: the drain returns immediately."""
    worker = _worker(alive=False, closed=False)
    runner, _ = _runner(worker)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await GatewayShutdownMixin._drain_active_agents(runner, timeout=180)
    elapsed = loop.time() - started

    assert elapsed < 1.0, (
        f"the drain burned {elapsed:.2f}s of a 180s budget while every counter was zero"
    )


def test_worker_without_a_process_handle_is_treated_as_live():
    """Absent evidence of death is not evidence of death."""
    from gateway.session_runtime_workers import managed_worker_is_live

    w = type("Worker", (), {})()
    w.closed = threading.Event()
    w.process = None
    assert managed_worker_is_live(w) is True


def test_worker_process_that_cannot_be_polled_is_treated_as_live():
    """A worker we cannot interrogate must not be assumed dead."""
    from gateway.session_runtime_workers import managed_worker_is_live

    class _Exploding:
        def poll(self):
            raise OSError("no such process")

    w = type("Worker", (), {})()
    w.closed = threading.Event()
    w.process = _Exploding()
    assert managed_worker_is_live(w) is True
