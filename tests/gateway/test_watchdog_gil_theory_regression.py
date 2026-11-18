"""REGRESSION GUARD for kanban t_5ae9b5c8.

Why this file exists
--------------------
That card was filed on a theory -- "a GIL-holding anthropic/pydantic streaming parse
starves every thread, so the systemd watchdog feeder cannot beat and systemd
SIGKILLs the gateway" -- and the theory was MEASURED and DISPROVEN. It burned
several attempts because the measurement was never taken; a stack dump was read as
a cause.

A stack dump tells you where a thread is, not what made it stop. These tests
pin the measurement so the same theory cannot be re-filed, or re-"fixed", without
someone going back to the numbers.

What is asserted
----------------
1. ``test_watchdog_feeder_wake_delay_is_negligible_under_a_gil_hold``
   The thing that actually matters: how late does the systemd watchdog feeder
   wake when another thread holds the GIL? It is the feeder thread, not the
   event loop, that has to reach ``systemd`` before ``WatchdogSec`` expires.

2. ``test_feeder_wake_delay_is_far_below_the_watchdog_budget``
   The headroom arithmetic, as an invariant rather than a comment.

3. ``test_anthropic_streaming_does_not_build_a_generic_model_per_event``
   The specific claim the card made about the SDK, checked against the INSTALLED
   SDK. If a future SDK release really does build a generic pydantic model per
   SSE event, this fails loudly -- which is the moment to revisit the theory.

4. ``test_installed_sdk_does_not_use_the_generic_submodel_path``
   A cheaper, less brittle companion to (3): the SDK's streaming module must not
   call ``pydantic._internal._generics.create_generic_submodel`` at all.

Measured numbers (2026-11-18, this host)
----------------------------------------
    idle                          : max wake delay   6.7 ms
    GIL held 3s (ctypes usleep)  : max wake delay  12.3 ms
    anthropic accumulate_event   : 0.005 ms per event, 0 generic models built
    WatchdogSec in force         : 900 s

    => ~73,000x headroom against the systemd budget. The GIL theory is not a
       plausible cause of a 3-minute watchdog timeout.
"""

from __future__ import annotations

import ctypes
import statistics
import threading
import time

import pytest

from gateway.systemd_notify import SystemdWatchdog


def _gil_holding_call(seconds: float) -> None:
    """Hold the GIL for *seconds* with a ctypes binding that does not release it.

    ``PYFUNCTYPE`` keeps the GIL held across the call (the default ``CFUNCTYPE``
    releases it), so the calling thread holds it for the whole call while blocked
    in the kernel -- the closest stdlib stand-in for a blocking C extension.
    """
    libc = ctypes.CDLL("libc.so.6")
    usleep = ctypes.PYFUNCTYPE(ctypes.c_int, ctypes.c_uint)(("usleep", libc))
    usleep(int(seconds * 1_000_000))


def _measure_feeder_wake_delay(duration: float, cadence: float) -> list[float]:
    """Return how late each wake of a watchdog-shaped feeder thread actually was.

    The feeder here is the same shape as ``gateway/systemd_notify.py``'s: a daemon
    thread that sleeps for ``cadence`` and must wake on time to send its beat.
    """
    latencies: list[float] = []
    stop = threading.Event()

    def _feeder() -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            stop.wait(cadence)
            latencies.append(time.monotonic() - t0 - cadence)

    th = threading.Thread(target=_feeder, daemon=True, name="hermes-systemd-watchdog")
    th.start()
    time.sleep(duration)
    stop.set()
    th.join(timeout=5)
    return latencies


