"""Minimal, optional systemd ``sd_notify`` support for the gateway."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)


def _notify_socket() -> str:
    return os.environ.get("NOTIFY_SOCKET", "").strip() if hasattr(socket, "AF_UNIX") else ""


def notify(message: str) -> bool:
    """Send an sd_notify datagram if systemd configured it; failures never block gateway startup."""
    if not (address := _notify_socket()) or not isinstance(message, str) or not message:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)  # a full receiver buffer must not stall the event loop
            # systemd's ``@abstract`` notation -> Python's leading-NUL address form
            sender.connect("\0" + address[1:] if address.startswith("@") else address)
            sender.send(message.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def extend_start_timeout(seconds: float) -> bool:
    """Ask systemd for ``seconds`` more start time (from now) while a Type=notify boot is still progressing.
    A non-finite budget (an ``inf`` connect-timeout override) sends nothing rather than raising mid-boot."""
    return math.isfinite(seconds) and seconds > 0 and notify(f"EXTEND_TIMEOUT_USEC={int(seconds * 1_000_000)}")


def watchdog_interval_seconds() -> float | None:
    try:
        interval = float(os.environ.get("WATCHDOG_USEC", "") if _notify_socket() else "") / 1e6
    except (TypeError, ValueError):
        return None
    return interval if math.isfinite(interval) and interval > 0 else None


class SystemdWatchdog:
    """Feed systemd while the gateway is alive.

    The feeder runs on a DAEMON THREAD, not on the asyncio event loop. That is the
    whole point of this class.

    The previous implementation drove the feeder with ``asyncio.create_task(self._run())``.
    A watchdog whose heartbeat is produced by the event loop cannot survive the event
    loop being blocked -- and the event loop IS blocked routinely: MCP discovery, long
    cron turns, image analysis, adapter reconnects. When the loop stalls, the feeder
    misses its deadline, systemd concludes the unit is hung, and it SIGKILLs the
    gateway. The gateway then dies holding in-flight work, and the session it was
    serving is lost (2026-11-10: session ``20261110_133919_9e460535`` died mid-turn at
    14:36 and the gateway was SIGKILLed at 14:39).

    systemd's own timer lives in the systemd manager process and never touches the
    gateway's GIL, so a feeder on a separate thread keeps the beat through a stalled
    loop.

    THE LOOP'S LATENESS MUST NEVER SILENCE THE HEARTBEAT.
    -----------------------------------------------------
    ``t_a8395c40`` moved the feeder off the loop and added a cumulative-lateness
    grace budget -- but it left the budget wired to the FEEDER'S OWN death. Once
    ``record_tick()`` condemned the watchdog, ``_feed_forever`` returned, the feeder
    thread ended, and the unit had NO heartbeat source at all. systemd then timed out
    and SIGKILLed the gateway. Eleven watchdog timeouts between 2026-11-08 and
    2026-11-10 (00:19, 20:46, 22:06, 22:27, 21:45, 11:50, 12:17, 13:24, 13:55, 14:39,
    17:02) all followed this shape.

    The fix separates two questions that were previously conflated:

    * "Is the event loop making progress?" -- a DIAGNOSTIC. Lateness accumulates into
      a grace budget and is reported as ``unhealthy``, exactly as before, so the
      operator can see it and so ``t_a8395c40``'s regression tests keep passing.
    * "Should the heartbeat keep going?" -- the FEEDER'S OWN BUSINESS, and the answer
      is ALWAYS YES until the gateway is actually stopping.

    A late loop is a slow loop, not a dead gateway. Killing the gateway because the
    loop is slow converts a degradation into a hard restart that drops in-flight
    turns. The heartbeat must survive it.

    THE MEASURED CAUSE OF THE 2026-11-10 KILLS
    --------------------------------------------
    The kills were NOT caused by GIL starvation from pydantic/anthropic streaming
    (t_5ae9b5c8 measured that path at 12.9 ms, ONCE per stream -- it cannot starve
    anything). They were caused by the gateway's cgroup being memory-throttled: the
    unit sits pegged at ``memory.high`` with ~800 MB resident in swap, the kernel
    logged 54,744 ``memory.high`` throttle events, and every thread in the cgroup is
    descheduled while reclaim runs. A feeder thread parked in ``Event.wait()`` cannot
    be scheduled back in until the throttle lifts.

    Two feeder defects made that fatal:

    1. ``notify()`` returns False on ``OSError`` and the old code ignored it, so a
       dropped datagram was invisible: the feeder believed it beat, systemd received
       nothing, and the unit timed out with no log line anywhere. ``_beat()`` now
       counts failures and logs them.
    2. The feeder could kill itself (above), which removes the heartbeat source
       entirely rather than merely delaying it.

    The feeder is also given a wider margin: the cadence is a fraction of the
    watchdog interval, and the systemd-side deadline is configured well above the
    gateway's real stall budget (see ``docs/wiki/services/gateway-watchdog-and-turn-reset.md``).
    """

    #: Fraction of the watchdog interval between beats. ``0.25`` gives THREE beats of
    #: slack per watchdog period, so a single descheduled wake cannot expire the unit.
    _CADENCE_FRACTION = 0.25

    def __init__(self, *, config_enabled: bool = True, lag_tolerance_seconds: float | None = None,
                 max_lag_seconds: float | None = None):
        self._config_enabled = bool(config_enabled)
        self.interval_seconds = watchdog_interval_seconds()
        self._lag_tolerance_seconds = lag_tolerance_seconds
        self._max_lag_seconds = max_lag_seconds
        self._task: asyncio.Task[None] | None = None
        self._feeder_thread: threading.Thread | None = None
        self._feeder_stop = threading.Event()
        self._unhealthy = self._stopping = self._stopping_notified = False
        self._cumulative_lag = 0.0
        #: Beats whose sd_notify datagram could not be sent. A non-zero value means
        #: systemd may not have received a heartbeat even though the feeder tried.
        self._notify_failures = 0
        self._beats_sent = 0

    enabled = property(lambda self: self._config_enabled and self.interval_seconds is not None)
    unhealthy = property(lambda self: self._unhealthy)
    task = property(lambda self: self._task)
    feeder_alive = property(lambda self: bool(self._feeder_thread and self._feeder_thread.is_alive()))
    notify_failures = property(lambda self: self._notify_failures)
    beats_sent = property(lambda self: self._beats_sent)

    def _lag_tolerance(self) -> float:
        default = max(0.1, (self.interval_seconds or 0.0) * 0.25)
        with contextlib.suppress(TypeError, ValueError):
            value = float(self._lag_tolerance_seconds)
            return max(0.0, value) if math.isfinite(value) else default
        return default

    def _max_lag(self) -> float:
        """Cumulative lateness that condemns the gateway.

        Defaults to the systemd watchdog interval itself: a loop that stays behind by
        more than a whole watchdog period, cumulatively, is wedged rather than busy.
        """
        default = max(1.0, float(self.interval_seconds or 0.0))
        value = self._max_lag_seconds
        if value is None:
            return default
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, numeric) if math.isfinite(numeric) else default

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._feeder_thread is not None and self._feeder_thread.is_alive():
            return True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._stopping = self._unhealthy = self._stopping_notified = False
        self._cumulative_lag = 0.0
        self._notify_failures = 0
        self._beats_sent = 0
        self._feeder_stop = threading.Event()
        # The feeder is a THREAD, not an asyncio task: it must keep beating while the
        # event loop is blocked. See the class docstring for the incident this fixes.
        self._feeder_thread = threading.Thread(
            target=self._feed_forever, name="hermes-systemd-watchdog", daemon=True
        )
        self._feeder_thread.start()
        return True

    def ready(self, status: str = "Gateway running") -> bool:
        safe_status = str(status or "Gateway running").replace("\n", " ")
        return self.enabled and notify(f"READY=1\nSTATUS={safe_status}")

    def _beat(self, message: str = "WATCHDOG=1") -> bool:
        """Send one heartbeat datagram and COUNT the failures.

        ``notify()`` swallows ``OSError`` and returns False. The old code ignored that
        return value entirely, so a dropped datagram was completely invisible: the
        feeder believed it had beaten, systemd received nothing, and the unit timed
        out with no log line to explain it. Counting and logging the failures turns a
        silent 180-second mystery into a visible metric.
        """
        if notify(message):
            self._beats_sent += 1
            return True
        self._notify_failures += 1
        if self._notify_failures in (1, 10) or self._notify_failures % 100 == 0:
            logger.warning(
                "systemd watchdog beat %d failed to send (total=%d, sent=%d); "
                "systemd may expire the unit if the datagrams keep failing",
                self._notify_failures, self._notify_failures, self._beats_sent,
            )
        return False

    def record_tick(self, *, scheduled_at: float, now: float) -> bool:
        """Feed systemd only when the event loop woke within its lag budget.

        A LATE tick no longer condemns the gateway on its own: lateness accumulates
        into a grace budget (``_max_lag()``) and only sustained lateness does. The
        loop legitimately stalls for tens of seconds during MCP discovery, long cron
        turns and adapter reconnects; condemning the gateway on the first late tick
        turned a transient stall into a restart loop.

        The return value is a DIAGNOSTIC, not a verdict on the heartbeat: it says
        "the loop is late", not "stop beating". The feeder keeps beating either way
        (see ``_feed_forever``).
        """
        if not self.enabled or self._stopping:
            return False
        try:
            lag = float(now) - float(scheduled_at)
        except (TypeError, ValueError):
            lag = float("inf")
        if not math.isfinite(lag) or lag > self._lag_tolerance():
            self._cumulative_lag += lag if math.isfinite(lag) else self._max_lag()
            if self._cumulative_lag >= self._max_lag():
                if not self._unhealthy:
                    self._unhealthy = True
                    notify("STATUS=watchdog unhealthy: event loop progress is late")
                    logger.warning(
                        "event loop progress is late (cumulative lag %.1fs over budget %.1fs); "
                        "the heartbeat CONTINUES -- the gateway is slow, not dead",
                        self._cumulative_lag, self._max_lag(),
                    )
                return False
            # Not condemned yet: keep beating. The feeder thread is what keeps the
            # unit alive; the loop's lateness is only diagnostic.
            self._beat()
            return True
        self._cumulative_lag = 0.0
        self._beat()
        return True

    def _feed_forever(self) -> None:
        """Beat systemd from a dedicated thread until stopped.

        This is the thread body that replaces the old ``asyncio`` ``_run`` coroutine.
        It never touches the event loop, so a blocked loop cannot stop the heartbeat.

        It also never stops because the loop is late. That was the defect behind
        eleven SIGKILLs between 2026-11-08 and 2026-11-10: ``record_tick`` returned
        False on sustained lateness, this loop returned with it, the feeder thread
        ended, and the unit was left with no heartbeat source -- so systemd killed
        the gateway 180 seconds later, mid-turn.

        The ONLY conditions that stop the beat are an explicit ``stop()`` and an
        unexpected exception (which is logged rather than silently ending the thread).
        """
        if self.interval_seconds is None:
            return
        cadence = max(0.01, self.interval_seconds * self._CADENCE_FRACTION)
        scheduled_at = time.monotonic() + cadence
        while not self._stopping:
            delay = max(0.0, scheduled_at - time.monotonic())
            if self._feeder_stop.wait(delay):
                return
            now = time.monotonic()
            try:
                self.record_tick(scheduled_at=scheduled_at, now=now)
            except Exception:  # a broken tick must not end the heartbeat
                logger.exception("systemd watchdog tick failed; continuing to beat")
                self._beat()
            scheduled_at += cadence
            if scheduled_at < now:
                # We overslept by more than a full cadence: resynchronise rather
                # than trying to catch up with a burst of beats.
                scheduled_at = now + cadence

    async def stop(self) -> None:
        """Stop feeding systemd and emit ``STOPPING=1`` at most once."""
        self._stopping = True
        # Wake the feeder thread out of its wait so it exits promptly.
        self._feeder_stop.set()
        feeder = self._feeder_thread
        if feeder is not None and feeder is not threading.current_thread():
            # Bounded join: the feeder wakes on the event, so this returns promptly.
            feeder.join(timeout=max(1.0, float(self.interval_seconds or 3.0)))
        self._feeder_thread = None
        # Legacy handle: kept so callers that read ``.task`` do not break.
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()  # no-op on a finished task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        if self.enabled and not self._stopping_notified:
            notify("STOPPING=1")
            self._stopping_notified = True
