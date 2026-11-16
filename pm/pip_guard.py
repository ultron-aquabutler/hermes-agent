"""The pip guard's health check, surfaced by ``hermes pm doctor``.

WHY THIS EXISTS (kanban t_f25aee23)
-----------------------------------
The pip guard (``scripts/pip_guard.py``) is what stops a bare ``pip install``
from writing into the managed Python store — a PUBLISHED artifact whose digest
is recorded in ``~/.hermes/tools/facts.json``.  Without the guard, the store is
silently writable and the next stray ``pip install`` invalidates the digest for
every install on the host.  That has already happened twice here (psycopg
2026-10-24, pyyaml 2026-10-25).

The guard used to be a hand-placed file at ``~/.local/bin/pip``.  The uv/host
bootstrap recreates ``~/.local``, so the guard used to disappear silently, with
no alarm.  It now lives canonically at ``~/.hermes/bin/pip_guard.py`` with
``~/.local/bin/pip`` symlinked to it, and a systemd timer repairs the symlink.

This module is the FAIL-LOUD half: if the guard is missing for any reason,
``hermes pm doctor`` says so, so the condition is visible even if the
self-healing repair did not run.

The check is advisory about the GUARD, never about the store's integrity.  The
store's own digest is verified separately (``digest_matches`` in
``pm/store.py``); this module only reports whether the guard is in place.
"""

from __future__ import annotations

import os
from pathlib import Path

# The canonical, bootstrap-proof copy.
CANONICAL = Path.home() / ".hermes" / "bin" / "pip_guard.py"

# The PATH-visible entry.  A symlink to CANONICAL.
LINK = Path.home() / ".local" / "bin" / "pip"


def _is_guard_text(text: str) -> bool:
    return "Hermes managed Python store" in text or "pip guard" in text.lower()


def _is_ours(path: Path) -> bool:
    try:
        text = path.resolve().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _is_guard_text(text)


def guard_status() -> tuple[bool, str]:
    """Return (installed, detail) for the pip guard.

    ``installed`` is True only when BOTH the canonical copy and the PATH entry
    are in place and resolve to our guard.  A partially-wired guard is
    reported as not installed, because a guard that is not first on PATH
    protects nothing.
    """
    canonical_ok = CANONICAL.is_file() and _is_ours(CANONICAL)
    link_ok = LINK.is_symlink() and _is_ours(LINK)

    if canonical_ok and link_ok:
        return True, f"{LINK} -> {CANONICAL}"

    problems = []
    if not canonical_ok:
        problems.append(f"canonical guard missing or wrong at {CANONICAL}")
    if not link_ok:
        if not LINK.exists() and not LINK.is_symlink():
            problems.append(f"PATH entry missing at {LINK}")
        elif not LINK.is_symlink():
            problems.append(f"PATH entry at {LINK} is not a symlink to the durable copy")
        else:
            problems.append(f"PATH entry at {LINK} does not point at the durable guard")
    return False, "; ".join(problems)


def repair_hint() -> str:
    """The one-liner that restores the guard."""
    return "python scripts/install_pip_guard.py"


def guard_on_path() -> bool:
    """Is the guard the FIRST ``pip`` a shell would resolve?

    A guard that is installed but shadowed by another pip protects nothing.
    """
    path_env = os.environ.get("PATH", "")
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        cand = Path(entry) / "pip"
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return _is_ours(cand)
        except OSError:
            continue
    return False
