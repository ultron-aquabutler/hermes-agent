"""Gateway lifecycle ledger — durable termination-reason evidence.

Graceful shutdowns leave forensics (``gateway-exit-diag.log``); an unclean
death (SIGKILL, kernel OOM, VM death) runs no handler.  A sentinel at
``<HERMES_HOME>/state/gateway.lifecycle.json`` closes the gap:
:func:`record_startup` finds ``phase == "running"`` from the previous life →
unclean death, appended to the exit-diag log as ``gateway.previous_unclean_exit``
and logged at WARNING; :func:`mark_exited` rewrites ``phase=exited`` on every
clean exit path.  Best-effort: forensics must never affect the lifecycle.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import inspect
from contextlib import closing
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _process_hermes_home() -> Path:
    """HERMES_HOME for process-level identity files (ignore task overrides)."""
    from hermes_constants import get_hermes_home, get_process_hermes_home

    # get_process_hermes_home expands ``~``/``$VAR`` (python -m gateway.run skips the CLI normalizer).
    return get_process_hermes_home() if os.environ.get("HERMES_HOME", "").strip() else get_hermes_home()


def _home_path(home: Optional[Path], *relative: str) -> Path:
    return (_process_hermes_home() if home is None else home).joinpath(*relative)


def get_lifecycle_sentinel_path(home: Optional[Path] = None) -> Path:
    """Return ``<HERMES_HOME>/state/gateway.lifecycle.json``."""
    return _home_path(home, "state", "gateway.lifecycle.json")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _proc_fields(path: str, wanted: dict[str, str]) -> dict[str, int]:
    """``{dst: int}`` for each ``src: dst`` key found in a ``Key: value`` /proc file."""
    found: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                key = line.split(":", 1)[0]
                if key in wanted:
                    found[wanted[key]] = int(line.split()[1])
                    if len(found) == len(wanted):
                        break
    except (OSError, ValueError, IndexError):
        return {}
    return found


def sample_memory() -> dict[str, Any]:
    """Cheap /proc snapshot (KiB): own RSS + MemTotal/MemAvailable + swap used.  Linux-only
    (``{}`` elsewhere), never raises; the 30s heartbeat embeds it so OOM cycles are classifiable."""
    sample = _proc_fields("/proc/self/status", {"VmRSS": "rss_kib"})
    mem = _proc_fields("/proc/meminfo", {"MemTotal": "mem_total_kib", "MemAvailable": "mem_available_kib",
                                         "SwapTotal": "SwapTotal", "SwapFree": "SwapFree"})
    swap_total, swap_free = mem.pop("SwapTotal", None), mem.pop("SwapFree", None)
    sample.update(mem)
    if swap_total is not None and swap_free is not None:
        sample["swap_used_kib"] = swap_total - swap_free
    return sample


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_sentinel(payload: dict[str, Any], home: Optional[Path]) -> None:
    try:
        from utils import atomic_json_write

        path = get_lifecycle_sentinel_path(home)
        from hermes_constants import mkdir_under_hermes_home

        mkdir_under_hermes_home(path.parent)
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write lifecycle sentinel", exc_info=True)


def _append_exit_diag(record: dict[str, Any], home: Optional[Path]) -> None:
    """Append a JSON line to gateway-exit-diag.log (same format as the CLI's ``_exit_diag``)."""
    try:
        path = _home_path(home, "logs", "gateway-exit-diag.log")
        from hermes_constants import mkdir_under_hermes_home

        mkdir_under_hermes_home(path.parent)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        logger.debug("Failed to append unclean-exit record", exc_info=True)


def _pid_is_sentinel_owner(pid: Any, start_time: Any, create_time: Any) -> bool:
    """True when ``pid`` is a live process that is the sentinel's incarnation — guards the
    ``--replace`` race: a live matching owner mid-teardown is a handover, not a death.

    Identity is the psutil ``create_time`` the sentinel stamps at claim (epoch seconds, same
    producer on both sides). The sentinel's ``start_time`` is the ledger claim time and is NOT
    comparable with ``gateway.status.get_process_start_time`` (proc ticks on Linux, centiseconds
    elsewhere) — the old comparison never matched, so every ``--replace`` handover read as an
    unclean death. A pre-stamp sentinel (no ``create_time``) still has ``start_time`` in epoch
    seconds: the owner was born BEFORE it claimed, a PID reuser AFTER the owner died, so a birth
    later than the claim is a reuser."""
    try:
        pid_int = int(pid)
        # NOT os.kill(pid, 0): on Windows that sends CTRL_C_EVENT to the target's console group.
        from gateway.status import _pid_exists

        if pid_int <= 0 or not _pid_exists(pid_int):
            return False
    except Exception:
        return False
    from hermes_cli.process_identity import _process_create_time

    actual = _process_create_time(pid_int)
    if actual is None:
        return True  # can't disambiguate PID reuse
    if type(create_time) in (int, float):
        return abs(actual - float(create_time)) <= 2.0
    if type(start_time) in (int, float):
        return actual <= float(start_time) + 2.0
    return True


def _suspected_oom(mem: dict[str, Any]) -> bool:
    """Heuristic only (classification stays with the reader); thresholds are
    memory_status' "critical" tier so a live warning and a post-mortem verdict agree."""
    from gateway.memory_status import _CRITICAL_AVAILABLE_FRACTION, _CRITICAL_AVAILABLE_KIB

    total, avail = mem.get("mem_total_kib"), mem.get("mem_available_kib")
    if not isinstance(avail, int):
        return False
    return avail < _CRITICAL_AVAILABLE_KIB or (
        isinstance(total, int) and total > 0 and avail / total < _CRITICAL_AVAILABLE_FRACTION
    )


def detect_unclean_exit(home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Evidence dict when the previous life died uncleanly, else ``None``. Read-only."""
    sentinel = _read_json(get_lifecycle_sentinel_path(home))
    if not sentinel or sentinel.get("phase") != "running":
        return None
    if _pid_is_sentinel_owner(sentinel.get("pid"), sentinel.get("start_time"), sentinel.get("create_time")):
        return None  # live owner — planned takeover in flight, not a death
    evidence: dict[str, Any] = {
        "prior_pid": sentinel.get("pid"), "prior_started_at": sentinel.get("started_at"),
        "prior_start_time": sentinel.get("start_time"),
    }
    # Enrich with the last heartbeat: last proven liveness and memory at that moment.
    try:
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        hb = _read_json(get_loop_heartbeat_path(home)) or {}
    except Exception:
        hb = {}
    if hb:
        evidence["last_heartbeat_at"] = hb.get("updated_at")
    mem = hb.get("mem")
    if isinstance(mem, dict):
        evidence["last_heartbeat_mem"] = mem
        if _suspected_oom(mem):
            evidence["suspected_oom"] = True
    return evidence


# Startup-watchdog lease held while the unclean-exit integrity check runs (#115542):
# ``PRAGMA quick_check(1)`` is one monolithic SQLite operation and per-call leases
# clamp at 900s, so a single entry lease cannot cover a multi-thousand-second healthy
# check on a huge store — without renewal the watchdog kills the attempt with exit 75
# and the restart loop never reaches the cron ticker. The progress handler below renews
# a phase-owned lease for as long as SQLite is making progress; without a deadline it
# always returns 0 so it never aborts the check, and the ok/absent/first-complaint
# verdicts stay fail-closed. When an explicit ``deadline_at`` is supplied the
# handler returns non-zero once it passes, so SQLite aborts the scan and the caller
# can bound a pathological store instead of pinning the boot path.
_INTEGRITY_CHECK_LEASE_PHASE = "state_db_unclean_integrity_check"
_INTEGRITY_CHECK_LEASE_S = 900.0
# VM instructions between progress-handler invocations; each invocation is a
# monotonic-clock compare, renewing at most every _INTEGRITY_CHECK_LEASE_RENEW_S.
_INTEGRITY_CHECK_PROGRESS_OPS = 100_000
_INTEGRITY_CHECK_LEASE_RENEW_S = 60.0
# Bound on the deferred post-connect scan: a pathological store cannot pin its thread forever.
STATE_DB_INTEGRITY_DEADLINE_S = 1800.0

# F2 (2026-10-23 gateway DM outage): ``PRAGMA quick_check(1)`` is O(store size).  On the
# 3.18 GB state.db it ran for ~36 minutes — the whole of the 22:44:33 -> 23:20:27 boot gap,
# during which the API server was not listening and every inbound was dropped.  The check
# runs on the boot path (``record_startup``), so its runtime IS the outage.
#
# The fix is a hard wall-clock budget on the check, not a better watchdog: the progress
# handler renews the lease for as long as SQLite makes progress, which correctly protects a
# slow-but-healthy check from the watchdog — but it also made the check unbounded, and
# "still progressing" is not the same as "will finish soon enough to serve traffic".
#
# A timeout is reported as a distinct verdict (``timeout``) that is NOT a corruption
# finding: the boot continues, the verdict is logged for the operator, and the store is
# left alone.  Failing closed here would be worse than the fault: a skipped check costs a
# diagnostic, while a blocked boot costs every message.
#
# The budget is generous enough for a healthy store of any plausible size (quick_check on
# a 500 MB store is ~2s per the docstring; 3 GB is ~10x that) and is overridable for
# operators who want a thorough check on a large store.
_INTEGRITY_CHECK_TIMEOUT_S = 120.0
# Above this store size the check is skipped by default: quick_check scales linearly and a
# multi-GB store cannot be checked inside the boot budget.  The skip is reported, not silent.
_INTEGRITY_CHECK_MAX_BYTES = 1_073_741_824  # 1 GiB


def _integrity_check_timeout_s() -> float:
    """Wall-clock budget for the unclean-exit integrity check (seconds).

    Overridable in ``config.yaml`` under ``gateway.state_db_integrity_check_timeout_s``
    for operators who want a longer (or shorter) check; invalid or non-positive values
    fall back to the default.  Read from the YAML layer rather than a ``HERMES_*`` env
    var: this is a behavioural setting, and the repo rule is that behaviour lives in
    ``config.yaml`` while ``.env`` is for credentials only.
    """
    default = _INTEGRITY_CHECK_TIMEOUT_S
    try:
        from gateway import config_loader
        from hermes_constants import get_hermes_home

        yaml_cfg = config_loader.read_yaml_layers(get_hermes_home()) or {}
        gateway_section = yaml_cfg.get("gateway")
        if not isinstance(gateway_section, dict):
            return default
        raw = gateway_section.get("state_db_integrity_check_timeout_s")
    except Exception as exc:  # health: allow BLE001 -- a broken config must not stop the boot
        logger.debug("state_db integrity check timeout unreadable: %s", exc)
        return default
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


def _install_integrity_check_lease(conn: sqlite3.Connection, *, deadline_at: Optional[float] = None) -> None:
    """Claim the integrity-check lease and renew it from a SQLite progress handler.

    ``report_startup_progress`` is a no-op when no watchdog is armed and never raises.
    Without ``deadline_at`` the handler always returns 0 -- it must never abort the verdict
    PRAGMA.  With ``deadline_at`` (a ``time.monotonic`` timestamp) it returns non-zero once
    that instant is reached, so SQLite raises ``OperationalError: interrupted`` and the
    caller can bound the scan.  Synchronous by design: no checker worker thread can outlive
    the check.
    """
    from hermes_startup_watchdog import report_startup_progress

    report_startup_progress(_INTEGRITY_CHECK_LEASE_S, phase=_INTEGRITY_CHECK_LEASE_PHASE)
    last_renew = time.monotonic()

    def _on_progress() -> int:
        nonlocal last_renew
        now = time.monotonic()
        if now - last_renew >= _INTEGRITY_CHECK_LEASE_RENEW_S:
            last_renew = now
            report_startup_progress(_INTEGRITY_CHECK_LEASE_S, phase=_INTEGRITY_CHECK_LEASE_PHASE)
        if deadline_at is not None and now >= deadline_at:
            return 1
        return 0

    conn.set_progress_handler(_on_progress, _INTEGRITY_CHECK_PROGRESS_OPS)


def _spawn_integrity_check_thread(target: Callable[[], None]) -> threading.Thread:
    """Start the integrity-check worker under the caller's contextvars.

    ``spawn_context_thread`` keeps the caller's profile scope (ContextVars do not cross a
    bare ``Thread``), which is what the repo's HX012 rule requires.  The fallback keeps
    the module importable if the agent package is unavailable — a lifecycle forensics
    helper must never be the thing that breaks the boot.
    """
    try:
        from agent.memory_provider import spawn_context_thread

        return spawn_context_thread(target, name="state-db-integrity-check", daemon=True)
    except Exception as exc:  # health: allow BLE001 -- forensics must never block the boot
        logger.debug("spawn_context_thread unavailable for integrity check: %s", exc)
        return threading.Thread(target=target, name="state-db-integrity-check", daemon=True)


def _run_quick_check(path: Path, deadline: float, *, deadline_at: Optional[float] = None) -> tuple[str, Optional[str]]:
    """Run ``PRAGMA quick_check(1)`` in a worker thread; return ``(verdict, error)``.

    The deadline is enforced by the CALLER, not by SQLite's progress handler: a
    ``quick_check`` on a multi-GB store does long stretches of bulk work between progress
    callbacks (measured: 93 s before the first callback fired at the default granularity),
    so a handler-side abort lands far past the budget.  Running the check on a worker
    thread lets the boot path abandon it on time regardless of SQLite's callback
    granularity.

    ``deadline_at`` is the handler-side abort instant (``time.monotonic``).  When it is
    supplied the SQLite progress handler also aborts the scan, so the thread finishes
    promptly instead of being abandoned mid-READ.

    The worker is a daemon thread and is deliberately NOT joined on timeout: the abandoned
    check only reads, and a thread that outlives the boot is harmless — but JOINING it
    would re-block the boot for exactly the duration we are trying to avoid.
    """
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        try:
            with closing(sqlite3.connect(str(path))) as conn:
                _install_integrity_check_lease(conn, deadline_at=deadline_at)
                row = conn.execute("PRAGMA quick_check(1)").fetchone()
            outcome["verdict"] = "check-failed: no result" if not row or row[0] is None else str(row[0])
        except Exception as exc:  # health: allow BLE001 -- the verdict must never raise; the class is recorded
            logger.warning("state.db integrity check raised", exc_info=True)
            outcome["error"] = str(exc)

    thread = _spawn_integrity_check_thread(_worker)
    thread.start()
    # Bounded join: on timeout we walk away and the daemon thread finishes on its own.
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        return "timeout", None
    if "error" in outcome:
        return "check-failed", str(outcome["error"])
    return str(outcome.get("verdict", "check-failed: no result")), None


def _store_exceeds_integrity_ceiling(path: Path) -> float:
    """The store's size in bytes; ``0.0`` when it cannot be stat'ed.

    ``quick_check`` is O(store size), so a multi-GB store cannot be checked inside any
    sane boot budget.  The ceiling is decided with NO I/O against the store itself.
    """
    try:
        return float(path.stat().st_size)
    except OSError:
        return 0.0


def _invoke_quick_check(path: Path, deadline: float, deadline_at: Optional[float]) -> tuple[str, Optional[str]]:
    """Call :func:`_run_quick_check`, tolerating a narrower replacement.

    Tests replace ``_run_quick_check`` with a two-argument stub; passing a keyword the
    stub does not accept would raise ``TypeError`` and mask the verdict under test.  The
    fallback is decided by SIGNATURE, not by catching ``TypeError`` — a ``TypeError``
    raised inside a real check must not trigger a second, unbounded call.
    """
    try:
        params = inspect.signature(_run_quick_check).parameters
    except (TypeError, ValueError):
        params = {}
    if "deadline_at" in params:
        return _run_quick_check(path, deadline, deadline_at=deadline_at)
    return _run_quick_check(path, deadline)


def check_state_db_integrity(home: Optional[Path] = None, *, deadline_s: Optional[float] = None) -> str:
    """``"ok"``, ``"absent"``, or the first ``quick_check`` complaint.  Never raises.

    Only after an unclean death — SIGKILL mid-WAL-checkpoint can leave half-written
    b-tree pages.  ``quick_check(1)`` stops at the first problem (~2s on a healthy
    500MB store): cheap once per unclean boot, too costly every boot.  Opened
    normally: a WAL store needs its -shm sidecar for read-only, and the PRAGMA writes nothing.

    Holds a phase-owned startup-watchdog lease for the check duration
    (renewed from a SQLite progress handler, #115542) so a long healthy check
    on a huge store is not mistaken for a parked deadlock.

    ``deadline_s`` (optional): the caller's own budget for the scan.  When supplied the
    SQLite progress handler also aborts the scan at that instant and the verdict is
    ``"check-incomplete: exceeded <N>s"`` — the historic contract of this argument.

    F2: the check is bounded twice over, because it runs on the boot path and its runtime
    IS the outage.  A store above ``_INTEGRITY_CHECK_MAX_BYTES`` is not checked at all
    (``skipped-too-large``, decided with no I/O), and a check that outlives its wall-clock
    budget is abandoned on time (``timeout``).  Neither verdict is a corruption finding:
    both keep the boot moving and are reported to the operator.  The bounds apply on BOTH
    paths — the inline scan and the deferred post-connect scan — because the deferred scan
    is the default boot path and an unbounded 3 GB store would pin its thread forever.
    """
    path = _home_path(home, "state.db")
    if not path.exists():
        return "absent"
    size = _store_exceeds_integrity_ceiling(path)
    if size > _INTEGRITY_CHECK_MAX_BYTES:
        # quick_check is O(size); a multi-GB store cannot be checked inside the boot
        # budget.  Report the skip instead of blocking the boot on a diagnostic.
        logger.warning(
            "state.db is %.1f MB — skipping the unclean-exit quick_check (limit %.1f MB) "
            "so the gateway can start serving. Run 'hermes doctor' for a full check.",
            size / 1024 / 1024, _INTEGRITY_CHECK_MAX_BYTES / 1024 / 1024,
        )
        return "skipped-too-large"
    timeout_s = _integrity_check_timeout_s()
    deadline_at: Optional[float] = None
    if deadline_s is not None:
        try:
            deadline_at = time.monotonic() + max(0.0, float(deadline_s))
        except (TypeError, ValueError):
            deadline_at = None
    # The caller-side budget is the tighter of the built-in wall-clock budget and the
    # caller's own ``deadline_s``: the check must not outlive either.
    deadline = time.monotonic() + timeout_s
    effective_limit_s = timeout_s
    if deadline_at is not None and deadline_at < deadline:
        deadline = deadline_at
        effective_limit_s = max(0.0, float(deadline_s)) if deadline_s is not None else 0.0
    verdict, error = _invoke_quick_check(path, deadline, deadline_at)
    if verdict == "timeout":
        logger.warning(
            "state.db quick_check exceeded its %.0fs budget and was abandoned; the gateway is "
            "starting anyway. Run 'hermes doctor' for a full check.", effective_limit_s,
        )
        if deadline_at is not None:
            # The caller's own budget is the binding one: keep the historic contract of
            # that argument and report the budget that actually expired.
            return f"check-incomplete: exceeded {effective_limit_s:g}s"
        return "timeout"
    if verdict == "check-failed":
        return f"check-failed: {error}"
    return verdict


def _state_db_journal_mode(path: Path) -> Optional[str]:
    """On-disk journal mode of ``path`` via one ``PRAGMA journal_mode`` read on a short-lived
    connection (no lock outlives it); ``None`` when undeterminable.  Never raises."""
    try:
        from hermes_state_wal import _on_disk_journal_mode

        with closing(sqlite3.connect(str(path), timeout=1.0)) as conn:
            return _on_disk_journal_mode(conn) or None
    except Exception:
        logger.debug("Could not read state.db journal mode", exc_info=True)
        return None


def _report_unclean_exit(evidence: dict[str, Any], home: Optional[Path], *, defer_integrity_check: bool = False) -> None:
    """Persist the exit-diag record and log at WARNING.

    When ``defer_integrity_check`` is set, the forensic store scan is NOT run here (it would
    gate platform startup); the record carries ``state_db_integrity: "deferred"`` and
    :func:`run_deferred_integrity_check` runs the scan post-connect.
    """
    if defer_integrity_check:
        evidence["state_db_integrity"] = "deferred"
    else:
        # The death may have torn the store; this is the only moment we know to look.
        verdict = evidence["state_db_integrity"] = check_state_db_integrity(home=home)
        _log_integrity_verdict(verdict)
    _append_exit_diag({"ts": _now_iso(), "tag": "gateway.previous_unclean_exit", "pid": os.getpid(), **evidence}, home)
    logger.warning(
        "Previous gateway life (pid=%s, started_at=%s) exited UNCLEANLY (no exit path ran — SIGKILL / OOM / "
        "VM death, or a process kill issued by the agent or one of its descendants, e.g. a pkill/taskkill of "
        "the host interpreter image; see #113667). last_heartbeat_at=%s last_mem=%s suspected_oom=%s",
        evidence.get("prior_pid"), evidence.get("prior_started_at"), evidence.get("last_heartbeat_at"),
        evidence.get("last_heartbeat_mem"), evidence.get("suspected_oom", False),
    )


# F2 verdicts: the check did NOT RUN, so the store was not verified.  Reporting them as a
# corruption finding would send an operator to repair a healthy multi-GB store; reporting
# them as a clean result would hide a real gap.  They are logged distinctly as inconclusive.
_INCONCLUSIVE_INTEGRITY_VERDICTS = ("timeout", "skipped-too-large")


def _log_integrity_verdict(verdict: str) -> None:
    """Log an integrity verdict at the severity its MEANING deserves.

    ``timeout`` / ``skipped-too-large`` mean the check did not run — inconclusive, not a
    corruption finding.  ``check-incomplete: exceeded <N>s`` is the same class of answer
    (the scan was abandoned, so the store was not verified).  Everything else that is not
    ``ok``/``absent`` is a real failure.
    """
    if verdict in ("ok", "absent"):
        return
    if verdict in _INCONCLUSIVE_INTEGRITY_VERDICTS or verdict.startswith("check-incomplete:"):
        logger.warning(
            "state.db integrity check was INCONCLUSIVE after an unclean gateway exit (%s) — the "
            "store was not verified. Run `hermes doctor` for a full check.", verdict,
        )
        return
    logger.error(
        "state.db FAILED integrity check after an unclean gateway exit: %s — sessions may read as "
        "missing until it is repaired. Run `hermes doctor`.",
        verdict,
    )


def record_startup(home: Optional[Path] = None, *, defer_integrity_check: bool = True) -> Optional[dict[str, Any]]:
    """Boot entry point: report any unclean previous exit (evidence dict, also persisted
    to ``gateway-exit-diag.log`` and logged at WARNING) then claim the sentinel.  Never raises.

    ``defer_integrity_check`` defaults to True: the state.db integrity scan is forensic and
    must NOT gate platform startup (a large store's ``quick_check`` blocked the ``:8644`` fleet
    peer-DM transport for minutes).  In deferred mode the exit-diag record is written
    immediately with ``state_db_integrity: "deferred"`` and the scan runs post-connect via
    :func:`run_deferred_integrity_check`.  Pass False for the historic INLINE scan.
    """
    evidence: Optional[dict[str, Any]] = None
    try:
        evidence = detect_unclean_exit(home)
        if evidence is not None:
            _report_unclean_exit(evidence, home, defer_integrity_check=defer_integrity_check)
    except Exception:
        logger.debug("Unclean-exit detection failed", exc_info=True)
    try:
        claim: dict[str, Any] = {"phase": "running", "pid": os.getpid(), "start_time": time.time(), "started_at": _now_iso()}
        # Process birth (psutil), distinct from ``start_time`` (the ledger claim, seconds later once
        # imports finish): the Windows start attestation binds PIDs to birth time (#110020 review).
        from hermes_cli.process_identity import _process_create_time

        create_time = _process_create_time(os.getpid())
        if create_time is not None:
            claim["create_time"] = create_time
        # Carry the verdict on the PREVIOUS life on the new sentinel: it is the only
        # machine-readable copy (/api/status reads it to report an OOM restart).
        # Scoped to this life — the next clean exit or boot rewrites the sentinel.
        if evidence is not None:
            claim["prior_unclean_exit"] = True
            if evidence.get("suspected_oom"):
                claim["prior_suspected_oom"] = True
        _write_sentinel(claim, home)
    except Exception:
        logger.debug("Failed to claim lifecycle sentinel", exc_info=True)
    return evidence


def run_deferred_integrity_check(
    evidence: Optional[dict[str, Any]], *, home: Optional[Path] = None,
    deadline_s: float = STATE_DB_INTEGRITY_DEADLINE_S,
) -> str:
    """Run the deferred post-connect state.db scan and append a second diag record.

    Companion to :func:`record_startup` with ``defer_integrity_check=True``: the forensic scan
    is moved off the startup path (where, on a multi-GB store, it gated every platform adapter
    — including the ``:8644`` fleet peer-DM transport — for minutes) into a daemon thread
    started only after platforms connected.  ``deadline_s`` bounds a pathological store so the
    thread cannot be pinned forever.

    Appends ``{"tag": "gateway.state_db_integrity", "deferred": true, "state_db_integrity":
    <verdict>, "prior_pid": ...}`` and logs the existing ERROR text on a bad verdict.  NEVER
    raises -- a forensic scan must not disturb a running gateway (the alerting path is the
    bounded per-poll readiness probe ``gateway/readiness.py::_probe_state_db``).

    Only a WAL store is scanned: in rollback-journal mode (``database.journal_mode: delete``,
    or a silent WAL fallback) the scan's SHARED lock would block every live commit for its
    whole run, so the scan is skipped with a ``skipped: ...`` verdict and one WARNING.  The
    on-disk mode decides, not config; an undeterminable mode is skipped too.
    """
    try:
        path = _home_path(home, "state.db")
        mode = _state_db_journal_mode(path) if path.exists() else "wal"
        if mode != "wal":
            verdict = (f"skipped: rollback-journal store (journal_mode={mode or 'unknown'}), "
                       "scan would block live writes")
            logger.warning(
                "Previous gateway exit was unclean, but the state.db integrity check was skipped: %s is a "
                "rollback-journal store (journal_mode=%s) and a background scan would block live writes. "
                "To check it, stop the gateway and run: sqlite3 %s 'PRAGMA quick_check'",
                path, mode or "unknown", path,
            )
        else:
            verdict = check_state_db_integrity(home=home, deadline_s=deadline_s)
        if verdict not in ("ok", "absent") and not verdict.startswith("skipped:"):
            logger.error(
                "state.db FAILED integrity check after an unclean gateway exit: %s — sessions may read as "
                "missing until it is repaired. Run `hermes doctor`.",
                verdict,
            )
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "tag": "gateway.state_db_integrity",
            "deferred": True,
            "state_db_integrity": verdict,
        }
        if isinstance(evidence, dict) and evidence.get("prior_pid") is not None:
            record["prior_pid"] = evidence.get("prior_pid")
        _append_exit_diag(record, home)
        if isinstance(evidence, dict):
            evidence["state_db_integrity"] = verdict
        return verdict
    except Exception:
        logger.debug("Deferred state.db integrity check failed", exc_info=True)
        return "check-failed: deferred scan raised"


def mark_exited(exit_code: Optional[int] = None, reason: str = "graceful_shutdown", home: Optional[Path] = None) -> None:
    """Mark the current life as cleanly exited.  Idempotent, never raises.

    Only rewrites a sentinel provably owned by this process: during ``--replace``
    the replacement claims it before the old process finishes teardown and must not
    be clobbered.  ``pid=None`` / malformed sentinels have unknown ownership → left alone.
    """
    try:
        sentinel = _read_json(get_lifecycle_sentinel_path(home))
        if sentinel is not None and sentinel.get("pid") != os.getpid():
            return
        exited: dict[str, Any] = {"phase": "exited", "pid": os.getpid(), "exit_code": exit_code, "exit_reason": reason,
                                  "exited_at": _now_iso()}
        # Carry the incarnation identity: the Windows start attestation matches a clean exit by
        # PID *and* start time so a reused PID's exit cannot vouch for a different life (#110020).
        for key in ("start_time", "create_time"):
            if sentinel is not None and sentinel.get(key) is not None:
                exited[key] = sentinel[key]
        _write_sentinel(exited, home)
    except Exception:
        logger.debug("Failed to mark lifecycle sentinel exited", exc_info=True)


def read_prior_exit_label(profile_home: Path) -> str:
    """``clean``/``unclean``/``unknown`` for the profile's last gateway life; annotates
    ``container-boot.log``.  ``running`` is unclean: the old PID namespace is gone at container boot."""
    try:
        phase = (_read_json(get_lifecycle_sentinel_path(profile_home)) or {}).get("phase")
        return {"exited": "clean", "running": "unclean"}.get(phase, "unknown")
    except Exception:
        return "unknown"
