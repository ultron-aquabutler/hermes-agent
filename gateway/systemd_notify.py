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
    """Send an sd_notify datagram if systemd configured it; failures never block gateway startup.

    Returns True only when the datagram was handed to the kernel. That is NOT proof
    that systemd accepted it -- ``sd_notify`` is fire-and-forget and systemd's
    manager is the only authority on acceptance. The watchdog therefore treats a
    successful send as "we tried", never as "systemd heard us".
    """
    if not isinstance(message, str) or not message:
        return False
    address = _notify_socket()
    if not address:
        # No notify socket at all: systemd is not supervising this process, or the
        # environment lost the variable. Say so ONCE per distinct reason rather
        # than failing silently -- a silent no-op here is indistinguishable from
        # a healthy heartbeat, which is how a dead feeder hides.
        logger.warning(
            "sd_notify: no NOTIFY_SOCKET configured; dropping %s. systemd is not "
            "receiving notifications from this process.", message.split("=", 1)[0])
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)  # a full receiver buffer must not stall the event loop
            # systemd's ``@abstract`` notation -> Python's leading-NUL address form
            sender.connect("\0" + address[1:] if address.startswith("@") else address)
            sender.send(message.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError) as exc:
        # A failed heartbeat is NOT the same as a healthy heartbeat: systemd's timer
        # keeps running whether or not we told it we are alive, so a silent send
        # failure turns into a watchdog timeout 180s later with nothing in the log
        # to explain it. Log it at WARNING (not ERROR): the caller may be a
        # best-effort status message, and the watchdog itself re-raises the alarm.
        logger.warning("sd_notify send failed (%s): %s: %s", message.split("=", 1)[0],
                       type(exc).__name__, exc)
        return False


def watchdog_interval_seconds() -> float | None:
    try:
        interval = float(os.environ.get("WATCHDOG_USEC", "") if _notify_socket() else "") / 1e6
    except (TypeError, ValueError):
        return None
    return interval if math.isfinite(interval) and interval > 0 else None


def _watched_target(original, on_done):
    """Wrap a thread target so the thread's ending is reported instead of ignored.

    Python prints NOTHING when a thread ends -- not on a normal return, not on an
    exception. A feeder thread that died therefore left the gateway running,
    logging as if nothing were wrong, while systemd's timer quietly counted
    down to the SIGABRT.

    The wrapper calls ``on_done(reason)`` with a description of HOW the thread
    ended, so the caller can tell an expected end (the gateway asked it to stop)
    from an unexplained one.

    The TARGET is wrapped, not ``Thread.run``: replacing ``run`` would make the
    wrapper call itself, which is infinite recursion.
    """
    def _watched(*args, **kwargs):
        try:
            original(*args, **kwargs)
        except BaseException as exc:
            on_done(f"{type(exc).__name__}: {exc}")
            return
        on_done("returned: the feeder thread returned without being stopped")

    return _watched


