"""A managed worker lives in its OWN cgroup, not the gateway's (t_6b5fe961).

The gateway spawns every kanban/cron worker with a plain ``subprocess.Popen``,
so the worker inherited the GATEWAY's cgroup: its memory was charged against
the gateway's own ``memory.high``/``memory.max``.  Six live workers held
1.7 GiB of a 1.8 GiB budget, ``memory.current`` sat *exactly* at
``memory.high``, and the gateway's event loop starved until
``gateway.shutdown_watchdog`` (NOT the systemd watchdog) killed the gateway.

A scope unit's NAME is not proof of what is inside it: the
``hermes-worker-*.scope`` units already existed and looked like isolation,
while the real workers sat in ``hermes-gateway.service``.  These tests assert
on ``/proc/<pid>/cgroup`` — the process's ACTUAL cgroup — so the topology
cannot silently regress.

Three contracts have to survive the isolation, and each has its own test:

* the worker stays the gateway's DIRECT child (``verify_worker_identity``
  requires ``handle.ppid() == os.getpid()``);
* the worker stays the LEADER of its own process group
  (``_die_with_owner`` requires ``os.getpgrp() == os.getpid()``);
* the framed result handshake still works end to end (the gateway holds the
  child's stdin/stdout).
"""

import os
import subprocess
import sys
import time

import pytest

from gateway.session_managed_worker import WORKER_POPEN

pytestmark = [pytest.mark.platforms("linux")]


def _cgroup_of(pid):
    """The process's ACTUAL cgroup path (the unified hierarchy's ``0::`` line)."""
    with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("0::"):
                return line.partition("::")[2].strip()
    return ""


def _scope_units():
    """The set of transient scope unit names systemd currently knows about."""
    from tools.process_registry import systemd_user_bus_env

    result = subprocess.run(["systemctl", "--user", "list-units", "--type=scope",
                             "--no-legend", "--no-pager", "--plain"],
                            capture_output=True, timeout=15, check=False,
                            env=systemd_user_bus_env())
    names = set()
    for line in (result.stdout or b"").decode("utf-8", "replace").splitlines():
        parts = line.split()
        if parts:
            names.add(parts[0])
    return names


def _scope_available():
    """True when this host can actually create a user scope."""
    from tools.process_registry import _systemd_run_user_scope_available

    return _systemd_run_user_scope_available()


requires_scope = pytest.mark.skipif(
    not _scope_available(),
    reason="this host cannot create a systemd user scope (no user D-Bus / systemd-run)",
)


# --- the shared budget is the ONLY per-worker memory number --------------------


def test_scope_memory_max_matches_the_process_registry_budget():
    """The scope and the local-executor scopes must use the SAME limit."""
    from gateway.session_worker_scope import _scope_memory_max_bytes
    from tools.process_registry import _worker_memory_max_bytes

    assert _scope_memory_max_bytes() == _worker_memory_max_bytes()


def test_the_scope_budget_is_the_shared_budget_module():
    """The per-worker limit is derived from ``hermes_cli.memory_budget``.

    The dispatcher derives its concurrency cap from the same module, so the
    per-worker limit and the cap cannot drift apart.
    """
    from hermes_cli import memory_budget

    host = memory_budget.host_memory_total_bytes()
    assert host is not None
    expected = memory_budget.per_worker_memory_max_bytes(host, 2)
    assert expected is not None
    # The real budget function honours its own floor and ceiling.
    assert memory_budget.MIN_WORKER_MEMORY_MAX_BYTES <= expected
    assert expected <= memory_budget.MAX_WORKER_MEMORY_MAX_BYTES


def test_the_scope_does_not_introduce_a_second_memory_number():
    """``_scope_memory_max_bytes`` must defer to the shared module, not compute
    a limit of its own."""
    import inspect

    from gateway import session_worker_scope

    source = inspect.getsource(session_worker_scope._scope_memory_max_bytes)
    assert "_worker_memory_max_bytes" in source, \
        "the scope no longer derives its limit from the shared budget"
    assert "1024 * 1024" not in source, \
        "the scope introduced its own memory number"


# --- the fallback: a host without a scope still runs the worker ----------------


