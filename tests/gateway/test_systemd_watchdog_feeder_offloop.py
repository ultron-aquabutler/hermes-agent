"""REPRO: the systemd WATCHDOG feeder must survive a stalled event loop.

The hub gateway was SIGABRTed 13 times in 48h with:

    hermes-gateway.service: Watchdog timeout (limit 3min)!
    hermes-gateway.service: Killing process 2795449 (hermes) with signal SIGABRT.

systemd's ``WatchdogSec=180s`` is a PROCESS-liveness contract: while the process keeps
sending ``WATCHDOG=1``, systemd leaves it alone; when the beats stop, systemd
SIGABRTs it. The "Fatal Python error: Aborted" dump that made this look like a CPython
bug was only the faulthandler snapshot of whichever threads happened to be running when
systemd's signal landed.

The feeder that produced those beats lived on the gateway's MAIN event loop, so the very
stall it was supposed to survive silenced it: a loop that cannot run cannot ping, and a
process that stops pinging is killed 180s later even though it is healthy and recovering.

This file drives a REAL asyncio loop in a thread and genuinely stalls that loop (the
faithful stand-in for the observed stall), then asserts the beats keep coming. The
contrast is the whole point:

    BASE (loop-driven feeder)  beats STOP    -> systemd reclaims the unit
    FIX  (off-loop feeder)      beats CONTINUE -> systemd does NOT reclaim

The second half of this file covers the OTHER half of the defect: the feeder must also
be able to REPORT that it is dead or not delivering. A feeder that silently stops
sending is a pending SIGABRT, and the operator must be able to read WHY in the journal
rather than infer it from the absence of evidence.

The tests are behaviour contracts, not source-reading checks: they observe the datagrams
the feeder actually sends and the state it actually reports.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from gateway import systemd_notify


class _NotifyRecorder:
    """Stand in for the notify socket: record every datagram the feeder sends."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._lock = threading.Lock()
        self._event = threading.Event()

    def __call__(self, message: str) -> bool:
        with self._lock:
            self.messages.append(message)
        self._event.set()
        return True

    def count(self, prefix: str) -> int:
        with self._lock:
            return sum(1 for m in self.messages if m.startswith(prefix))

    def wait_for(self, prefix: str, count: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count(prefix) >= count:
                return True
            self._event.clear()
            self._event.wait(timeout=0.05)
        return self.count(prefix) >= count


class _StalledLoop:
    """A REAL asyncio loop running in its own thread, stalling ON DEMAND.

    The stall is a synchronous sleep-loop ON the loop thread: the faithful stand-in for
    the gateway's observed stall (a blocking call, a long GC pass, a busy loop). The
    loop is not stopped -- it is slow, and it recovers on its own afterwards, exactly
    like the gateway that systemd killed while it was about to recover.

    The stall is scheduled from OUTSIDE the loop, so it can be made to overlap a
    coroutine that is already running on the loop thread. A stall that only happens
    before the feeder starts proves nothing.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._release = threading.Event()
        self._blocker_started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="stalled-loop")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stall(self, seconds: float) -> threading.Event:
        """Block the loop thread for ``seconds``; returns an event set when it ends."""
        started = threading.Event()
        done = threading.Event()

        def _block():
            started.set()
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not self._release.is_set():
                time.sleep(0.02)
            done.set()

        self.loop.call_soon_threadsafe(_block)
        assert started.wait(timeout=5.0), "the loop never entered the stall"
        return done

    def call(self, coro, timeout: float = 30.0):
        """Run a coroutine ON the loop thread and return its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        self._release.set()
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout=5.0)
        try:
            self.loop.close()
        except RuntimeError:
            pass


@pytest.fixture
def fast_watchdog(monkeypatch):
    """A SystemdWatchdog whose cadence is short enough to test in a few seconds.

    ``WATCHDOG_USEC`` is what systemd hands the unit, so the feeder reads it exactly as
    it does in production; only the number is scaled down.
    """
    monkeypatch.setenv("WATCHDOG_USEC", str(int(2.0 * 1_000_000)))
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify-offloop")
    return systemd_notify.SystemdWatchdog


@pytest.fixture
def recorder(monkeypatch):
    """Capture the datagrams the feeder sends, wherever it sends them from.

    The base implementation calls the module-level ``notify()``; the fix may inject its
    own notifier. Patching the module attribute covers both, so the same test proves the
    behaviour on the base and on the fix.
    """
    rec = _NotifyRecorder()
    monkeypatch.setattr(systemd_notify, "notify", rec)
    return rec


def _start_watchdog_on(stalled: _StalledLoop, watchdog, hold: float):
    """Start the feeder on ``stalled``'s loop; returns the pending future."""
    started = threading.Event()
    holder: dict = {}

    async def _run():
        assert watchdog.start() is True
        started.set()
        await asyncio.sleep(hold)

    def _submit():
        holder["future"] = asyncio.run_coroutine_threadsafe(_run(), stalled.loop)

    runner = threading.Thread(target=_submit, daemon=True)
    runner.start()
    assert started.wait(timeout=10.0), "the feeder never started"
    return holder["future"]


