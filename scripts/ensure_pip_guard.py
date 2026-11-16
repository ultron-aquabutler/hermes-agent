#!/usr/bin/env python3
"""Ensure the pip guard is installed, and repair it if it is not.

This is the SELF-HEALING half of the pip guard's durability story
(kanban t_f25aee23).  It runs from the systemd user timer
``hermes-pip-guard.timer`` every few minutes and:

  1. checks that the canonical guard exists at ``~/.hermes/bin/pip_guard.py``
  2. checks that ``~/.local/bin/pip`` is a symlink to it
  3. repairs whatever is missing, from the canonical copy
  4. ALERTS (via stderr + the systemd journal, and optionally a Telegram
     webhook) when it had to repair something — a silent repair would hide
     the fact that a bootstrap ran.

WHY THE REPAIR SOURCE IS THE CANONICAL COPY, NOT THE REPO
----------------------------------------------------------
The repo checkout (``~/.hermes/hermes-agent``) can move, be re-pointed, or be
replaced by a worktree that is later pruned.  ``~/.hermes/bin`` is the same
durable location the docker CLI uses (card ``t_8bb558a7``) and it survives
both a uv bootstrap and a checkout churn.  If the canonical copy is ALSO
missing, this script falls back to any known checkout, and if that fails too
it ALERTS rather than silently leaving the store unguarded.

Usage:
    python scripts/ensure_pip_guard.py            # check + repair
    python scripts/ensure_pip_guard.py --check    # report only, never repair
    python scripts/ensure_pip_guard.py --verify   # exit 2 if unhealthy
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CANONICAL = Path.home() / ".hermes" / "bin" / "pip_guard.py"
LINK = Path.home() / ".local" / "bin" / "pip"

# Where to look for the guard SOURCE if the canonical copy has gone missing.
# Ordered by preference; each entry is a directory holding ``pip_guard.py``.
SOURCE_DIRS = [
    Path.home() / ".hermes" / "bin",
    Path.home() / ".hermes" / "hermes-agent" / "scripts",
    Path.home() / ".hermes" / "hermes-agent" / ".worktrees" / "t_0b4d74e3" / "scripts",
]

STATE_FILE = Path.home() / ".hermes" / "state" / "pip-guard.json"

# Optional Telegram alert.  Read from the env so the unit does not carry a
# token.  Absent -> the repair is only reported in the journal.
TELEGRAM_ENV = "HERMES_PIP_GUARD_TELEGRAM"


def log(msg: str) -> None:
    print(f"pip-guard: {msg}", flush=True)


def _is_guard_text(text: str) -> bool:
    return "Hermes managed Python store" in text or "pip guard" in text.lower()


def _is_ours(path: Path) -> bool:
    try:
        text = path.resolve().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _is_guard_text(text)


def find_source() -> Path | None:
    """A pip_guard.py source to (re)install from."""
    for d in SOURCE_DIRS:
        cand = d / "pip_guard.py"
        if cand.is_file() and _is_ours(cand):
            return cand
    # Last resort: any pip_guard.py under ~/.hermes, shallow first.
    try:
        hits = sorted(
            (Path.home() / ".hermes").glob("*/scripts/pip_guard.py"),
            key=lambda p: len(p.parts),
        )
        hits += sorted((Path.home() / ".hermes").glob("*/.worktrees/*/scripts/pip_guard.py"))
    except OSError:
        hits = []
    for cand in hits:
        if _is_ours(cand):
            return cand
    return None


def state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        log(f"WARN could not write state file {STATE_FILE}: {exc}")


def alert(message: str) -> None:
    """Report a repair.  Journal always; Telegram when configured."""
    log(f"ALERT {message}")
    url = os.environ.get(TELEGRAM_ENV, "").strip()
    if not url:
        return
    payload = json.dumps({"text": f"[pip-guard] {message}"}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        log("alert delivered")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"WARN could not deliver alert: {exc}")


def ensure(check_only: bool = False) -> tuple[bool, list[str]]:
    """Return (healthy, problems).  Repairs unless check_only."""
    problems: list[str] = []
    repaired: list[str] = []

    canonical_ok = CANONICAL.is_file() and _is_ours(CANONICAL)
    if not canonical_ok:
        problems.append(f"canonical guard missing or wrong at {CANONICAL}")
        src = find_source()
        if src is None:
            problems.append(f"no pip_guard.py source found in {SOURCE_DIRS}")
        elif not check_only:
            CANONICAL.parent.mkdir(parents=True, exist_ok=True)
            CANONICAL.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            CANONICAL.chmod(0o755)
            repaired.append(f"restored canonical guard from {src}")
            log(f"repaired: restored canonical guard from {src}")

    link_ok = LINK.is_symlink() and _is_ours(LINK)
    if not link_ok:
        problems.append(f"PATH entry missing or wrong at {LINK}")
        if not check_only and canonical_ok:
            LINK.parent.mkdir(parents=True, exist_ok=True)
            if LINK.exists() and not LINK.is_symlink():
                if not _is_ours(LINK):
                    problems.append(f"{LINK} is not ours — refusing to replace it")
                else:
                    LINK.unlink()
                    LINK.symlink_to(CANONICAL)
                    repaired.append(f"replaced regular {LINK} with symlink")
                    log(f"repaired: replaced regular {LINK} with symlink")
            elif not LINK.exists():
                LINK.symlink_to(CANONICAL)
                repaired.append(f"re-created symlink {LINK} -> {CANONICAL}")
                log(f"repaired: re-created symlink {LINK} -> {CANONICAL}")
            elif LINK.is_symlink() and not _is_ours(LINK):
                LINK.unlink()
                LINK.symlink_to(CANONICAL)
                repaired.append(f"repointed symlink {LINK} -> {CANONICAL}")
                log(f"repaired: repointed symlink {LINK} -> {CANONICAL}")

    healthy = not problems
    if repaired:
        alert("; ".join(repaired))
    return healthy, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only, never repair")
    ap.add_argument("--verify", action="store_true", help="exit 2 if unhealthy")
    args = ap.parse_args()

    healthy, problems = ensure(check_only=args.check)

    if healthy:
        log("healthy (canonical + symlink)")
        return 0

    for p in problems:
        log(f"PROBLEM {p}")

    if args.check:
        return 1
    if args.verify:
        return 2
    # Repair ran.  Exit non-zero when anything is still wrong so the journal
    # records it, but 0 when the repair succeeded.
    healthy, problems = ensure(check_only=True)
    if healthy:
        log("repaired — healthy now")
        return 0
    for p in problems:
        log(f"STILL BROKEN {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