class SystemdWatchdog:
    """Feed systemd from a thread that does not live on the event loop.

    systemd's ``WatchdogSec`` is a **process** liveness contract: while the process
    keeps saying "alive", systemd leaves it alone; when the beats stop, systemd
    reclaims the unit. It is not an event-loop progress contract.

    The previous implementation fed systemd from an ``asyncio`` task on the gateway's
    MAIN event loop, which made the watchdog's own heartbeat depend on the very thing
    it was monitoring. On 2026-11-03 00:12:07 UTC the gateway was SIGABRTed by
    systemd's watchdog after the main thread stalled: the faulthandler snapshot shows
    the main thread in ``Garbage-collecting``, and the last loop heartbeat was
    00:09:26. Because the heartbeat task could not run while the main thread could not
    run the loop, ``WATCHDOG=1`` stopped and systemd killed a gateway that was about
    to recover on its own. A main-thread stall is strictly worse than the
    GIL-holding C call this class was written for: it stops the loop outright instead
    of merely contending for the GIL.

    The heartbeat therefore runs on its own daemon thread, which proves liveness at the
    level systemd actually checks — the process is running and the thread is
    completing its work. A main-thread GC pass or a busy event loop cannot silence it.

    The systemd backstop is NOT weakened:

    * A genuinely wedged process cannot run ANY Python thread, because the GIL is
      held. The feeder thread stops sending beats and systemd reclaims the unit
      exactly as before.
    * Event-loop progress is a separate concern with a separate detector:
      ``gateway.shutdown_watchdog.start_loop_liveness_watchdog`` already runs OFF the
      loop, probes the loop via ``call_soon_threadsafe``, and escalates to a
      faulthandler dump plus exit 75 after sustained misses. That detector — not the
      systemd heartbeat — is the thing that catches a stalled loop, and it can report
      *why* the gateway died.
    * A late or missing beat is still recorded (``unhealthy``) and surfaced through
      ``STATUS=`` so the failure is diagnosable rather than silent.

    A SINGLE late tick must not condemn the gateway. The loop legitimately stalls for
    tens of seconds during MCP discovery, long cron turns, and adapter reconnects;
    killing the service on one late tick turns a transient stall into a restart loop
    (#t_b3e3f2cb). Lateness therefore accumulates against a grace budget, and only
    sustained lateness stops the heartbeat.
    """

    def __init__(self, *, config_enabled: bool = True, lag_tolerance_seconds: float | None = None,
                 max_lag_seconds: float | None = None, notify_fn=None):
        self._config_enabled = bool(config_enabled)
        self.interval_seconds = watchdog_interval_seconds()
        self._lag_tolerance_seconds = lag_tolerance_seconds
        self._max_lag_seconds = max_lag_seconds
        # Injectable notifier: the thread calls it directly, so a test can observe the
        # beats without a real NOTIFY_SOCKET.
        self._notify = notify_fn if notify_fn is not None else notify
        self._task: asyncio.Task[None] | None = None
        self._feeder_thread: threading.Thread | None = None
        self._feeder_stop = threading.Event()
        self._unhealthy = self._stopping = self._stopping_notified = False
        self._cumulative_lag = 0.0
        self._last_beat_monotonic: float | None = None
        # Set when the feeder thread dies or fails. A dead feeder is the gateway's
        # most dangerous silent state: systemd keeps counting down its 180s timer,
        # the gateway keeps running and logging as if nothing were wrong, and the
        # unit is SIGABRTed with no explanation in the journal.
        self._failure: str | None = None
        self._tick_failures = 0
        self._heartbeat_failures = 0
        # Consecutive beats that produced NO heartbeat at all (the send failed).
        # The feeder is alive but is NOT feeding, which is the state that must
        # never be reported as healthy.
        self._consecutive_missed_heartbeats = 0
        # Set when the feeder thread ended for a reason we did NOT ask for.
        self._feeder_ended = False

    enabled = property(lambda self: self._config_enabled and self.interval_seconds is not None)
    unhealthy = property(lambda self: self._unhealthy)
    task = property(lambda self: self._task)
    feeder_alive = property(lambda self: bool(self._feeder_thread and self._feeder_thread.is_alive()))
    last_beat_monotonic = property(lambda self: self._last_beat_monotonic)
    failed = property(lambda self: self._failure is not None)
    failure_reason = property(lambda self: self._failure)
    heartbeat_failures = property(lambda self: self._heartbeat_failures)
    tick_failures = property(lambda self: self._tick_failures)
    consecutive_missed_beats = property(
        lambda self: self._consecutive_missed_heartbeats)
    consecutive_missed_heartbeats = property(
        lambda self: self._consecutive_missed_heartbeats)
    last_heartbeat_monotonic = property(lambda self: self._last_beat_monotonic)
    stopping = property(lambda self: self._stopping)

    @property
    def healthy(self) -> bool:
        """True only when the feeder is running AND actually feeding.

        Deliberately strict. A feeder that is alive but not sending heartbeats is
        NOT healthy: systemd will time the unit out on schedule and the gateway
        will be SIGABRTed with nothing in the journal. A feeder that is dead is
        NOT healthy either, and it carries the reason.

        This is a statement about the FEEDER, never about systemd: a successful
        send means "handed to the kernel", not "systemd accepted it".
        """
        return (
            self._failure is None
            and not self._unhealthy
            and not self._stopping
            and self._heartbeat_failures == 0
            and self._consecutive_missed_heartbeats == 0
            and not self._feeder_ended
        )

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
        return numeric if math.isfinite(numeric) and numeric > 0 else default

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
        self._failure = None
        self._tick_failures = 0
        self._heartbeat_failures = 0
        self._consecutive_missed_heartbeats = 0
        self._feeder_ended = False
        self._feeder_stop.clear()
        # spawn_context_thread (not a bare threading.Thread) so the feeder keeps the
        # caller's profile scope: it runs inside a multiplexed gateway that serves
        # several profiles, and a bare thread would inherit the launch profile's
        # contextvars instead of the caller's.
        from agent.memory_provider import spawn_context_thread

        # A thread that dies is INVISIBLE unless someone watches it: Python prints
        # nothing for a thread that ends. The target is wrapped so a dead feeder
        # is always reported with a reason, instead of the gateway sitting there
        # unsupervised until systemd reclaims the unit 180s later.
        self._feeder_thread = spawn_context_thread(
            _watched_target(self._feed_forever, self._on_feeder_done),
            name="hermes-systemd-watchdog", daemon=True)
        self._feeder_thread.start()
        return True

    def _on_feeder_done(self, reason: str) -> None:
        """Report the feeder's death. Called by the feeder watcher when the thread ends.

        Three endings must be distinguished:

        * the thread RAISED           -> an unexpected fault; report it.
        * the thread RETURNED NORMALLY -> the feeder is dead. The only legitimate
          normal return is the one ``stop()`` asked for (which sets ``_stopping``
          first) or the unhealthy path (which sets ``_unhealthy``). Anything else
          is an unexplained death and must be reported.
        * the feeder could not even be started -> also a death.

        A feeder that ends is a pending SIGABRT: systemd keeps counting down its
        180s timer and the gateway keeps running as if nothing were wrong.
        """
        if self._stopping:
            return  # stop() asked for this end; it reports STOPPING=1 itself.
        if self._unhealthy:
            return  # record_tick already sent STATUS=watchdog unhealthy.
        self._feeder_ended = True
        self._failure = reason
        logger.critical(
            "Systemd watchdog feeder ended unexpectedly (%s); systemd will now time the "
            "unit out after %ss because nothing is feeding it. The gateway stays up but "
            "is no longer supervised.", reason, self.interval_seconds)
        # One-shot operator-visible stamp: the journal line is 180s before the kill, so
        # the operator needs to be able to SEE that the gateway is unsupervised NOW.
        self._publish_unsupervised_state()

    def _publish_unsupervised_state(self) -> None:
        """Record that the gateway is no longer supervised. Best-effort; never raises.

        OFF THE EVENT LOOP and NON-BLOCKING: ``write_runtime_status`` takes a
        process-wide lock and then BLOCKS on the background writer -- measured
        2.5 s of stall with a slow writer, in a gateway whose whole problem is
        stalls. ``publish_runtime_status`` hands the write to the background
        writer and returns.

        Only top-level fields are stamped: ``error_code``/``error_message``/
        ``needs_attention`` in ``gateway.status`` are PLATFORM-scoped and are only
        written when a ``platform=`` argument is passed, so a platform-less call
        silently drops them. The full failure reason goes to the journal, where it
        belongs.
        """
        reason = self._failure or "unknown"
        try:
            from gateway.status import publish_runtime_status
        except Exception:
            logger.debug("Failed to import publish_runtime_status", exc_info=True)
            return

        def _publish() -> None:
            try:
                publish_runtime_status(
                    gateway_state="degraded",
                    exit_reason="systemd_watchdog_feeder_failed")
            except Exception:
                logger.debug("Failed to stamp the systemd watchdog feeder failure",
                             exc_info=True)

        # The reason goes to the journal (the status record carries only the code).
        logger.critical("systemd watchdog feeder is unsupervised: %s", reason)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _publish()
            return
        # run_in_executor keeps the disk write off the loop thread. The task itself
        # is fire-and-forget, so a stuck writer cannot wedge the loop or block shutdown.
        loop.run_in_executor(None, _publish)

    def ready(self, status: str = "Gateway running") -> bool:
        safe_status = str(status or "Gateway running").replace("\n", " ")
        return self.enabled and self._notify(f"READY=1\nSTATUS={safe_status}")

    def _feed_forever(self) -> None:
        """Send ``WATCHDOG=1`` on a fixed cadence from a dedicated thread.

        The cadence is systemd's own contract: feed at least twice per ``WatchdogSec``
        (``WATCHDOG_USEC`` is already half of it, so half of that is a safe period).

        This thread is the systemd heartbeat's ONLY source. It deliberately does NOT
        consult the event loop: the point is that a main-thread stall cannot silence
        the beat. Process-level wedging is still caught, because a process holding the
        GIL cannot run this thread either.
        """
        interval = self.interval_seconds
        if interval is None:
            return
        cadence = max(0.01, interval / 2.0)
        while not self._feeder_stop.is_set() and not self._stopping and not self._unhealthy:
            if self._feeder_stop.wait(timeout=cadence):
                return
            if self._stopping or self._unhealthy:
                return
            self._send_beat()

    def _send_beat(self) -> bool:
        """Emit one heartbeat and record whether a heartbeat ACTUALLY went out.

        ``notify()`` swallows socket errors and returns False. Before this, a
        broken notify socket was indistinguishable from a successful heartbeat, so
        the gateway kept running for 180s believing it was supervised while
        systemd's timer quietly expired.

        A successful return means "the datagram was handed to the kernel", NOT
        "systemd accepted it": ``sd_notify`` is fire-and-forget and systemd's
        manager is the only authority on acceptance.
        """
        if self._unhealthy or self._stopping:
            return False
        try:
            sent = bool(self._notify("WATCHDOG=1"))
        except Exception:
            # notify() catches the socket errors it knows about; anything else
            # (a monkeypatched socket, an unexpected error from the socket
            # module) must not be allowed to escape and kill the feeder -- and
            # must not be counted as a successful heartbeat either.
            self._heartbeat_failures += 1
            self._consecutive_missed_heartbeats += 1
            logger.warning("sd_notify heartbeat raised an unexpected error", exc_info=True)
            return False
        if not sent:
            self._heartbeat_failures += 1
            self._consecutive_missed_heartbeats += 1
            logger.critical(
                "Systemd watchdog heartbeat was not delivered (failure #%d); systemd will "
                "time this unit out unless a later heartbeat reaches it.",
                self._heartbeat_failures)
            return False
        self._heartbeat_failures = 0
        self._consecutive_missed_heartbeats = 0
        self._last_beat_monotonic = time.monotonic()
        self._last_heartbeat_monotonic = self._last_beat_monotonic
        return True

    def record_tick(self, *, scheduled_at: float, now: float) -> bool:
        """Feed systemd only when the event loop woke within its lag budget.

        A late tick ACCUMULATES lateness instead of condemning the gateway on the
        first one: the loop gets a grace budget of ``_max_lag()`` seconds of
        cumulative lateness before the watchdog gives up on it. Feeding systemd on a
        late tick keeps the unit alive during a transient stall (MCP discovery, a long
        cron turn, an adapter reconnect); only a persistently late loop — the actual
        wedge — stops the heartbeat and lets systemd reclaim the unit.

        This path is retained for callers that drive the watchdog from the loop; the
        gateway itself uses the off-loop feeder so a main-thread stall cannot stop
        the heartbeat.
        """
        if not self.enabled or self._stopping or self._unhealthy:
            return False
        try:
            lag = float(now) - float(scheduled_at)
        except (TypeError, ValueError):
            lag = float("inf")
        if not math.isfinite(lag) or lag < 0:
            # A clock that went backwards (or a bogus value) is not evidence of a wedge.
            lag = 0.0
        if lag > self._lag_tolerance():
            self._cumulative_lag += lag
            if self._cumulative_lag >= self._max_lag():
                self._unhealthy = True
                self._notify("STATUS=watchdog unhealthy: event loop progress is late")
                return False
            # Still late, but within the grace budget: keep systemd fed.
            return self._send_beat()
        self._cumulative_lag = 0.0
        return self._send_beat()

    async def _run(self) -> None:
        """Loop-driven heartbeat.

        Kept for callers that want the heartbeat tied to event-loop progress. The
        gateway does NOT use this path: tying the systemd heartbeat to the event loop
        is what let a main-thread stall silence the beat on 2026-11-03.
        """
        if self.interval_seconds is None:
            return
        cadence = max(0.01, self.interval_seconds / 2.0)
        loop = asyncio.get_running_loop()
        scheduled_at = loop.time() + cadence
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stopping and not self._unhealthy:
                await asyncio.sleep(max(0.0, scheduled_at - loop.time()))
                now = loop.time()
                if not self.record_tick(scheduled_at=scheduled_at, now=now):
                    return
                scheduled_at += cadence
                if scheduled_at < now:
                    scheduled_at = now + cadence

    async def stop(self) -> None:
        """Stop feeding systemd and emit ``STOPPING=1`` at most once."""
        self._stopping = True
        self._feeder_stop.set()
        feeder = self._feeder_thread
        if feeder is not None and feeder is not threading.current_thread():
            # Bounded join: the feeder wakes at most one cadence after the stop
            # event, so this returns promptly. It is joined so a restarted watchdog
            # never has two threads sending beats.
            with contextlib.suppress(RuntimeError):
                feeder.join(timeout=max(1.0, (self.interval_seconds or 1.0)))
        self._feeder_thread = None
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()  # no-op on a finished task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        if self.enabled and not self._stopping_notified:
            self._notify("STOPPING=1")
            self._stopping_notified = True