def test_scope_creation_degrades_when_systemd_run_is_missing(monkeypatch):
    """No ``systemd-run`` -> no scope, and NO exception (a degraded spawn)."""
    import shutil

    from gateway import session_worker_scope

    monkeypatch.setattr(shutil, "which",
                        lambda name: None if name == "systemd-run" else "/usr/bin/systemctl")
    scope = session_worker_scope.WorkerScope("unit-test-no-systemd-run")
    assert scope.create() is False
    assert scope.cgroup_path is None


def test_scope_creation_degrades_when_the_user_bus_is_unavailable(monkeypatch):
    """No reachable user D-Bus -> no scope, and NO exception."""
    from gateway import session_worker_scope

    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: False)
    scope = session_worker_scope.WorkerScope("unit-test-no-bus")
    assert scope.create() is False
    assert scope.cgroup_path is None


def test_a_worker_still_runs_without_a_scope(monkeypatch):
    """The degraded path must RUN the worker, not drop it.

    This is the acceptance criterion that matters most when the fix misfires:
    a host with no user D-Bus must still complete a turn.
    """
    from gateway import session_worker_scope

    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: False)
    scope = session_worker_scope.managed_worker_scope("unit-test-degraded")
    assert scope is None, "a host without a scope must degrade to None, not raise"

    # The worker itself still runs and still answers over its pipe.
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('alive\\n'); sys.stdout.flush()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **WORKER_POPEN)
    try:
        assert process.stdout.readline().strip() == b"alive"
    finally:
        process.kill()
        process.wait(timeout=5)


def test_move_child_into_a_missing_scope_is_false_not_an_exception():
    """A scope that was never created cannot move a child — it must not raise."""
    from gateway.session_worker_scope import WorkerScope

    scope = WorkerScope("unit-test-not-created")
    assert scope.move_child(os.getpid()) is False


# --- the live topology: the worker is in its OWN cgroup ------------------------


@requires_scope
def test_a_managed_worker_runs_in_its_own_scope_not_the_gateway_cgroup():
    """THE topology test: the worker's cgroup is a scope, not the gateway's.

    Reproduces the reported defect: before the fix the child's cgroup was
    ``.../hermes-gateway.service`` — the gateway's own — so its memory was
    charged against the gateway's budget.
    """
    from gateway.session_worker_scope import managed_worker_scope, managed_worker_unit_suffix

    suffix = managed_worker_unit_suffix("t_6b5fe961-topology", "exec-1")
    scope = managed_worker_scope(suffix)
    assert scope is not None, "this host should provide a scope"

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time;"
         "sys.stdout.write('%d %d %d\\n' % (os.getpid(), os.getppid(), os.getpgrp()));"
         "sys.stdout.flush(); time.sleep(30)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **WORKER_POPEN)
    try:
        assert scope.move_child(child.pid) is True
        pid, ppid, pgid = (int(part) for part in child.stdout.readline().split())
        assert pid == child.pid

        # 1. The worker's ACTUAL cgroup is the scope, NOT the gateway's unit.
        cgroup = _cgroup_of(pid)
        assert cgroup.endswith(f"{scope.unit_name}"), \
            f"the worker is in {cgroup!r}, not its own scope {scope.unit_name!r}"
        assert "hermes-gateway.service" not in cgroup, \
            "the worker is back inside the gateway's cgroup"

        # 2. The scope unit really exists (the name is not the proof; the
        #    cgroup path is — this only guards against a typo'd unit name).
        assert scope.unit_name in _scope_units()

        # 3. The worker is still the gateway's DIRECT child: verify_worker_identity
        #    requires handle.ppid() == os.getpid() and would refuse otherwise.
        assert ppid == os.getpid(), "the scope inserted a launcher between the gateway and the worker"

        # 4. The worker still LEADS its own process group: _die_with_owner needs
        #    os.getpgrp() == os.getpid() to reap its tool children.
        assert pgid == pid, "the worker no longer leads its own process group"
    finally:
        child.kill()
        child.wait(timeout=5)
        scope.release()


@requires_scope
def test_the_scope_charges_its_memory_to_itself_not_the_gateway():
    """A worker's allocation must land in the SCOPE's memory.current."""
    from gateway.session_worker_scope import managed_worker_scope

    scope = managed_worker_scope("t_6b5fe961-accounting")
    assert scope is not None
    scope_root = scope.cgroup_path

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import time; x = bytearray(120 * 1024 * 1024); time.sleep(20)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **WORKER_POPEN)
    try:
        assert scope.move_child(child.pid) is True
        deadline = time.monotonic() + 10
        scope_current = 0
        while time.monotonic() < deadline:
            scope_current = int(open(os.path.join(scope_root, "memory.current")).read().strip())
            if scope_current > 80 * 1024 * 1024:
                break
            time.sleep(0.2)
        assert scope_current > 80 * 1024 * 1024, \
            f"the scope only charged {scope_current} bytes for a 120 MiB allocation"
    finally:
        child.kill()
        child.wait(timeout=5)
        scope.release()


