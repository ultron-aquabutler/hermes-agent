"""Regression tests: a process that SHARES the gateway's cgroup must be scoped.

Card t_6b5fe961 (2026-11-11).

Background
----------
``tools/process_registry._is_supervised_gateway_process()`` answers "am I the
gateway process that owns the live PID file?". That is the right question for
the *self-kill guards* -- and the wrong question for *cgroup isolation*.

Every descendant of the gateway shares the gateway's service cgroup, and
therefore its ``memory.high`` / ``memory.max`` budget:

* a ``agent.managed_worker`` (a plain ``subprocess.Popen`` child), and
* any command an agent runs through the terminal tool.

Measured 2026-11-11: a ``pytest tests/ -q`` started through the terminal tool
ran for 88 minutes inside ``hermes-gateway.service`` holding **3,156 MiB of
swap**, while the gateway's own working set sat pinned at ``memory.high``.
The gateway's event loop then starved and ``gateway/shutdown_watchdog.py``
fired -- killing the gateway.

The scope machinery already existed and worked; the gate simply excluded the
processes that needed it. ``_shares_gateway_cgroup()`` asks the question that
actually matters for memory isolation.

These tests pin BOTH halves of the contract:

* a gateway descendant (shares the cgroup) IS scoped, and
* an unrelated CLI that merely inherited the markers is NOT scoped.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tools import process_registry


@pytest.fixture()
def registry(monkeypatch, tmp_path):
    """A minimal ProcessRegistry with the real spawn path, no checkpoint writes."""
    from tools.process_registry import ProcessRegistry

    reg = ProcessRegistry()
    monkeypatch.setattr(reg, "_write_checkpoint", MagicMock())
    return reg


def _fake_popen_capture():
    """Return (fake_popen, captured) where captured["argv"] gets the argv."""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 4321
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _FakePopen, captured


def _scoped_argv(captured):
    """True when the argv was wrapped in a systemd-run scope."""
    argv = captured.get("argv") or []
    return any(str(a).endswith("systemd-run") for a in argv)


# --------------------------------------------------------------------------
# The predicate itself
# --------------------------------------------------------------------------


class TestSharesGatewayCgroup:
    """``_shares_gateway_cgroup`` -- do we spend the gateway's memory budget?"""

    def test_true_when_in_the_gateway_cgroup(self, monkeypatch):
        monkeypatch.setattr(
            process_registry, "_enclosing_cgroup_path", lambda: "/user.slice/hermes-gateway.service"
        )
        monkeypatch.setattr(
            process_registry,
            "_gateway_unit_cgroup_path",
            lambda: "/user.slice/hermes-gateway.service",
        )
        assert process_registry._shares_gateway_cgroup() is True

    def test_false_when_in_a_different_cgroup(self, monkeypatch):
        monkeypatch.setattr(
            process_registry, "_enclosing_cgroup_path", lambda: "/user.slice/hermes-worker-x.scope"
        )
        monkeypatch.setattr(
            process_registry,
            "_gateway_unit_cgroup_path",
            lambda: "/user.slice/hermes-gateway.service",
        )
        assert process_registry._shares_gateway_cgroup() is False

    def test_trailing_slash_is_not_a_difference(self, monkeypatch):
        monkeypatch.setattr(
            process_registry, "_enclosing_cgroup_path", lambda: "/user.slice/hermes-gateway.service/"
        )
        monkeypatch.setattr(
            process_registry,
            "_gateway_unit_cgroup_path",
            lambda: "/user.slice/hermes-gateway.service",
        )
        assert process_registry._shares_gateway_cgroup() is True

    def test_false_when_the_cgroup_cannot_be_read(self, monkeypatch):
        monkeypatch.setattr(process_registry, "_enclosing_cgroup_path", lambda: None)
        assert process_registry._shares_gateway_cgroup() is False

    def test_never_raises_when_systemctl_is_missing(self, monkeypatch):
        monkeypatch.setattr(
            process_registry, "_enclosing_cgroup_path", lambda: "/user.slice/hermes-gateway.service"
        )
        monkeypatch.setattr(
            process_registry, "_gateway_unit_cgroup_path", lambda: None
        )
        assert process_registry._shares_gateway_cgroup() is False


# --------------------------------------------------------------------------
# The behaviour: scope a gateway descendant
# --------------------------------------------------------------------------


