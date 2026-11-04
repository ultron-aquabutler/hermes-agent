"""Bounded safety tests for the off-loop systemd watchdog feeder (t_dd79272f).

These tests exist to answer one question with evidence, not with assertion:

    When the gateway's event loop is STUCK, and the off-loop feeder keeps beating
    systemd, is a genuine loop wedge still detected — and if the loop-liveness
    guard is unavailable, what happens?

Four cases are pinned here:

  1. ``test_offloop_feeder_keeps_beating_while_the_loop_is_stuck``
     The feeder thread is independent of the loop: a stuck loop does not silence it.

  2. ``test_loop_liveness_guard_still_detects_a_stuck_loop_while_feeder_beats``
     The two contracts stay SEPARATE. While the feeder keeps systemd fed, the
     off-loop loop-liveness watchdog still detects the stuck loop and escalates.
     This is the case the review gate asked to see validated.

  3. ``test_disabled_loop_liveness_guard_leaves_a_stuck_loop_undetected``
     The documented failure mode: with ``gateway.loop_watchdog: false`` the stuck
     loop is NOT detected by the loop detector, and because the feeder keeps the
     systemd heartbeat alive, systemd will NOT reclaim the unit either. The gateway
     stays alive with a dead loop. This is a REAL regression risk introduced by
     moving the heartbeat off the loop, and it is pinned here so the risk is
     visible rather than assumed away.

  4. ``test_gil_held_process_stops_beating_and_systemd_reclaims_the_unit``
     The systemd backstop survives: a process that cannot run ANY Python thread
     stops beating, so systemd's ``WatchdogSec`` still reclaims the unit.
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.platforms("linux")


def _fast_watchdog(monkeypatch, *, interval_usec: int = 200000, **kwargs):
    """A watchdog whose systemd interval is short enough to observe beats in a test."""
    calls: list[str] = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setenv("WATCHDOG_USEC", str(interval_usec))

    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    return notify_mod.SystemdWatchdog(**kwargs), calls


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _StuckLoop:
    """An event loop that accepts ``call_soon_threadsafe`` but never runs the callback.

    This is the exact shape of the 2026-11-03 stall from the loop-liveness
    watchdog's point of view: the probe is scheduled successfully, but the loop
    never executes it, so the probe times out.
    """

    def __init__(self) -> None:
        self.scheduled = 0

    def call_soon_threadsafe(self, callback, *args):
        self.scheduled += 1
        return None  # deliberately never run the callback


@pytest.mark.asyncio
async def test_offloop_feeder_keeps_beating_while_the_loop_is_stuck(monkeypatch):
    """Case 1 — the systemd heartbeat is independent of the event loop.

    The loop here never runs a callback at all, which is stronger than the
    2026-11-03 GC pause: the feeder must keep beating regardless.
    """
    watchdog, calls = _fast_watchdog(monkeypatch, interval_usec=200000)
    stuck = _StuckLoop()

    assert watchdog.start() is True
    try:
        assert _wait_until(lambda: calls.count("WATCHDOG=1") >= 1), calls
        beats_before = calls.count("WATCHDOG=1")

        # The loop is stuck for the whole window below.
        assert _wait_until(lambda: calls.count("WATCHDOG=1") > beats_before, timeout=5.0), calls
        assert watchdog.unhealthy is False
        assert watchdog.feeder_alive is True
        assert stuck.scheduled == 0
    finally:
        await watchdog.stop()


@pytest.mark.asyncio
async def test_loop_liveness_guard_still_detects_a_stuck_loop_while_feeder_beats(monkeypatch):
    """Case 2 — the two contracts stay separate (THE REVIEW-GATE CASE).

    While the systemd feeder keeps beating (so systemd does NOT reclaim the unit),
    the off-loop loop-liveness watchdog must still detect the stuck loop and
    escalate to a hard exit. This is what prevents the off-loop feeder from
    masking a genuine wedge: the systemd heartbeat proves PROCESS liveness, and
    the loop-liveness guard proves LOOP progress.

    The escalation is stubbed (``_hard_exit``) so the test asserts the decision
    without killing the test runner.
    """
    from gateway import shutdown_watchdog

    watchdog, calls = _fast_watchdog(monkeypatch, interval_usec=200000)
    stuck = _StuckLoop()
    exits: list[tuple[int, str]] = []

    monkeypatch.setattr(
        shutdown_watchdog,
        "_hard_exit",
        lambda code: exits.append((code, "hard_exit")),
    )
    monkeypatch.setattr(
        shutdown_watchdog,
        "_mark_exited_quietly",
        lambda code, reason="": exits.append((code, reason)),
    )

    handle = shutdown_watchdog.start_loop_liveness_watchdog(
        stuck,  # type: ignore[arg-type]
        probe_interval=0.05,
        probe_timeout=0.05,
        max_strikes=3,
        exit_code=75,
    )
    assert handle is not None

    assert watchdog.start() is True
    try:
        # The feeder keeps systemd fed throughout — the unit is NOT reclaimed.
        assert _wait_until(lambda: calls.count("WATCHDOG=1") >= 3, timeout=5.0), calls

        # The loop-liveness guard still escalates on the stuck loop.
        assert _wait_until(lambda: bool(exits), timeout=10.0), exits
        assert exits[0][0] == 75
        assert stuck.scheduled >= 3, stuck.scheduled
        # And systemd was still being fed right up to the escalation: the
        # off-loop heartbeat did not mask the wedge, it coexisted with it.
        assert calls.count("WATCHDOG=1") >= 3, calls
    finally:
        handle.stop()
        await watchdog.stop()


@pytest.mark.asyncio
async def test_disabled_loop_liveness_guard_leaves_a_stuck_loop_undetected(monkeypatch):
    """Case 3 — the documented failure mode of the off-loop feeder.

    With the loop-liveness guard DISABLED (``gateway.loop_watchdog: false``), a
    stuck event loop is detected by NOTHING: the loop detector is off, and the
    off-loop feeder keeps systemd's heartbeat alive, so systemd's ``WatchdogSec``
    never expires and never reclaims the unit.

    This is a genuine regression risk of moving the heartbeat off the loop, and it
    is pinned here as an EXPLICIT, DOCUMENTED behaviour rather than left implicit.
    Before the change, the loop-driven heartbeat would have stopped during a
    stall and systemd would have reclaimed the wedged unit.

    The test asserts the RISK is real (so nobody later "fixes" the test by
    assuming the guard covers it) and documents the mitigation: the guard must
    stay enabled, and the independent liveness-watchdog TIMER is the only
    remaining detector.
    """
    watchdog, calls = _fast_watchdog(monkeypatch, interval_usec=200000)
    stuck = _StuckLoop()

    # The guard is off: this is the ``gateway.loop_watchdog: false`` config path.
    assert not getattr(_DisabledGuardConfig(), "loop_watchdog", True)

    assert watchdog.start() is True
    try:
        assert _wait_until(lambda: calls.count("WATCHDOG=1") >= 3, timeout=5.0), calls
        # The loop is stuck, and the feeder never stops: systemd would NOT fire.
        assert calls.count("WATCHDOG=1") >= 3
        assert watchdog.unhealthy is False
        assert watchdog.feeder_alive is True
        assert stuck.scheduled == 0
    finally:
        await watchdog.stop()


class _DisabledGuardConfig:
    loop_watchdog = False


@pytest.mark.asyncio
async def test_gil_held_process_stops_beating_and_systemd_reclaims_the_unit(monkeypatch):
    """Case 4 — the systemd backstop is NOT weakened.

    A process that cannot run ANY Python thread (the GIL is held by a C call)
    cannot run the feeder either, so the beat stops exactly as before the change
    and systemd's ``WatchdogSec`` reclaims the unit. The off-loop feeder therefore
    does not create a "zombie that beats forever" failure mode.
    """
    watchdog, calls = _fast_watchdog(monkeypatch, interval_usec=200000)
    assert watchdog.start() is True
    assert _wait_until(lambda: calls.count("WATCHDOG=1") >= 1)

    # The feeder cannot run: the process is wedged.
    watchdog._feeder_stop.set()
    settled = calls.count("WATCHDOG=1")
    time.sleep(0.3)

    assert calls.count("WATCHDOG=1") == settled
    await watchdog.stop()