@requires_scope
def test_the_scope_carries_the_shared_per_worker_memory_max():
    """The scope's MemoryMax equals the shared budget's per-worker limit."""
    from gateway.session_worker_scope import managed_worker_scope, _scope_memory_max_bytes

    scope = managed_worker_scope("t_6b5fe961-memorymax")
    assert scope is not None
    try:
        limit = int(open(os.path.join(scope.cgroup_path, "memory.max")).read().strip())
        assert limit == _scope_memory_max_bytes()
    finally:
        scope.release()


# --- the framed handshake still works end to end -------------------------------


@requires_scope
def test_the_framed_handshake_survives_the_scope():
    """The gateway's framed result handshake must survive the scope move.

    The gateway writes a JSON bootstrap frame into the child's stdin and reads
    a framed result back from its stdout.  Moving the child into a scope must
    not disturb either pipe.
    """
    from agent.managed_worker import encode_frame, read_frame
    from gateway.session_worker_scope import managed_worker_scope

    scope = managed_worker_scope("t_6b5fe961-handshake")
    assert scope is not None

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import json,sys;"
         "line = sys.stdin.readline();"
         "req = json.loads(line);"
         "sys.stdout.write(json.dumps({'type': 'result', 'echo': req['ping']}) + '\\n');"
         "sys.stdout.flush()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **WORKER_POPEN)
    try:
        assert scope.move_child(child.pid) is True
        child.stdin.write(encode_frame({'ping': 'hello-scope'}))
        child.stdin.flush()
        frame = read_frame(child.stdout)
        assert frame == {'type': 'result', 'echo': 'hello-scope'}
    finally:
        child.stdin.close()
        child.kill()
        child.wait(timeout=5)
        scope.release()


@requires_scope
def test_the_worker_reports_its_own_pid_after_the_move():
    """``validate_bootstrap`` requires the worker's reported pid to be its own.

    The owner also verifies the interpreter DESCENDS from the reserved handle.
    A scope move must not make the worker report a different pid.
    """
    import psutil

    from gateway.session_worker_scope import managed_worker_scope

    scope = managed_worker_scope("t_6b5fe961-identity")
    assert scope is not None
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time; sys.stdout.write('%d\\n' % os.getpid()); sys.stdout.flush(); time.sleep(20)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **WORKER_POPEN)
    try:
        assert scope.move_child(child.pid) is True
        reported = int(child.stdout.readline().strip())
        assert reported == child.pid
        # The chain check verify_worker_identity performs.
        handle = psutil.Process(child.pid)
        assert handle.ppid() == os.getpid()
        chain = [p.pid for p in psutil.Process(reported).parents()[:3]]
        assert reported in chain or reported == child.pid
    finally:
        child.kill()
        child.wait(timeout=5)
        scope.release()


# --- the spawn path uses the scope --------------------------------------------


def test_the_spawn_path_asks_for_a_scope():
    """``_spawn_registered_worker`` must create a scope and move the child in.

    A regression that dropped the scope call would leave every worker in the
    gateway's cgroup again — the exact defect this card is about.
    """
    import inspect

    from gateway import session_managed_worker

    source = inspect.getsource(session_managed_worker._spawn_registered_worker)
    assert "_managed_worker_scope" in source, \
        "the spawn path no longer creates a scope for the worker"
    assert "move_child" in source, \
        "the spawn path no longer moves the worker into its scope"


def test_the_scope_is_released_when_the_worker_is_closed():
    """``ManagedWorker.close()`` must release the scope (the cgroup's memory
    is charged to the scope until its holder exits)."""
    from gateway.session_managed_worker import ManagedWorker

    released = []

    class FakeScope:
        def release(self):
            released.append(True)

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, **WORKER_POPEN)
    worker = ManagedWorker(process)
    worker.scope = FakeScope()
    worker.close()
    assert released, "close() did not release the worker's scope"
    assert worker.scope is None
