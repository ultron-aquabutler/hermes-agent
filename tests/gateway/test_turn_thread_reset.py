"""Regression: a partially-initialized module must not kill the turn worker.

Observed on 2026-11-10 (t_a8395c40): ``gateway/turn_executor.py::_park`` called
``reset_reused_thread_state()``, which raised

    AttributeError: partially initialized module 'tools.terminal_tool_sudo' from
    '/home/serveradmin/.hermes/hermes-agent/tools/terminal_tool_sudo.py' has no
    attribute '_sudo_password_cache_lock' (most likely due to a circular import).

``_park`` treats a failed reset as "this worker is not reusable" and retires the
thread, so a turn that was merely parked mid-flight died with its state half-dropped.
The gateway logged "turn worker state reset failed; retiring the thread" and the
session stalled instead of continuing.

The module is only PARTIALLY initialized when it is observed inside an import
cycle. That is exactly what these tests model.
"""

from __future__ import annotations

import sys
import types

import pytest

from gateway.turn_thread_reset import reset_reused_thread_state


@pytest.fixture
def clean_sudo_module(monkeypatch):
    """Install a fake ``tools.terminal_tool_sudo`` and yield the module object."""
    fake = types.ModuleType("tools.terminal_tool_sudo")
    monkeypatch.setitem(sys.modules, "tools.terminal_tool_sudo", fake)
    return fake


def test_missing_module_is_a_noop(monkeypatch):
    """A module that was never imported holds no state: nothing to drop."""
    monkeypatch.delitem(sys.modules, "tools.terminal_tool_sudo", raising=False)
    reset_reused_thread_state()  # must not raise


def test_partially_initialized_module_does_not_raise(clean_sudo_module):
    """The 2026-11-10 crash: module present, module-level names not yet bound.

    ``clean_sudo_module`` deliberately has NO ``_sudo_password_cache_lock`` and
    NO ``_sudo_password_cache`` -- the exact shape of a module caught mid-import.
    """
    assert not hasattr(clean_sudo_module, "_sudo_password_cache_lock")
    assert not hasattr(clean_sudo_module, "_sudo_password_cache")

    reset_reused_thread_state()  # must not raise


def test_partially_initialized_module_with_only_lock(clean_sudo_module):
    """Half-bound is still half-bound: a lock without its cache must not raise."""
    import threading

    clean_sudo_module._sudo_password_cache_lock = threading.Lock()
    assert not hasattr(clean_sudo_module, "_sudo_password_cache")

    reset_reused_thread_state()  # must not raise


def test_partially_initialized_module_with_only_cache(clean_sudo_module):
    """A cache without its lock must not raise either."""
    clean_sudo_module._sudo_password_cache = {}
    assert not hasattr(clean_sudo_module, "_sudo_password_cache_lock")

    reset_reused_thread_state()  # must not raise


def test_fully_initialized_module_still_drops_the_thread_entry():
    """The fix must not weaken the normal path: the thread entry IS dropped."""
    import threading

    fake = types.ModuleType("tools.terminal_tool_sudo")
    fake._sudo_password_cache_lock = threading.Lock()
    # The cache key is scoped to the CURRENT thread id -- that is the entry the
    # reset is responsible for dropping.
    stale_key = f"thread:{threading.get_ident()}"
    fake._sudo_password_cache = {stale_key: "secret", "session:other": "keep-me"}
    old = sys.modules.get("tools.terminal_tool_sudo")
    sys.modules["tools.terminal_tool_sudo"] = fake
    try:
        reset_reused_thread_state()
    finally:
        if old is None:
            sys.modules.pop("tools.terminal_tool_sudo", None)
        else:
            sys.modules["tools.terminal_tool_sudo"] = old

    assert stale_key not in fake._sudo_password_cache
    # A session-scoped entry belongs to a different owner and must survive.
    assert fake._sudo_password_cache["session:other"] == "keep-me"


def test_turn_executor_park_survives_a_broken_reset(monkeypatch):
    """``_park`` must not raise when the reset fails.

    ``_park`` already swallows a failed reset (it logs and retires the worker), so
    the caller-level contract is "never propagate". The SUDO-module regression is
    the case where the reset raised for a reason that should NOT have retired the
    worker at all -- covered by the tests above.
    """
    from gateway import turn_executor
    import gateway.turn_thread_reset as mod

    class _Boom(RuntimeError):
        pass

    def _explode():
        raise _Boom("reset failed for an unrelated reason")

    monkeypatch.setattr(mod, "reset_reused_thread_state", _explode)

    executor = turn_executor._UnboundedThreadExecutor()
    worker = turn_executor._Worker()
    # _park must not propagate the failure to its caller.
    executor._park(worker)