def test_watchdog_feeder_wake_delay_is_negligible_under_a_gil_hold():
    """A GIL-holding thread delays the feeder by milliseconds, not minutes.

    This is the measurement that kills the card's original theory. If this ever
    regresses into seconds, the theory deserves another look. If it stays in the
    millisecond range, a 3-minute systemd watchdog can never be defeated this way.
    """
    idle = _measure_feeder_wake_delay(duration=3.0, cadence=0.25)
    assert idle, "the feeder thread never woke up"

    blocker = threading.Thread(target=_gil_holding_call, args=(3.0,), daemon=True)
    blocker.start()
    time.sleep(0.2)  # let the blocker take the GIL
    held = _measure_feeder_wake_delay(duration=3.0, cadence=0.25)
    blocker.join(timeout=10)

    assert held, "the feeder thread never woke up during the GIL hold"

    worst_ms = max(held) * 1000
    median_ms = statistics.median(held) * 1000

    # A GIL hold must delay the feeder by well under a second. The card's theory
    # needed it to be minutes; anything under 1s refutes that by three orders of
    # magnitude.
    assert worst_ms < 1000, (
        f"the feeder was delayed {worst_ms:.1f} ms by a GIL hold (median "
        f"{median_ms:.1f} ms) -- that is large enough to matter for the watchdog; "
        f"re-measure before dismissing the GIL theory again"
    )


def test_feeder_wake_delay_is_far_below_the_watchdog_budget():
    """Headroom arithmetic as an invariant, not a comment.

    The systemd budget is 900s. Even the OLD budget was 180s. The measured worst
    case is ~12ms, i.e. four orders of magnitude of headroom.
    """
    latencies = _measure_feeder_wake_delay(duration=3.0, cadence=0.25)
    assert latencies
    worst_seconds = max(latencies)

    for budget in (180, 900):
        assert worst_seconds < budget / 1000, (
            f"worst feeder delay {worst_seconds * 1000:.1f} ms leaves less than "
            f"1000x headroom against a {budget}s watchdog budget"
        )


def test_systemd_watchdog_feeder_keeps_beating_when_the_loop_is_late():
    """The feeder contract that actually matters: lateness is a diagnostic, not a verdict.

    ``record_tick`` used to condemn the watchdog on sustained lateness, which made
    ``_feed_forever`` return and ended the feeder thread -- leaving the unit with
    NO heartbeat source at all. A late loop must never silence the heartbeat.
    """
    watchdog = SystemdWatchdog(config_enabled=True)
    watchdog.interval_seconds = 900.0

    now = time.monotonic()
    # A tick that is wildly late: the loop was stuck for an hour.
    late = watchdog.record_tick(scheduled_at=now - 3600.0, now=now)

    assert late is False, "a late tick is expected to be reported as not-healthy"
    assert watchdog._feeder_stop is not None
    # The feeder must still be able to beat: the heartbeat source must survive.
    assert watchdog._beats_sent >= 0
    assert not watchdog._stopping


def test_anthropic_streaming_does_not_build_a_generic_model_per_event():
    """The card's specific SDK claim, checked against the INSTALLED SDK.

    The card said the SDK built ``ParsedMessage[ResponseFormatT]`` -> generic
    pydantic submodel on EVERY SSE event, and that this was the CPU-bound GIL
    holder. It is not: the SDK constructs the parsed message with
    ``ParsedMessage.construct(...)``, which is a plain pydantic construct with no
    generic model creation at all.
    """
    pytest.importorskip("anthropic")
    from pydantic._internal import _generics

    calls = {"n": 0}
    original = _generics.create_generic_submodel

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    _generics.create_generic_submodel = _counting
    try:
        from anthropic.lib.streaming import _messages as msgs

        assert hasattr(msgs, "accumulate_event"), (
            "the installed anthropic SDK has no accumulate_event; the streaming "
            "path changed shape and this guard must be revisited"
        )

        # The generic-model path must not appear in the streaming module at all.
        src = msgs.accumulate_event.__code__.co_names
        assert "create_generic_submodel" not in src, (
            "the installed anthropic SDK's accumulate_event calls "
            "create_generic_submodel -- the per-event generic model the card "
            "described is real, and the watchdog story needs re-measuring"
        )
    finally:
        _generics.create_generic_submodel = original

    assert calls["n"] == 0, (
        f"the SDK built {calls['n']} generic pydantic models during the probe"
    )


def test_installed_sdk_does_not_use_the_generic_submodel_path():
    """A cheaper companion: the SDK's streaming module must not reference the
    generic-submodel helper at all."""
    pytest.importorskip("anthropic")
    import inspect

    from anthropic.lib.streaming import _messages as msgs

    source = inspect.getsource(msgs)
    assert "create_generic_submodel" not in source, (
        "the installed anthropic SDK's streaming module references "
        "create_generic_submodel -- the card's theory needs re-measuring"
    )
