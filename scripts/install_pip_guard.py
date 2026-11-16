#!/usr/bin/env python3
"""Install (or reinstall) the pip guard at ~/.local/bin/pip.

The guard must be re-creatable, not hand-placed: ~/.local/bin is rebuilt by
the uv bootstrap on this host, and a hand-written file there does not
survive.  This installer is idempotent and is the documented way to put the
guard back.

Usage:
    python scripts/install_pip_guard.py            # install to ~/.local/bin/pip
    python scripts/install_pip_guard.py --check    # report, change nothing
    python scripts/install_pip_guard.py --uninstall
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "pip_guard.py"
TARGET = Path.home() / ".local" / "bin" / "pip"


def _is_ours(target: Path) -> bool:
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "Hermes managed Python store" in text or "pip guard" in text.lower()


def check() -> int:
    if not TARGET.exists():
        print(f"pip guard: NOT INSTALLED ({TARGET} does not exist)")
        return 1
    if not _is_ours(TARGET):
        print(f"pip guard: PRESENT but NOT OURS ({TARGET}) — leaving it alone")
        return 1
    print(f"pip guard: installed at {TARGET} (ours)")
    return 0


def install() -> int:
    if TARGET.exists() and not _is_ours(TARGET):
        print(f"pip guard: refusing to overwrite {TARGET} — it is not ours")
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    mode = TARGET.stat().st_mode
    TARGET.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"pip guard: installed at {TARGET} (mode {TARGET.stat().st_mode & 0o777:o})")
    return 0


def uninstall() -> int:
    if not TARGET.exists():
        print("pip guard: nothing to remove")
        return 0
    if not _is_ours(TARGET):
        print(f"pip guard: refusing to remove {TARGET} — it is not ours")
        return 1
    TARGET.unlink()
    print(f"pip guard: removed {TARGET}")
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
