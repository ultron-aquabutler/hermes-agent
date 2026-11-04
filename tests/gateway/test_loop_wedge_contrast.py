"""Base-vs-fix contrast for the loop-wedge detection contract (t_dd79272f).

The question under test: does moving the systemd heartbeat off the event loop
leave a genuinely stuck event loop undetected?

To measure the BASE behaviour faithfully, this harness runs a REAL asyncio event
loop in a DEDICATED thread, and then genuinely stops that thread from running
the loop — which is exactly what happened on 2026-11-03 00:12:07, when the main
thread was in ``Garbage-collecting`` and the loop could not run a single
callback.

Two implementations are compared under the IDENTICAL stall:

  BASE : the pre-2026-11-03 implementation — the heartbeat is an asyncio task on
         the event loop, and ``record_tick`` is the only source of beats.
  FIX  : the PR-9 implementation — an off-loop feeder thread sends the beats.

Measured outcome:

  * BASE — a stuck loop stops the heartbeat, so systemd's ``WatchdogSec`` expires
    and the unit is reclaimed. The wedge IS caught, but by systemd (SIGABRT),
    with no diagnosis of WHY the loop was stuck.
  * FIX  — the feeder keeps beating, so systemd does NOT reclaim the unit. The
    wedge is caught ONLY by the off-loop loop-liveness watchdog.

So the fix does not mask the wedge PROVIDED the loop-liveness watchdog is armed.
The residual risk (guard disabled) is pinned in ``test_loop_wedge_safety.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytestmark = pytest.mark.platforms("linux")


class _LoopThread:
    """A real asyncio event loop running in a dedicated thread.

    ``stop_the_loop()`` makes the thread genuinely stop executing the loop — the
    faithful stand-in for "the main thread is in Garbage-collecting and the loop
    cannot run a single callback".
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="loop-under-test", daemon=True)
        self._gate = threading.Event()
        self._gate.set()  # open: the loop runs

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.loop.is_running():
            time.sleep(0.005)

    def stop_the_loop(self) -> None:
        """Close the gate: the thread stops running the loop from now on.

        A callback is scheduled that parks the loop thread inside the gate, so
        the loop executes no further callbacks until ``open_the_loop()``. This is
        exactly the state the gateway was in at 00:12:07.
        """
        self._gate.clear()
        self.loop.call_soon_threadsafe(self._block_in_gate)

    def _block_in_gate(self) -> None:
        self._gate.wait(timeout=30.0)

    def open_the_loop(self) -> None:
        self._gate.set()

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout=timeout)


def _install_base_impl(monkeypatch):
    """Return a SystemdWatchdog subclass whose beats come ONLY from the loop."""
    import gateway.systemd_notify as notify_mod

    class _BaseWatchdog(notify_mod.SystemdWatchdog):
        """Heartbeat driven by the event loop only — the pre-fix behaviour."""

        def start(self) -> bool:
            if not self.enabled:
                return False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
            self._stopping = self._unhealthy = self._stopping_notified = False
            self._task = loop.create_task(self._run())
            return True

    return _BaseWatchdog


def _watchdog_factory(monkeypatch, *, cls=None):
    calls: list[str] = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setenv("WATCHDOG_USEC", "200000")

    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    factory = cls or notify_mod.SystemdWatchdog
    return factory(), calls


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _drive_on(loop_thread: _LoopThread, coro_factory) -> None:
    """Run a coroutine factory on the loop-under-test."""
    def _bootstrap() -> None:
        asyncio.ensure_future(coro_factory(), loop=loop_thread.loop)

    loop_thread.loop.call_soon_threadsafe(_bootstrap)


@pytest.mark.asyncio
async def test_base_implementation_stops_beating_when_the_loop_is_stuck(monkeypatch):
    """BASE: a stuck loop silences the heartbeat — systemd WOULD reclaim the unit.

    Reproduced with a REAL event loop in a dedicated thread that is genuinely
    stopped from running the loop. Same failure shape as 2026-11-03: the loop
    cannot run a single callback.
    """
    base_cls = _install_base_impl(monkeypatch)
    watchdog, calls = _watchdog_factory(monkeypatch, cls=base_cls)

    loop_thread = _LoopThread()
    loop_thread.start()

    # The base watchdog needs a RUNNING loop at start() time, so drive it there.
    def _start() -> None:
        watchdog.start()

    _drive_on(loop_thread, _start)

    try:
        # The base implementation waits a FULL cadence before its first beat.
        assert _wait_until(lambda: calls.count("WATCHDOG=1") >= 1, timeout=5.0), calls
        beats_before = calls.count("WATCHDOG=1")

        # Genuinely stop the loop from running.
        loop_thread.stop_the_loop()
        time.sleep(0.2)

        # While the loop is stuck, the base implementation produces NO new beats.
        time.sleep(0.8)
        assert calls.count("WATCHDOG=1") == beats_before, calls
    finally:
        loop_thread.open_the_loop()
        await watchdog.stop()
        loop_thread.join()


@pytest.mark.asyncio
async def test_fix_implementation_keeps_beating_when_the_loop_is_stuck(monkeypatch):
    """FIX: a stuck loop does NOT silence the heartbeat.

    The direct contrast with the test above: the SAME stalled loop, the same
    cadence, and the beat count still advances. This is what makes the systemd
    ``WatchdogSec`` a PROCESS-liveness contract rather than a loop-progress
    contract — and therefore why loop-wedge detection had to keep its own,
    independent detector.
    """
    watchdog, calls = _watchdog_factory(monkeypatch)

    loop_thread = _LoopThread()
    loop_thread.start()

    try:
        assert watchdog.start() is True
        assert _wait_until(lambda: calls.count("WATCHDOG=1") >= 1, timeout=5.0), calls
        beats_before = calls.count("WATCHDOG=1")

        # Genuinely stop the loop from running.
        loop_thread.stop_the_loop()
        time.sleep(0.2)

        # The off-loop feeder keeps systemd fed.
        assert _wait_until(lambda: calls.count("WATCHDOG=1") > beats_before, timeout=5.0), calls
        assert watchdog.unhealthy is False
        assert watchdog.feeder_alive is True
    finally:
        loop_thread.open_the_loop()
        await watchdog.stop()
        loop_thread.join()
