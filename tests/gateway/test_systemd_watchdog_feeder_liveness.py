"""The systemd watchdog feeder must never die SILENTLY.

The hub gateway was SIGABRTed by systemd 13 times in 48h with:

    hermes-gateway.service: Watchdog timeout (limit 3min)!
    hermes-gateway.service: Killing process 2795449 (hermes) with signal SIGABRT.

and nothing in the journal said WHY the heartbeat stopped. systemd's ``WatchdogSec``
is a process-liveness contract: when the beats stop, systemd reclaims the unit. A
feeder that stops feeding is therefore a pending SIGABRT, and it must be visible in
the journal and on the health surface rather than discovered by the kill.

These are behaviour contracts on what the feeder REPORTS, not on its shape:

* a feeder that stopped sending is not healthy;
* a feeder that died is not healthy, and says why;
* a feeder that is beating is healthy;
* the health surface never claims systemd ACCEPTED a datagram.

The complementary contract -- that the feeder keeps beating while the EVENT LOOP is
stalled -- lives in ``test_systemd_watchdog_feeder_offloop.py``. This file covers
what the feeder says about itself; that one covers when the beats actually happen.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from gateway import systemd_notify


class _Recorder:
    """Records the datagrams the feeder sends and can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail
        self._lock = threading.Lock()

    def __call__(self, message: str) -> bool:
        if self.fail:
            return False
        with self._lock:
            self.messages.append(message)
        return True

    def count(self, prefix: str) -> int:
        with self._lock:
            return sum(1 for m in self.messages if m.startswith(prefix))


