#!/usr/bin/env python3
"""Install (or reinstall) the pip guard so it SURVIVES the uv/host bootstrap.

WHY THIS EXISTS (kanban t_f25aee23)
-----------------------------------
The pip guard used to live only at ``~/.local/bin/pip`` — a hand-placed file in
the one directory on this host that the uv/host bootstrap RECREATES from
scratch.  The 2026-11-03 bootstrap proved this is not theoretical: it wiped
``~/.local`` and took the static docker CLI, the ssh shim and ``~/.docker``
with it (card ``t_66795d80``).

The guard now follows the proven precedent on this host (the docker CLI fix,
``scripts/ensure-docker-cli.sh``): the CANONICAL copy lives in
``~/.hermes/bin``, which the bootstrap does not touch, and ``~/.local/bin/pip``
is a SYMLINK to it.  A bootstrap that recreates ``~/.local`` therefore destroys
the symlink but not the guard, and the self-healing timer
(``hermes-pip-guard.timer``) re-creates the symlink.

TWO-LAYER PROTECTION
--------------------
    layer 1 (durability)  ~/.hermes/bin/pip_guard.py  <- canonical, survives
    layer 2 (self-heal)   hermes-pip-guard.timer       -> re-creates the link
    layer 3 (fail loud)   `hermes pm doctor`            -> warns when unguarded

Usage:
    python scripts/install_pip_guard.py            # install + link
    python scripts/install_pip_guard.py --check    # report, change nothing
    python scripts/install_pip_guard.py --uninstall # remove the link + canonical
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "pip_guard.py"

# The canonical, bootstrap-proof location.  ~/.hermes has survived every
# bootstrap on this host; ~/.local has not.
CANONICAL = Path.home() / ".hermes" / "bin" / "pip_guard.py"

# The PATH-visible location.  A symlink, never a copy: the guard resolves
# itself with os.path.realpath(__file__), so a symlink still identifies the
# right "real pip" to delegate to (see pip_guard._find_real_pip).
LINK = Path.home() / ".local" / "bin" / "pip"


def _is_ours(path: Path) -> bool:
    """True if `path` (or what it points at) is our guard."""
    try:
        text = path.resolve().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "Hermes managed Python store" in text or "pip guard" in text.lower()


def _describe(path: Path) -> str:
    """Human-readable state of `path` for --check output."""
    if not path.exists() and not path.is_symlink():
        return f"ABSENT ({path})"
    if path.is_symlink():
        dest = os.readlink(path)
        if _is_ours(path):
            return f"OK (symlink -> {dest})"
        return f"WRONG SYMLINK (-> {dest})"
    if _is_ours(path):
        return "OK (regular file, ours)"
    return "PRESENT BUT NOT OURS"


def check() -> int:
    """Report state, change nothing.  Exit 0 only when fully wired."""
    print(f"pip guard: canonical  {_describe(CANONICAL)}")
    print(f"pip guard: path entry {_describe(LINK)}")
    ok = _is_ours(CANONICAL) and LINK.is_symlink() and _is_ours(LINK)
    if ok:
        print("pip guard: installed (durable + linked)")
        return 0
    print("pip guard: NOT fully installed — run scripts/install_pip_guard.py")
    return 1


def install() -> int:
    # 1. Canonical copy.  This is the layer that survives a bootstrap.
    CANONICAL.parent.mkdir(parents=True, exist_ok=True)
    if CANONICAL.exists() and not _is_ours(CANONICAL):
        print(f"pip guard: refusing to overwrite {CANONICAL} — it is not ours")
        return 1
    CANONICAL.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    mode = CANONICAL.stat().st_mode
    CANONICAL.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"pip guard: canonical copy at {CANONICAL} (mode {CANONICAL.stat().st_mode & 0o777:o})")

    # 2. PATH-visible symlink.  Recreated by the timer if a bootstrap eats it.
    LINK.parent.mkdir(parents=True, exist_ok=True)
    if LINK.is_symlink():
        if os.readlink(LINK) == str(CANONICAL):
            print(f"pip guard: link already correct -> {CANONICAL}")
        else:
            LINK.unlink()
            LINK.symlink_to(CANONICAL)
            print(f"pip guard: relinked {LINK} -> {CANONICAL}")
    elif LINK.exists():
        if not _is_ours(LINK):
            print(f"pip guard: refusing to replace {LINK} — it is not ours")
            return 1
        # An older install left a regular copy there.  Replace it with the
        # symlink so the two paths cannot drift apart.
        LINK.unlink()
        LINK.symlink_to(CANONICAL)
        print(f"pip guard: replaced regular file {LINK} with symlink -> {CANONICAL}")
    else:
        LINK.symlink_to(CANONICAL)
        print(f"pip guard: linked {LINK} -> {CANONICAL}")

    return 0


def uninstall() -> int:
    removed = []
    if LINK.is_symlink() or LINK.exists():
        if _is_ours(LINK) or LINK.is_symlink():
            LINK.unlink()
            removed.append(str(LINK))
        else:
            print(f"pip guard: refusing to remove {LINK} — it is not ours")
            return 1
    if CANONICAL.exists():
        if _is_ours(CANONICAL):
            CANONICAL.unlink()
            removed.append(str(CANONICAL))
        else:
            print(f"pip guard: refusing to remove {CANONICAL} — it is not ours")
            return 1
    for p in removed:
        print(f"pip guard: removed {p}")
    if not removed:
        print("pip guard: nothing to remove")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--uninstall", action="store_true", help="remove the guard")
    args = ap.parse_args()

    if args.check:
        return check()
    if args.uninstall:
        return uninstall()
    return install()


if __name__ == "__main__":
    sys.exit(main())