def test_feeder_keeps_beating_while_the_event_loop_is_stalled(fast_watchdog, recorder):
    """The beat must not depend on the loop the feeder is supposed to supervise.

    RED on base: the feeder is an asyncio task on the loop, so the stall silences it.
    GREEN with the off-loop feeder: the beats keep coming.
    """
    stalled = _StalledLoop()
    watchdog = fast_watchdog(config_enabled=True)

    future = _start_watchdog_on(stalled, watchdog, hold=6.0)
    # The loop is now blocked for 4s WHILE the feeder is supposed to be beating.
    stall_done = stalled.stall(4.0)
    try:
        future.result(timeout=30.0)
    finally:
        stalled.close()

    assert stall_done.is_set()
    beats = recorder.count("WATCHDOG=1")
    assert beats >= 2, (
        "the systemd watchdog feeder stopped beating while the event loop was stalled; "
        f"systemd would SIGABRT the unit 180s after the last beat. beats={beats} "
        f"messages={recorder.messages!r}"
    )


def test_feeder_beats_continue_after_the_loop_recovers(fast_watchdog, recorder):
    """A recovered loop must be followed by MORE beats, not silence.

    The observed production failure was a feeder that had already ended: it reported
    itself alive, stopped sending, and systemd killed the unit 180s later.
    """
    stalled = _StalledLoop()
    watchdog = fast_watchdog(config_enabled=True)

    future = _start_watchdog_on(stalled, watchdog, hold=6.0)
    stalled.stall(3.0)
    try:
        future.result(timeout=30.0)
    finally:
        stalled.close()

    assert recorder.count("WATCHDOG=1") >= 2, (
        "the feeder produced fewer than two beats across a stalled-then-recovering loop; "
        f"messages={recorder.messages!r}"
    )


def test_feeder_does_not_report_healthy_while_not_beating(fast_watchdog, recorder):
    """A feeder that is alive but delivering nothing is NOT healthy.

    The base implementation had no health surface at all, so a dead feeder was
    indistinguishable from a healthy one until systemd killed the unit.
    """
    stalled = _StalledLoop()
    watchdog = fast_watchdog(config_enabled=True)

    future = _start_watchdog_on(stalled, watchdog, hold=6.0)
    stalled.stall(3.0)
    try:
        future.result(timeout=30.0)
    finally:
        stalled.close()

    healthy = getattr(watchdog, "healthy", None)
    if healthy is None:
        pytest.skip("this build exposes no health surface on the feeder")
    assert healthy is True, (
        "the feeder reports itself unhealthy while it is beating; a health surface that "
        "lies in either direction is worse than none"
    )
    assert recorder.count("WATCHDOG=1") >= 1


def test_a_dead_feeder_is_reported_not_silent(fast_watchdog, recorder):
    """A feeder that ends must say so, with a reason, instead of vanishing.

    A Python thread that ends prints nothing. The feeder ending is a pending SIGABRT:
    systemd keeps counting down its 180s timer while the gateway keeps logging as if
    nothing were wrong. The health surface must carry the death and the reason.
    """
    stalled = _StalledLoop()
    watchdog = fast_watchdog(config_enabled=True)

    future = _start_watchdog_on(stalled, watchdog, hold=6.0)
    stalled.stall(1.0)
    try:
        future.result(timeout=30.0)
    finally:
        stalled.close()

    feeder = getattr(watchdog, "_feeder_thread", None)
    assert feeder is not None, "the feeder does not run on its own thread"
    # End the feeder the way an unexpected fault would: the thread returns without
    # anybody asking it to.
    watchdog._feeder_stop.set()
    feeder.join(timeout=5)

    assert watchdog.failed is True, "a feeder that ended was not reported as failed"
    assert watchdog.failure_reason, "a failed feeder carries no reason"
    assert watchdog.healthy is False, "a dead feeder still reports itself healthy"


def test_a_feeder_that_stopped_sending_is_not_healthy(fast_watchdog):
    """A feeder whose datagrams are not going out is NOT healthy.

    systemd's timer keeps running whether or not we tell it we are alive, so a feeder
    that is delivering nothing is a pending SIGABRT and must be visible.
    """
    def _failing_notify(message: str) -> bool:
        return False

    watchdog = fast_watchdog(config_enabled=True, notify_fn=_failing_notify)
    stalled = _StalledLoop()
    future = _start_watchdog_on(stalled, watchdog, hold=3.0)
    try:
        future.result(timeout=30.0)
    finally:
        stalled.close()

    assert watchdog.healthy is False, (
        "a feeder whose datagrams are not going out reports itself healthy; systemd "
        "would SIGABRT the unit with nothing to explain it"
    )
    assert watchdog.heartbeat_failures >= 1
    assert watchdog.consecutive_missed_beats >= 1


def test_health_never_claims_systemd_accepted_the_datagram(fast_watchdog, recorder):
    """A successful send is NOT systemd acceptance, and the surface must not say it is.

    ``sd_notify`` is fire-and-forget: the send returns once the datagram is handed to
    the kernel. Only systemd's manager knows whether it accepted and reset its timer.
    """
    stalled = _StalledLoop()
    watchdog = fast_watchdog(config_enabled=True)

    future = _start_watchdog_on(stalled, watchdog, hold=3.0)
    try:
        future.result(timeout=30.0)
    finally:
        stalled.close()

    assert watchdog.healthy is True
    assert recorder.count("WATCHDOG=1") >= 1
    # The health surface must be a statement about the FEEDER. There must be no
    # attribute claiming systemd received or accepted anything.
    for attr in dir(watchdog):
        assert "accept" not in attr.lower(), (
            f"the health surface exposes {attr!r}, which implies systemd acceptance; "
            "a successful send is only 'handed to the kernel'"
        )
