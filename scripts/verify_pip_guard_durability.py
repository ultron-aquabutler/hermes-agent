#!/usr/bin/env python3
"""Verify that the pip guard SURVIVES a ~/.local wipe (kanban t_f25aee23).

The guard used to be a hand-placed file at ``~/.local/bin/pip``.  The uv/host
bootstrap recreates ``~/.local`` from scratch, so the guard vanished silently
and the managed Python store became writable by a bare ``pip install`` — with
no alarm.  ``scripts/install_pip_guard.py`` had ZERO call sites.

This verifier proves the fix actually works, end to end, by reproducing the
failure and watching it get repaired:

  1. BASELINE — the guard is installed (canonical copy + PATH symlink) and the
     guard refuses a bare ``pip install``.
  2. WIPE — the ``~/.local/bin/pip`` symlink is destroyed, exactly as a
     ``~/.local`` recreation leaves it.  The store's OWN pip is now what
     ``command -v pip`` resolves: the guard is GONE.
  3. SURVIVED — the canonical copy at ``~/.hermes/bin/pip_guard.py`` is still
     there and byte-identical to the committed source.  The bootstrap did NOT
     take the guard with it.
  4. SELF-HEAL — the systemd unit ``hermes-pip-guard.service`` is invoked (the
     same thing the timer does).  The symlink comes back.
  5. ARMED — the timer is enabled and active, so the repair happens
     unattended within minutes, not only when a human notices.
  6. REFUSES AGAIN — a bare ``pip install`` is refused with exit 2.
  7. FAIL-LOUD — ``pm.pip_guard.guard_status()`` reports healthy; and when
     the guard is missing, the doctor module reports it as NOT installed.

Nothing here mutates the managed store: the only ``pip`` invocation that
targets it is the bare ``pip install`` the guard is supposed to refuse, and
its refusal is what is asserted.

Usage:
    python scripts/verify_pip_guard_durability.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

CANONICAL = Path.home() / ".hermes" / "bin" / "pip_guard.py"
LINK = Path.home() / ".local" / "bin" / "pip"
SOURCE = HERE / "pip_guard.py"

ENSURER = Path.home() / ".hermes" / "scripts" / "ensure_pip_guard.py"
UNIT = "hermes-pip-guard.service"
TIMER = "hermes-pip-guard.timer"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {name:<58} {'PASS' if ok else 'FAIL'}")
    print(f"      {detail}")


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def which_pip() -> str:
    r = subprocess.run(
        ["sh", "-c", "command -v pip"], capture_output=True, text=True, check=False
    )
    return r.stdout.strip()


def bare_install_refused() -> tuple[bool, str]:
    """A bare `pip install` must be refused with a non-zero exit."""
    r = subprocess.run(
        ["pip", "install", "somepkg"], capture_output=True, text=True, check=False
    )
    combined = r.stdout + r.stderr
    return r.returncode != 0 and "refus" in combined.lower(), f"exit={r.returncode}"


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def main() -> int:
    print("=" * 78)
    print("pip guard DURABILITY — survives a ~/.local wipe")
    print("=" * 78)
    print()

    # ---------------------------------------------------------- 1. BASELINE
    print("-" * 78)
    print("1. BASELINE — the guard is installed and refusing")
    print("-" * 78)

    if not LINK.is_symlink():
        print(f"  FAIL: {LINK} is not a symlink. Run scripts/install_pip_guard.py first.")
        return 1
    record(
        f"{LINK} is a symlink",
        True,
        f"-> {os.readlink(LINK)}",
    )
    record(
        "the symlink points at the durable canonical copy",
        os.readlink(LINK) == str(CANONICAL),
        f"readlink={os.readlink(LINK)}",
    )
    record(
        f"canonical copy exists at {CANONICAL}",
        CANONICAL.is_file(),
        f"mode={CANONICAL.stat().st_mode & 0o777:o}" if CANONICAL.is_file() else "ABSENT",
    )
    record(
        "canonical copy is byte-identical to the committed source",
        CANONICAL.is_file() and sha256(CANONICAL) == sha256(SOURCE),
        f"canonical={sha256(CANONICAL)[:16]}  source={sha256(SOURCE)[:16]}"
        if CANONICAL.is_file()
        else "canonical absent",
    )
    refused, detail = bare_install_refused()
    record("a bare `pip install` is refused", refused, detail)
    print()

    # --------------------------------------------------------------- 2. WIPE
    print("-" * 78)
    print("2. WIPE — destroy ~/.local/bin/pip exactly as a ~/.local recreate would")
    print("-" * 78)

    if LINK.is_symlink() or LINK.exists():
        LINK.unlink()
    gone = not LINK.exists()
    record("the PATH entry is gone", gone, f"{LINK} exists={LINK.exists()}")
    resolved = which_pip()
    now_unguarded = resolved != str(LINK)
    record(
        "`command -v pip` now resolves to the STORE's unguarded pip",
        now_unguarded,
        f"-> {resolved}",
    )
    print()

    # ----------------------------------------------------------- 3. SURVIVED
    print("-" * 78)
    print("3. SURVIVED — the canonical copy outlived the wipe")
    print("-" * 78)

    record(
        f"the canonical guard at {CANONICAL} still exists",
        CANONICAL.is_file(),
        "present" if CANONICAL.is_file() else "ABSENT",
    )
    if CANONICAL.is_file():
        record(
            "and is still byte-identical to the committed source",
            sha256(CANONICAL) == sha256(SOURCE),
            f"sha256={sha256(CANONICAL)[:16]}",
        )
    print()

    # ---------------------------------------------------------- 4. SELF-HEAL
    print("-" * 78)
    print("4. SELF-HEAL — the systemd unit re-establishes the guard")
    print("-" * 78)

    if not ENSURER.is_file():
        record(
            f"runtime ensurer installed at {ENSURER}",
            False,
            "ABSENT — install with: install -m 0755 scripts/ensure_pip_guard.py "
            f"{ENSURER}",
        )
    else:
        record(f"runtime ensurer installed at {ENSURER}", True, "present")

    r = systemctl("start", UNIT)
    record(
        f"`systemctl --user start {UNIT}` succeeds",
        r.returncode == 0,
        f"exit={r.returncode}  {r.stderr.strip()[:120]}",
    )
    healed = LINK.is_symlink() and LINK.resolve() == CANONICAL.resolve()
    record(
        "the symlink is back and points at the canonical copy",
        healed,
        f"{LINK} -> {os.readlink(LINK) if LINK.is_symlink() else '(not a symlink)'}",
    )
    print()

    # -------------------------------------------------------------- 5. ARMED
    print("-" * 78)
    print("5. ARMED — the repair runs unattended")
    print("-" * 78)

    en = systemctl("is-enabled", TIMER)
    record(
        f"{TIMER} is enabled",
        en.stdout.strip() == "enabled",
        en.stdout.strip() or en.stderr.strip(),
    )
    act = systemctl("is-active", TIMER)
    record(
        f"{TIMER} is active",
        act.stdout.strip() == "active",
        act.stdout.strip() or act.stderr.strip(),
    )
    show = systemctl(
        "show", TIMER, "-p", "TimersMonotonic", "-p", "NextElapseUSecMonotonic"
    )
    record(
        f"{TIMER} has a recurring schedule",
        "OnUnitActiveUSec=" in show.stdout,
        " | ".join(l.strip() for l in show.stdout.strip().splitlines()),
    )
    print()

    # ---------------------------------------------------- 6. REFUSES AGAIN
    print("-" * 78)
    print("6. REFUSES AGAIN — the restored guard still protects the store")
    print("-" * 78)

    record(
        "`command -v pip` resolves to the guard again",
        which_pip() == str(LINK),
        f"-> {which_pip()}",
    )
    refused, detail = bare_install_refused()
    record("a bare `pip install` is refused again", refused, detail)
    print()

    # ---------------------------------------------------------- 7. FAIL-LOUD
    print("-" * 78)
    print("7. FAIL-LOUD — pm doctor reports the guard's state")
    print("-" * 78)

    try:
        from pm.pip_guard import guard_status

        installed, detail = guard_status()
        record(
            "pm.pip_guard.guard_status() reports the guard installed",
            installed,
            detail,
        )
    except ImportError as exc:
        record("pm.pip_guard is importable", False, str(exc))

    # The negative case: with the guard removed, the status must report NOT
    # installed.  Restore it immediately afterwards.
    if LINK.is_symlink():
        saved_target = os.readlink(LINK)
        LINK.unlink()
        try:
            from pm.pip_guard import guard_status

            installed_neg, detail_neg = guard_status()
            record(
                "with the guard removed, guard_status() reports NOT installed",
                not installed_neg,
                detail_neg,
            )
        finally:
            LINK.symlink_to(saved_target)
    print()

    print("=" * 78)
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"DURABILITY RESULT: FAIL — {len(failed)} case(s) failed:")
        for n in failed:
            print(f"   - {n}")
        return 1
    print(f"DURABILITY RESULT: PASS — all {len(results)} cases")
    print("  - the guard's canonical copy lives in ~/.hermes/bin and survives a ~/.local wipe")
    print("  - `hermes-pip-guard.timer` re-establishes the ~/.local/bin/pip symlink unattended")
    print("  - `hermes pm doctor` reports the guard's state, so an absence is never silent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
