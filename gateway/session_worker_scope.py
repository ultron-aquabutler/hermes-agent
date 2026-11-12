"""Give a managed worker its own cgroup, without breaking its pipe contract.

A managed worker is spawned by the gateway and talks to it over a pair of
pipes: the gateway writes a JSON bootstrap frame into the child's stdin and
reads framed results back from its stdout (``agent.managed_worker``'s
``encode_frame``/``read_frame``).  Three separate contracts depend on the
process topology:

* ``gateway.session_worker_reservation.verify_worker_identity`` requires the
  reserved ``Popen`` handle to be a DIRECT child of the gateway
  (``handle.ppid() == os.getpid()``), and the interpreter behind it to descend
  from that handle;
* ``agent.managed_worker._die_with_owner`` requires the worker to LEAD its own
  process group (``os.getpgrp() == os.getpid()``), so it can kill its tool
  children when the owner dies;
* ``agent.managed_worker.validate_bootstrap`` requires the pid the worker
  reports in its ``hello`` frame to be its own ``os.getpid()``.

A plain ``subprocess.Popen`` inherits the gateway's cgroup, so the worker's
memory counts against the gateway's own ``memory.high``/``memory.max``.  Six
live workers held 1.7 GiB of a 1.8 GiB budget and pinned ``memory.high``
exactly, which starved the gateway's event loop and fired
``gateway.shutdown_watchdog`` (NOT the systemd watchdog) — the gateway killed
itself under memory pressure.

``systemd-run --user --scope`` would give the worker its own cgroup, but it
inserts a launcher process between the gateway and the worker: the worker's
``ppid`` is the launcher, not the gateway, and the worker is not the leader of
its process group.  That breaks the first two contracts above, so a
scope-wrapped ``Popen`` cannot be used here.

This module therefore takes the other route the card names: create the scope
FIRST, spawn the worker as an ordinary direct child (pipes and process group
unchanged), and then MOVE that child into the scope by writing its pid to the
scope's ``cgroup.procs``.  Every contract above survives:

* the worker is still the gateway's direct child, and still leads its group;
* the gateway still holds the child's stdin/stdout, so the framed handshake is
  untouched;
* the worker's memory is accounted to the scope, not the gateway.

The scope's ``MemoryMax`` comes from ``tools.process_registry``'s
``_worker_memory_max_bytes()``, which is itself derived from the SHARED
``hermes_cli.memory_budget`` module — the same numbers
``hermes_cli.kanban_db_dispatch`` uses for its concurrency cap.  No second
memory number is introduced here.

The scope is created by a *holder* process: ``systemd-run --scope`` runs a
trivial command that blocks on a pipe the parent holds open, so the scope
exists (and is addressable) for as long as the worker needs it.  When the
parent closes the pipe the holder exits, ``--collect`` garbage-collects the
scope, and the worker's memory is returned to the host.  If any step fails —
no user D-Bus, no ``systemd-run``, a refused cgroup write — the module
degrades to an ordinary spawn: the worker still runs, it simply shares the
gateway's cgroup again.  A degraded worker is strictly better than a dropped
one.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

# The scope holder is spawned with its OWN reference to ``Popen``.  A test
# that patches ``subprocess.Popen`` globally does so to intercept the WORKER's
# spawn (the gateway's ``acquire_process(subprocess.Popen, ...)``); letting
# that patch capture the holder as well would run the holder through the
# test's fake and hand the test a child it did not ask for.
_Popen = subprocess.Popen

# The scope's cgroup only exists once systemd has created the unit; polling
# ``systemctl show`` for its ControlGroup is how the path is discovered.  The
# bound is generous because the first call can race the unit's activation.
_SCOPE_CREATE_TIMEOUT_SECONDS = 15.0
_SCOPE_POLL_INTERVAL_SECONDS = 0.05
# How long to wait for the holder to exit after the release pipe closes.
_HOLDER_EXIT_TIMEOUT_SECONDS = 10.0
# The holder must not outlive the gateway by much: it holds one idle process.
_HOLDER_EXIT_GRACE_SECONDS = 5.0


class WorkerScope:
    """A transient user scope a managed worker can be moved into.

    ``WorkerScope`` is a context manager: entering it creates the scope, and
    leaving it releases the holder so systemd garbage-collects the scope.
    ``move_child`` places an already-spawned child into the scope.
    """

    def __init__(self, unit_suffix: str):
        self.unit_suffix = unit_suffix
        self.unit_name = f"hermes-worker-{unit_suffix}.scope"
        self.cgroup_path: Optional[str] = None
        self._holder: Optional[subprocess.Popen] = None
        self._release_fd: Optional[int] = None
        self._created = False

    # -- lifecycle ---------------------------------------------------------

    def create(self) -> bool:
        """Create the transient scope.  True when a scope exists.

        Returns False (never raises) when the host cannot provide one: no
        ``systemd-run``, no reachable user bus, or the unit failed to start.
        The caller then spawns the worker without isolation.
        """
        from tools.process_registry import _systemd_run_user_scope_available
        from tools.process_registry import systemd_user_bus_env

        binary = shutil.which("systemd-run")
        if binary is None:
            logger.debug("managed worker scope: systemd-run is not installed")
            return False
        if not _systemd_run_user_scope_available():
            logger.debug("managed worker scope: systemd-run --user --scope is unavailable")
            return False

        # The holder blocks reading one byte from a pipe we keep open.  When we
        # close it the holder exits and systemd collects the scope.
        read_fd, write_fd = os.pipe()
        try:
            self._holder = _Popen(
                [
                    binary, "--user", "--scope", "--quiet",
                    "--unit", self.unit_name, "--collect",
                    "--property", "MemoryAccounting=yes",
                    "--property", f"MemoryMax={_scope_memory_max_bytes()}",
                    "--", "/bin/sh", "-c", "head -c 1 >/dev/null",
                ],
                stdin=read_fd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=systemd_user_bus_env(),
            )
        except OSError as exc:
            logger.debug("managed worker scope: could not start the scope holder: %s", exc)
            os.close(read_fd)
            os.close(write_fd)
            return False
        finally:
            try:
                os.close(read_fd)
            except OSError:
                pass
        self._release_fd = write_fd

        path = self._await_scope_path()
        if path is None:
            self.release()
            return False
        self.cgroup_path = path
        self._created = True
        return True

    def _await_scope_path(self) -> Optional[str]:
        """The scope's cgroup directory, once systemd has created the unit."""
        systemctl = shutil.which("systemctl")
        if systemctl is None:
            return None
        from tools.process_registry import systemd_user_bus_env

        deadline = time.monotonic() + _SCOPE_CREATE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._holder is not None and self._holder.poll() is not None:
                # The holder died: the scope never became usable.
                return None
            try:
                result = subprocess.run(
                    [systemctl, "--user", "show", self.unit_name, "-p", "ControlGroup"],
                    capture_output=True,
                    timeout=5,
                    stdin=subprocess.DEVNULL,
                    env=systemd_user_bus_env(),
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            control_group = ""
            for line in (result.stdout or b"").decode("utf-8", "replace").splitlines():
                if line.startswith("ControlGroup="):
                    control_group = line.partition("=")[2].strip()
                    break
            if control_group:
                candidate = os.path.join("/sys/fs/cgroup", control_group.lstrip("/"))
                if os.path.isdir(candidate):
                    return candidate
            time.sleep(_SCOPE_POLL_INTERVAL_SECONDS)
        return None

    def move_child(self, pid: int) -> bool:
        """Move *pid* into the scope.  True when the move landed.

        A false return leaves the child exactly where it was — inside the
        gateway's cgroup — which is the degraded-but-running state, not a
        failure.  The caller must not drop the worker for this.
        """
        if not self._created or not self.cgroup_path:
            return False
        procs = os.path.join(self.cgroup_path, "cgroup.procs")
        try:
            with open(procs, "w", encoding="utf-8") as handle:
                handle.write(str(int(pid)))
        except (OSError, ValueError) as exc:
            logger.debug("managed worker scope: could not move pid %s into %s: %s",
                         pid, self.cgroup_path, exc)
            return False
        return True

    def release(self) -> None:
        """Release the holder so systemd collects the scope."""
        if self._release_fd is not None:
            try:
                os.close(self._release_fd)
            except OSError:
                pass
            self._release_fd = None
        holder, self._holder = self._holder, None
        if holder is not None and holder.poll() is None:
            try:
                holder.wait(timeout=_HOLDER_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                holder.kill()
                with _suppress_timeout():
                    holder.wait(timeout=_HOLDER_EXIT_GRACE_SECONDS)
        self._created = False

    def __enter__(self) -> WorkerScope:
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _suppress_timeout():
    import contextlib

    return contextlib.suppress(subprocess.TimeoutExpired)


def _scope_memory_max_bytes() -> int:
    """The per-worker cgroup limit, from the SHARED budget module.

    ``tools.process_registry._worker_memory_max_bytes()`` is the same function
    the local-executor scopes use, and it derives its number from
    ``hermes_cli.memory_budget`` — the module the dispatcher's concurrency cap
    also comes from.  Reusing it here means there is exactly ONE per-worker
    memory number in the system.
    """
    from tools.process_registry import _worker_memory_max_bytes

    return _worker_memory_max_bytes()


def managed_worker_scope(unit_suffix: str):
    """Create a scope for a managed worker, or ``None`` when the host has none.

    The caller keeps the returned object alive for the worker's lifetime and
    calls ``release()`` when the worker is done.
    """
    scope = WorkerScope(unit_suffix)
    try:
        if scope.create():
            return scope
    except Exception as exc:  # pragma: no cover - defensive: isolation must never break a spawn
        logger.debug("managed worker scope: creation failed: %s", exc)
        scope.release()
    return None


def managed_worker_unit_suffix(session_id: str, execution_id: str = "") -> str:
    """A scope unit name that identifies the worker without being unbounded."""
    # Import guard: gateway.session_managed_worker imports this module, so the
    # reverse import is deferred to the call site to avoid a cycle.
    import gateway.session_managed_worker_log  # pragma: no cover - import guard

    parts = [p for p in (session_id, execution_id) if p]
    raw = "-".join(parts) if parts else "worker"
    # systemd unit names are bounded and must not contain separators.
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw)
    return safe[:96] or "worker"


__all__ = [
    "WorkerScope",
    "managed_worker_scope",
    "managed_worker_unit_suffix",
]
