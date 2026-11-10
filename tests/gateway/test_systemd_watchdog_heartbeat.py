"""Regression: the watchdog heartbeat must survive a stalled event loop AND a
failing sd_notify socket.

Background (kanban ``t_5ae9b5c8``): between 2026-11-08 and 2026-11-10 the gateway
was SIGKILLed by systemd's watchdog eleven times. Each kill dropped an in-flight
turn. ``t_a8395c40`` had moved the feeder off the event loop onto a daemon
thread, which fixed the LOOP-stall case but not these.

The measured cause was NOT GIL starvation from pydantic/anthropic streaming
(that path costs 12.9 ms and runs ONCE per stream). The cause was the gateway's
cgroup being memory-throttled: the unit sits pegged at ``memory.high`` with
~800 MB resident in swap and the kernel logged 54,744 throttle events, so every
thread in the cgroup is descheduled while reclaim runs.

Two feeder defects turned that stall fatal:

1. The feeder could kill ITSELF. ``record_tick`` returned False on sustained
   lateness, ``_feed_forever`` returned with it, the feeder thread ended, and
   the unit was left with no heartbeat source at all. systemd then timed out
   180 seconds later and SIGKILLed the gateway mid-turn.
2. ``notify()`` swallows ``OSError`` and returns False, and the old code
   ignored that return value. A dropped datagram was completely invisible:
   the feeder believed it beat, systemd received nothing, and the unit timed
   out with no log line anywhere to explain it.

These tests pin the corrected contract:

  1. sustained loop lateness marks the watchdog ``unhealthy`` (the diagnostic)
     but the feeder thread KEEPS BEATING (the heartbeat).
  2. a failing ``notify()`` is counted and surfaced, not swallowed.
  3. the feeder keeps a healthy margin: several beats fit inside one watchdog
     period, so a single descheduled wake cannot expire the unit.
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


# ---------------------------------------------------------------------------
# 1. The heartbeat survives sustained loop lateness
# ---------------------------------------------------------------------------


def test_sustained_lateness_marks_unhealthy_but_the_feeder_keeps_beating(watchdog):
    """A late loop is a SLOW loop. It must not stop the heartbeat.

    This is the defect that produced eleven SIGKILLs. ``record_tick`` reports
    the lateness as ``unhealthy`` (a diagnostic), but the feeder thread must
    survive it -- otherwise the unit loses its only heartbeat source and
    systemd kills the gateway 180 seconds later.
    """
    async def _run():
        assert watchdog.start() is True
        try:
            await asyncio.sleep(0.1)
            assert watchdog.feeder_alive is True

            # Drive the loop hard past the grace budget: every tick is late by
            # far more than the tolerance, so the watchdog goes unhealthy.
            for _ in range(6):
                watchdog.record_tick(scheduled_at=0.0, now=10.0)
            assert watchdog.unhealthy is True, "sustained lateness must still be reported"

            # THE POINT: the feeder thread must still be alive and still beating.
            beats_before = len(watchdog._sent)
            await asyncio.sleep(0.5)  # > one cadence at a 0.4s interval
            assert watchdog.feeder_alive is True, (
                "the feeder must not die when the event loop is late -- "
                "a dead feeder means systemd kills the gateway mid-turn"
            )
            assert len(watchdog._sent) > beats_before, (
                "the heartbeat must continue while the loop is late"
            )
            assert any("WATCHDOG=1" in m for m in watchdog._sent[beats_before:])
        finally:
            await watchdog.stop()

    asyncio.run(_run())


def test_the_feeder_keeps_beating_after_the_watchdog_goes_unhealthy(watchdog):
    """Even once unhealthy, the beat count must keep climbing."""
    async def _run():
        assert watchdog.start() is True
        try:
            await asyncio.sleep(0.1)
            for _ in range(6):
                watchdog.record_tick(scheduled_at=0.0, now=10.0)
            assert watchdog.unhealthy is True
            first = watchdog.beats_sent
            await asyncio.sleep(0.6)
            assert watchdog.beats_sent > first
        finally:
            await watchdog.stop()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. A failing notify() must be visible
# ---------------------------------------------------------------------------


def test_a_failing_notify_is_counted_not_swallowed(monkeypatch):
    """A dropped datagram must be counted and logged.

    ``notify()`` returns False on ``OSError``. The old code ignored that, so the
    feeder beat into the void and systemd timed out with no explanation.
    """
    sent: list[str] = []

    def failing(message: str) -> bool:
        sent.append(message)
        return False

    monkeypatch.setattr(systemd_notify, "notify", failing)
    monkeypatch.setattr(systemd_notify, "watchdog_interval_seconds", lambda: 0.4)
    wd = SystemdWatchdog(config_enabled=True)

    async def _run():
        assert wd.start() is True
        try:
            await asyncio.sleep(0.1)
            before = wd.notify_failures
            await asyncio.sleep(0.5)
            assert wd.notify_failures > before, (
                "a failing sd_notify must be counted -- otherwise the unit times "
                "out mysteriously"
            )
            assert wd.beats_sent == 0, "no beat can be counted as sent when the send failed"
        finally:
            await wd.stop()

    asyncio.run(_run())


def test_notify_failure_is_logged(monkeypatch, caplog):
    """The first failure must produce a log line, not silence."""
    monkeypatch.setattr(systemd_notify, "notify", lambda message: False)
    monkeypatch.setattr(systemd_notify, "watchdog_interval_seconds", lambda: 0.4)
    wd = SystemdWatchdog(config_enabled=True)

    with caplog.at_level("WARNING"):
        wd.record_tick(scheduled_at=0.0, now=0.0)
    assert any("watchdog beat" in r.message for r in caplog.records), (
        f"expected a warning about the failed beat, got {caplog.records!r}"
    )


def test_a_healthy_notify_is_counted_as_sent(monkeypatch):
    """The happy path still counts beats."""
    monkeypatch.setattr(systemd_notify, "notify", lambda message: True)
    monkeypatch.setattr(systemd_notify, "watchdog_interval_seconds", lambda: 0.4)
    wd = SystemdWatchdog(config_enabled=True)
    wd.record_tick(scheduled_at=0.0, now=0.0)
    assert wd.beats_sent == 1
    assert wd.notify_failures == 0


# ---------------------------------------------------------------------------
# 3. The feeder keeps a healthy margin
# ---------------------------------------------------------------------------


def test_the_cadence_leaves_several_beats_per_watchdog_period(monkeypatch):
    """At least three beats must fit in one watchdog period.

    The old cadence was ``interval / 2`` -- exactly two. One descheduled wake
    could therefore push the next beat past systemd's deadline with no slack.
    """
    monkeypatch.setattr(systemd_notify, "notify", lambda message: True)
    monkeypatch.setattr(systemd_notify, "watchdog_interval_seconds", lambda: 180.0)
    wd = SystemdWatchdog(config_enabled=True)
    cadence = 180.0 * wd._CADENCE_FRACTION
    assert 180.0 / cadence >= 3.0, "the feeder must beat at least three times per watchdog period"


def test_the_watchdog_interval_is_the_systems_contract_not_a_guess():
    """The systemd-side deadline is documented, not implied.

    ``WatchdogSec`` must be generous enough that the gateway's real stall budget
    (a memory-throttled cgroup descheduling every thread for minutes) cannot
    expire it. The drop-in that sets it lives next to the unit.

    The drop-in file name is not fixed by any contract, so glob for it rather
    than hard-coding a name that a future edit may change.
    """
    from pathlib import Path

    drop_in_dir = Path.home() / ".config/systemd/user/hermes-gateway.service.d"
    candidates = sorted(drop_in_dir.glob("*.conf")) if drop_in_dir.exists() else []
    watchdog_drops = [p for p in candidates if "WatchdogSec=" in p.read_text()]
    if not watchdog_drops:
        pytest.skip("watchdog drop-in is not installed on this host")
    assert watchdog_drops, "a drop-in sets WatchdogSec but it declares no value"

    value = 0
    for path in watchdog_drops:
        text = path.read_text()
        assert "WatchdogSec=" in text, "the drop-in must set WatchdogSec explicitly"
        value = int(text.split("WatchdogSec=")[1].split()[0].strip())
        # 180s was the old value and it expired eleven times in three days.
        assert value >= 600, f"WatchdogSec={value}s is too tight for a memory-throttled gateway"


# ---------------------------------------------------------------------------
# 4. The thread must not die silently
# ---------------------------------------------------------------------------


def test_the_feeder_thread_survives_a_raising_tick(watchdog, monkeypatch):
    """A broken tick must not end the heartbeat thread."""

    def boom(**_kwargs):
        raise RuntimeError("tick exploded")

    async def _run():
        assert watchdog.start() is True
        try:
            await asyncio.sleep(0.1)
            monkeypatch.setattr(watchdog, "record_tick", boom)
            await asyncio.sleep(0.3)
            assert watchdog.feeder_alive is True, (
                "a raising tick must not end the feeder thread -- the beat must continue"
            )
        finally:
            await watchdog.stop()

    asyncio.run(_run())


def test_stop_still_ends_the_feeder(watchdog):
    """The explicit stop path must keep working."""

    async def _run():
        assert watchdog.start() is True
        await asyncio.sleep(0.1)
        assert watchdog.feeder_alive is True
        await watchdog.stop()
        assert watchdog.feeder_alive is False

    asyncio.run(_run())


def test_the_feeder_thread_is_a_daemon_named_for_observability(watchdog):
    """The thread name is how an operator finds it in a stack dump."""

    async def _run():
        assert watchdog.start() is True
        try:
            thread = watchdog._feeder_thread
            assert thread is not None
            assert thread.name == "hermes-systemd-watchdog"
            assert thread.daemon is True
        finally:
            await watchdog.stop()

    asyncio.run(_run())