@pytest.fixture
def watchdog_factory(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", str(int(1_000_000)))
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify-liveness")
    return systemd_notify.SystemdWatchdog


def _start_and_wait(watchdog, seconds: float) -> None:
    """Start the feeder on a real running loop and let it beat for ``seconds``.

    ``start()`` requires a running event loop (it is called from the gateway's
    async startup), so the loop must actually be RUNNING for the coroutine to be
    scheduled. A bare ``new_event_loop()`` would never schedule it and the wait
    would time out.
    """
    loop = asyncio.new_event_loop()
    holder: dict = {}

    async def _run():
        watchdog.start()
        await asyncio.sleep(seconds)

    def _loop_thread():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    runner = threading.Thread(target=_loop_thread, daemon=True, name="watchdog-test-loop")
    runner.start()

    def _submit():
        holder["future"] = asyncio.run_coroutine_threadsafe(_run(), loop)

    submitter = threading.Thread(target=_submit, daemon=True)
    submitter.start()
    try:
        # The submitter sets holder["future"]; wait for it to be in place before
        # reading it, otherwise this races the submitter's first statement.
        submitter.join(timeout=5)
        assert "future" in holder, "the coroutine was never submitted to the loop"
        holder["future"].result(timeout=seconds + 20)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        submitter.join(timeout=5)
        loop.close()


def test_a_feeder_that_stopped_sending_is_not_healthy(watchdog_factory):
    """The health surface must not report a dead feeder as healthy.

    RED on base: there was no health surface at all, so a feeder that had stopped
    sending was indistinguishable from a healthy one until systemd killed the unit.
    """
    recorder = _Recorder(fail=True)
    watchdog = watchdog_factory(config_enabled=True, notify_fn=recorder)
    _start_and_wait(watchdog, seconds=2.5)

    assert watchdog.healthy is False, (
        "a feeder whose datagrams are not going out reports itself healthy; "
        "systemd would SIGABRT the unit with nothing to explain it"
    )
    assert watchdog.heartbeat_failures >= 1
    assert watchdog.consecutive_missed_beats >= 1


def test_a_feeder_that_is_beating_is_healthy(watchdog_factory):
    """A working feeder reports healthy — the health surface must not cry wolf."""
    recorder = _Recorder()
    watchdog = watchdog_factory(config_enabled=True, notify_fn=recorder)
    _start_and_wait(watchdog, seconds=2.5)

    assert recorder.count("WATCHDOG=1") >= 1, "the feeder never beat"
    assert watchdog.healthy is True, (
        "the feeder is beating but reports itself unhealthy; a health surface that "
        "lies in either direction is worse than none"
    )


def test_a_dead_feeder_is_reported_not_silent(watchdog_factory):
    """A feeder that dies must say so, with a reason.

    This is the whole point of the failure-reporting layer: the unit is going to be
    SIGABRTed 180s after the last beat, and the operator has to be able to read WHY
    in the journal rather than infer it from the absence of evidence.

    Python prints nothing when a thread ends, so a feeder that died used to leave the
    gateway running and logging as if nothing were wrong.
    """
    recorder = _Recorder()
    watchdog = watchdog_factory(config_enabled=True, notify_fn=recorder)

    # start() needs a running event loop, so start the feeder from inside one.
    loop = asyncio.new_event_loop()
    holder: dict = {}

    async def _run():
        watchdog.start()
        await asyncio.sleep(0.2)

    def _loop_thread():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    runner = threading.Thread(target=_loop_thread, daemon=True, name="watchdog-dead-loop")
    runner.start()

    def _submit():
        holder["future"] = asyncio.run_coroutine_threadsafe(_run(), loop)

    submitter = threading.Thread(target=_submit, daemon=True)
    submitter.start()
    try:
        submitter.join(timeout=5)
        assert "future" in holder, "the coroutine was never submitted to the loop"
        holder["future"].result(timeout=30)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        submitter.join(timeout=5)
        loop.close()

    feeder = getattr(watchdog, "_feeder_thread", None)
    assert feeder is not None, "the feeder does not run on its own thread"

    # End the feeder the way an unexpected fault would: the thread returns without
    # anybody asking it to. The stop event is the only lever a thread has, and it is
    # NOT the same as stop() -- stop() also sets _stopping, which is the end the
    # gateway asked for and must stay quiet.
    watchdog._feeder_stop.set()
    feeder.join(timeout=5)
    assert not feeder.is_alive(), "the feeder thread did not end"

    # The thread ended; the watchdog must now be unhealthy AND carry a reason.
    assert watchdog.failed is True, "a feeder that ended was not reported as failed"
    assert watchdog.failure_reason, "a failed feeder carries no reason"
    assert watchdog.healthy is False


def test_a_stopped_feeder_is_quiet(watchdog_factory):
    """A feeder the gateway asked to stop must NOT be reported as a failure.

    The mirror of the death test: an expected end is not a fault, and the journal
    must not cry wolf on every ordinary gateway shutdown.
    """
    recorder = _Recorder()
    watchdog = watchdog_factory(config_enabled=True, notify_fn=recorder)

    async def _run():
        watchdog.start()
        await asyncio.sleep(0.2)
        await watchdog.stop()

    loop = asyncio.new_event_loop()
    holder: dict = {}

    def _loop_thread():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    runner = threading.Thread(target=_loop_thread, daemon=True, name="watchdog-stop-loop")
    runner.start()

    def _submit():
        holder["future"] = asyncio.run_coroutine_threadsafe(_run(), loop)

    submitter = threading.Thread(target=_submit, daemon=True)
    submitter.start()
    try:
        submitter.join(timeout=5)
        assert "future" in holder, "the coroutine was never submitted to the loop"
        holder["future"].result(timeout=30)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        submitter.join(timeout=5)
        loop.close()

    assert watchdog.failed is False, (
        "a feeder the gateway stopped on purpose was reported as failed"
    )
    assert watchdog.failure_reason is None


def test_health_never_claims_systemd_accepted_the_datagram(watchdog_factory):
    """A successful send is NOT systemd acceptance, and the surface must not say it is.

    ``sd_notify`` is fire-and-forget: the send returns once the datagram is handed to
    the kernel. Only systemd's manager knows whether it accepted and reset its timer.
    """
    recorder = _Recorder()
    watchdog = watchdog_factory(config_enabled=True, notify_fn=recorder)
    _start_and_wait(watchdog, seconds=2.5)

    assert watchdog.healthy is True
    # The health surface must be a statement about the FEEDER. There must be no
    # attribute claiming systemd received or accepted anything.
    for attr in dir(watchdog):
        assert "accept" not in attr.lower(), (
            f"the health surface exposes {attr!r}, which implies systemd acceptance; "
            "a successful send is only 'handed to the kernel'"
        )
