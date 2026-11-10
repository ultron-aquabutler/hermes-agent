"""Regression: the systemd watchdog feeder must not live on the event loop.

Observed on 2026-11-10 (t_a8395c40): the gateway was SIGKILLed by systemd at 14:39
while a long turn was running. ``NRestarts`` reached 5. The cause was structural:
``SystemdWatchdog.start()`` created the feeder with
``asyncio.create_task(self._run(), ...)``, so the heartbeat was produced BY the event
loop. When the loop stalls -- MCP discovery, a long cron turn, image analysis, an
adapter reconnect -- the feeder misses its deadline, systemd concludes the unit is
hung, and it kills the gateway. The gateway dies holding in-flight work, so the
session it was serving is lost (session ``20261110_133919_9e460535`` died mid-turn at
14:36, three minutes before the kill).

systemd's own timer lives in the systemd manager process and never touches the
gateway's GIL, so a feeder on a separate thread keeps the beat through a stalled
loop.

These tests pin the two properties that matter:
  1. the feeder runs on a THREAD, not on the event loop;
  2. a late loop tick no longer condemns the gateway on its own -- lateness
     accumulates into a grace budget.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from gateway import systemd_notify
from gateway.systemd_notify import SystemdWatchdog


@pytest.fixture
def watchdog(monkeypatch):
    """A watchdog with a short interval and a fake sd_notify."""
    sent: list[str] = []

    monkeypatch.setattr(systemd_notify, "notify", lambda message: sent.append(message) or True)
    monkeypatch.setattr(systemd_notify, "watchdog_interval_seconds", lambda: 0.4)
    wd = SystemdWatchdog(config_enabled=True)
    wd._sent = sent  # type: ignore[attr-defined]
    return wd


def test_feeder_runs_on_a_thread_not_the_event_loop(watchdog):
    """The feeder must not be an asyncio task on the calling loop."""
    async def _run():
        assert watchdog.start() is True
        try:
            # Give the feeder a moment to spin up.
            await asyncio.sleep(0.1)
            assert watchdog.feeder_alive is True
            # The thread is NOT the loop's thread, and is not an asyncio task.
            assert watchdog._feeder_thread is not None
            assert watchdog._feeder_thread is not threading.current_thread()
            assert watchdog._task is None, "the feeder must not be an asyncio task"
        finally:
            await watchdog.stop()

    asyncio.run(_run())


def test_feeder_keeps_beating_while_the_loop_is_blocked(watchdog):
    """A blocked event loop must not stop the heartbeat.

    This is the actual failure: the loop is blocked (simulated with a synchronous
    sleep), so an event-loop feeder would stall. The thread feeder must keep
    emitting WATCHDOG=1.
    """
    sent_after: list[list[str]] = []

    async def _run():
        assert watchdog.start() is True
        try:
            await asyncio.sleep(0.1)
            count_before = len(watchdog._sent)
            # Block the event loop for longer than one cadence. This is a
            # DELIBERATE synchronous stall: it is the condition under test, so
            # the blocking sleep is the point (ruff's ASYNC251 is suppressed
            # here on purpose).
            time.sleep(1.0)  # noqa: ASYNC251
            sent_after.append(watchdog._sent[count_before:])
        finally:
            await watchdog.stop()

    asyncio.run(_run())

    after = sent_after[0] if sent_after else []
    beats = [m for m in after if m == "WATCHDOG=1"]
    assert beats, f"the thread feeder must keep beating during a blocked loop, got {after!r}"
    assert not any("unhealthy" in m for m in after), f"a blocked loop must not condemn the gateway: {after!r}"


def test_a_single_late_tick_does_not_condemn(watchdog):
    """Lateness accumulates; one late tick is not a verdict."""
    watchdog._cumulative_lag = 0.0
    # The grace budget floors at one second, so a single 0.5s-late tick is under it.
    budget = watchdog._max_lag()
    assert budget >= 1.0
    ok = watchdog.record_tick(scheduled_at=0.0, now=0.5)
    assert ok is True, "one late tick must not condemn the gateway"
    assert watchdog.unhealthy is False
    assert watchdog._cumulative_lag > 0


def test_sustained_lateness_does_condemn(watchdog):
    """Sustained lateness (past the grace budget) still condemns the gateway."""
    watchdog._cumulative_lag = 0.0
    budget = watchdog._max_lag()
    # Two ticks each late by 0.75s accumulate past the 1.0s budget.
    watchdog.record_tick(scheduled_at=0.0, now=0.75)
    assert watchdog.unhealthy is False
    ok = watchdog.record_tick(scheduled_at=0.0, now=0.75)
    assert ok is False
    assert watchdog.unhealthy is True
    assert budget >= 1.0


def test_an_on_time_tick_resets_the_grace_budget(watchdog):
    """Consecutive stalls count; an on-time tick clears the accumulated lateness."""
    watchdog._cumulative_lag = 0.0
    watchdog.record_tick(scheduled_at=0.0, now=0.3)  # late, accumulates
    assert watchdog._cumulative_lag > 0
    watchdog.record_tick(scheduled_at=10.0, now=10.0)  # on time
    assert watchdog._cumulative_lag == 0.0


def test_stop_ends_the_feeder_thread(watchdog):
    """``stop()`` must wake and join the feeder, not leak it."""

    async def _run():
        assert watchdog.start() is True
        await asyncio.sleep(0.1)
        assert watchdog.feeder_alive is True
        await watchdog.stop()
        assert watchdog.feeder_alive is False

    asyncio.run(_run())