class TestGatewayDescendantIsScoped:
    """A gateway descendant shares the gateway's budget, so it gets its own scope."""

    @pytest.fixture()
    def _descendant(self, monkeypatch):
        """This process is a gateway DESCENDANT: not the gateway, but in its cgroup."""
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        # Not the gateway process itself -> the PID-ownership gate stays False.
        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda *, cleanup_stale=False: os.getpid() + 1,
        )
        monkeypatch.setattr(
            process_registry, "_shares_gateway_cgroup", lambda: True
        )
        monkeypatch.setattr(
            process_registry, "_is_supervised_gateway_process", lambda: False
        )

    @pytest.mark.parametrize("use_pty", [False, True])
    def test_descendant_command_is_wrapped_in_a_scope(
        self, registry, monkeypatch, _descendant, use_pty
    ):
        monkeypatch.setattr(process_registry, "_find_shell", lambda: "/bin/bash")
        monkeypatch.setattr(
            process_registry, "_systemd_run_user_scope_available", lambda: True
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

        if use_pty:
            from ptyprocess import PtyProcess

            fake_pty = MagicMock(pid=4321)
            with (
                patch.object(PtyProcess, "spawn", return_value=fake_pty) as pty_spawn,
                patch("threading.Thread", return_value=MagicMock()),
                patch.object(registry, "_write_checkpoint"),
            ):
                session = registry.spawn_local("pytest -q", cwd="/tmp", use_pty=True)
            argv = pty_spawn.call_args.args[0]
            assert any(str(a).endswith("systemd-run") for a in argv), argv
        else:
            fake_popen, captured = _fake_popen_capture()
            with (
                patch("subprocess.Popen", side_effect=fake_popen),
                patch("threading.Thread", return_value=MagicMock()),
                patch.object(registry, "_write_checkpoint"),
            ):
                session = registry.spawn_local("pytest -q", cwd="/tmp")
            assert _scoped_argv(captured), captured.get("argv")

        assert session.systemd_unit is not None
        assert session.systemd_unit.startswith("hermes-worker-")

    def test_descendant_still_runs_when_no_scope_is_available(
        self, registry, monkeypatch, _descendant
    ):
        """No user D-Bus -> degraded isolation, but the worker still runs."""
        monkeypatch.setattr(process_registry, "_find_shell", lambda: "/bin/bash")
        monkeypatch.setattr(
            process_registry, "_systemd_run_user_scope_available", lambda: False
        )

        fake_popen, captured = _fake_popen_capture()
        with (
            patch("subprocess.Popen", side_effect=fake_popen),
            patch("threading.Thread", return_value=MagicMock()),
            patch.object(registry, "_write_checkpoint"),
        ):
            registry.spawn_local("pytest -q", cwd="/tmp")

        # The command still runs -- just without its own cgroup.
        assert captured["argv"] == ["/bin/bash", "-lic", "set +m; pytest -q"]
        assert captured["kwargs"].get("start_new_session") is True


class TestUnrelatedCliIsNotScoped:
    """An unrelated CLI that inherited the markers must NOT be scoped."""

    @pytest.fixture()
    def _unrelated_cli(self, monkeypatch):
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        monkeypatch.setenv("INVOCATION_ID", "inherited-systemd-marker")
        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda *, cleanup_stale=False: os.getpid() + 1,
        )
        monkeypatch.setattr(
            process_registry, "_is_supervised_gateway_process", lambda: False
        )
        # Different cgroup -> not the gateway's budget.
        monkeypatch.setattr(
            process_registry, "_shares_gateway_cgroup", lambda: False
        )

    @pytest.mark.parametrize("use_pty", [False, True])
    def test_inherited_markers_do_not_scope_an_unrelated_cli(
        self, registry, monkeypatch, _unrelated_cli, use_pty
    ):
        monkeypatch.setattr(process_registry, "_find_shell", lambda: "/bin/bash")
        monkeypatch.setattr(
            process_registry, "_systemd_run_user_scope_available", lambda: True
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

        if use_pty:
            from ptyprocess import PtyProcess

            fake_pty = MagicMock(pid=4321)
            with (
                patch.object(PtyProcess, "spawn", return_value=fake_pty) as pty_spawn,
                patch("threading.Thread", return_value=MagicMock()),
                patch.object(registry, "_write_checkpoint"),
            ):
                registry.spawn_local("codex", cwd="/tmp", use_pty=True)
            assert pty_spawn.call_args.args[0] == [
                "/bin/bash",
                "-lic",
                "set +m; codex",
            ]
        else:
            fake_popen, captured = _fake_popen_capture()
            with (
                patch("subprocess.Popen", side_effect=fake_popen),
                patch("threading.Thread", return_value=MagicMock()),
                patch.object(registry, "_write_checkpoint"),
            ):
                registry.spawn_local("codex", cwd="/tmp")
            assert captured["argv"] == ["/bin/bash", "-lic", "set +m; codex"]
            assert captured["kwargs"].get("start_new_session") is True


class TestGatewayItselfIsStillScoped:
    """The gateway process keeps its existing behaviour."""

    @pytest.mark.parametrize("use_pty", [False, True])
    def test_gateway_process_is_scoped(self, registry, monkeypatch, use_pty):
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda *, cleanup_stale=False: os.getpid(),
        )
        monkeypatch.setattr(
            process_registry, "_is_supervised_gateway_process", lambda: True
        )
        monkeypatch.setattr(
            process_registry, "_shares_gateway_cgroup", lambda: False
        )
        monkeypatch.setattr(process_registry, "_find_shell", lambda: "/bin/bash")
        monkeypatch.setattr(
            process_registry, "_systemd_run_user_scope_available", lambda: True
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

        if use_pty:
            from ptyprocess import PtyProcess

            fake_pty = MagicMock(pid=4321)
            with (
                patch.object(PtyProcess, "spawn", return_value=fake_pty) as pty_spawn,
                patch("threading.Thread", return_value=MagicMock()),
                patch.object(registry, "_write_checkpoint"),
            ):
                registry.spawn_local("codex", cwd="/tmp", use_pty=True)
            assert any(
                str(a).endswith("systemd-run") for a in pty_spawn.call_args.args[0]
            )
        else:
            fake_popen, captured = _fake_popen_capture()
            with (
                patch("subprocess.Popen", side_effect=fake_popen),
                patch("threading.Thread", return_value=MagicMock()),
                patch.object(registry, "_write_checkpoint"),
            ):
                registry.spawn_local("codex", cwd="/tmp")
            assert _scoped_argv(captured), captured.get("argv")
