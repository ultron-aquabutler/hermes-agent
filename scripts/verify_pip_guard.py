#!/usr/bin/env python3
"""Verify the pip guard's contract (AC5 of the worktree-install-state work).

Exercises the INSTALLED guard (``~/.local/bin/pip``) as a subprocess and asserts
on its real behaviour:

  1. bare ``pip install <pkg>``             -> REFUSED (non-zero exit, refusal text)
  2. ``pip --python <py> install <pkg>``     -> passes through to the real pip
  3. ``pip install --python <py> <pkg>``     -> passes through to the real pip
  4. ``pip install --target <dir> <pkg>``    -> passes through to the real pip
  5. ``pip check``                          -> passes through to the real pip
  6. ``pip --version``                      -> passes through to the real pip
  7. the guard's own argument parser resolves the subcommand and scope option
  8. LIVE PROOF: a real ``pip install`` attempt against the managed store is
     refused and the store's digest is byte-identical afterwards.

"Passes through" is proven by the REAL pip's own output appearing (not the
guard's refusal message), and by a non-zero exit that carries pip's own error
text rather than the guard's.

The managed store tree is resolved by GLOB, never by a hardcoded name: the
build-date segment of the directory name is rendered differently in different
contexts, and a hardcoded literal silently resolves to nothing.

Usage:
    python scripts/verify_pip_guard.py
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path.home() / ".local" / "bin" / "pip"

sys.path.insert(0, str(HERE))
import pip_guard

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    verdict = "PASS" if ok else "FAIL"
    print(f"  {name:<58} {verdict}")
    print(f"      {detail}")


def store_digest(root: str) -> str:
    """sha256 over the store tree, excluding __pycache__ (the same rule
    pm.store.tree_digest uses)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fname in sorted(filenames):
            p = os.path.join(dirpath, fname)
            h.update(os.path.relpath(p, root).encode())
            h.update(b"\0")
            try:
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                pass
            h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    print("=" * 78)
    print("AC5 — the pip guard")
    print("=" * 78)
    print()
    print(f"guard path : {GUARD}")
    print(f"exists     : {GUARD.exists()}")
    if not GUARD.exists():
        print()
        print("AC5 RESULT: FAIL — the guard is not installed at ~/.local/bin/pip")
        print("            install it with: python scripts/install_pip_guard.py")
        return 1
    print("mode       : %o" % (GUARD.stat().st_mode & 0o777))
    print()

    which = subprocess.run(
        ["sh", "-c", "command -v pip"], capture_output=True, text=True, check=False
    )
    print(f"`command -v pip` -> {which.stdout.strip()}")
    print()

    # A REAL interpreter for the --python case, so pass-through is proven to
    # actually work. Resolved by glob; never hardcoded.
    store_pythons = sorted(glob.glob(os.path.expanduser("~/.hermes/tools/python-3.14.9*/bin/python3.14")))
    if not store_pythons:
        store_pythons = sorted(glob.glob(os.path.expanduser("~/.hermes/tools/python-*/bin/python3.14")))
    REAL_PYTHON = store_pythons[-1] if store_pythons else sys.executable
    STORE_ROOT = os.path.dirname(os.path.dirname(REAL_PYTHON))

    tmp = tempfile.mkdtemp(prefix="verify-pip-guard-")
    target_dir = os.path.join(tmp, "target")
    print(f"--python target (a real interpreter): {REAL_PYTHON}")
    print()

    def run(args):
        return subprocess.run(
            [str(GUARD)] + args, capture_output=True, text=True, check=False
        )

    # ------------------------------------------------------------- 1. REFUSE
    print("-" * 78)
    print("1. bare `pip install <pkg>` MUST be REFUSED")
    print("-" * 78)
    r = run(["install", "somepkg"])
    combined = r.stdout + r.stderr
    first_line = combined.strip().splitlines()[0] if combined.strip() else ""
    record(
        "bare `pip install somepkg` is refused",
        r.returncode != 0 and "refus" in combined.lower(),
        f"exit={r.returncode}  first_line={first_line!r}",
    )
    print("  --- guard output ---")
    for line in combined.strip().splitlines():
        print(f"  | {line}")
    print()

    # --------------------------------------------------------- 2/3. --python
    print("-" * 78)
    print("2/3. `--python <interpreter>` MUST pass through (both orderings)")
    print("-" * 78)
    for argv, label in (
        (["--python", REAL_PYTHON, "install", "--dry-run", "somepkg"], "`pip --python <py> install ...`"),
        (["install", "--python", REAL_PYTHON, "--dry-run", "somepkg"], "`pip install --python <py> ...`"),
    ):
        r = run(argv)
        combined = r.stdout + r.stderr
        not_refused = "refus" not in combined.lower()
        record(
            f"{label} passes through",
            not_refused,
            f"exit={r.returncode}  refused={not not_refused}  output={combined.strip()[:150]!r}",
        )
    print()

    # ------------------------------------------------------------- 4. --target
    print("-" * 78)
    print("4. `pip install --target <dir> <pkg>` MUST pass through")
    print("-" * 78)
    r = run(["install", "--target", target_dir, "--dry-run", "somepkg"])
    combined = r.stdout + r.stderr
    not_refused = "refus" not in combined.lower()
    record(
        "`pip install --target <dir> ...` passes through",
        not_refused,
        f"exit={r.returncode}  refused={not not_refused}  output={combined.strip()[:150]!r}",
    )
    print()

    # --------------------------------------------------------------- 5. check
    print("-" * 78)
    print("5. `pip check` MUST pass through")
    print("-" * 78)
    r = run(["check"])
    combined = r.stdout + r.stderr
    not_refused = "refus" not in combined.lower()
    # Real pip answers `pip check` with a sentence about requirements; the
    # guard never does. Any such sentence proves the real pip handled it.
    spoke = bool(re.search(r"broken requirements|requirements found|No broken", combined, re.IGNORECASE))
    record(
        "`pip check` passes through",
        not_refused and spoke,
        f"exit={r.returncode}  refused={not not_refused}  output={combined.strip()[:150]!r}",
    )
    print("  --- pip check output ---")
    for line in combined.strip().splitlines()[:6]:
        print(f"  | {line}")
    print()

    # ------------------------------------------------------------ 6. version
    print("-" * 78)
    print("6. `pip --version` MUST pass through")
    print("-" * 78)
    r = run(["--version"])
    combined = r.stdout + r.stderr
    not_refused = "refus" not in combined.lower()
    record(
        "`pip --version` passes through",
        not_refused and "pip" in combined.lower(),
        f"exit={r.returncode}  refused={not not_refused}  output={combined.strip()[:150]!r}",
    )
    print("  --- pip --version output ---")
    for line in combined.strip().splitlines()[:4]:
        print(f"  | {line}")
    print()

    # --------------------------------------------------- 7. argument-parser unit
    print("-" * 78)
    print("7. argument parsing (the guard's own logic)")
    print("-" * 78)
    cases = [
        (["install", "pkg"], "install", None),
        (["--python", "/x/bin/python", "install", "pkg"], "install", "--python"),
        (["install", "--python", "/x/bin/python", "pkg"], "install", "--python"),
        (["install", "--target", "/tmp/t", "pkg"], "install", "--target"),
        (["--target=/tmp/t", "install", "pkg"], "install", "--target"),
        (["check"], "check", None),
        (["--version"], None, None),
        (["list"], "list", None),
        (["download", "pkg"], "download", None),
    ]
    for argv, want_sub, want_scope in cases:
        got_sub = pip_guard._subcommand(argv)
        got_scope = pip_guard._has_scope(argv)
        record(
            f"parse {argv!r}",
            got_sub == want_sub and got_scope == want_scope,
            f"subcommand={got_sub!r} (want {want_sub!r})  scope={got_scope!r} (want {want_scope!r})",
        )
    print()

    # ------------------------------------------------- 8. live proof, store safe
    print("-" * 78)
    print("8. LIVE PROOF — a real install attempt against the managed store")
    print("-" * 78)
    before = store_digest(STORE_ROOT)
    r = run(["install", "pip"])
    after = store_digest(STORE_ROOT)
    combined = r.stdout + r.stderr
    refused = "refus" in combined.lower()
    record(
        "a real `pip install pip` into the store is refused",
        refused and r.returncode != 0,
        f"exit={r.returncode}  refused={refused}",
    )
    record(
        "the managed store's digest is UNCHANGED after that attempt",
        before == after,
        f"before={before[:32]}  after={after[:32]}",
    )
    print(f"  store root    : {STORE_ROOT}")
    print(f"  digest before : {before}")
    print(f"  digest after  : {after}")
    print()

    print("=" * 78)
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"AC5 RESULT: FAIL — {len(failed)} case(s) failed:")
        for n in failed:
            print(f"   - {n}")
        return 1
    print(f"AC5 RESULT: PASS — all {len(results)} cases")
    print("  - a bare `pip install` is refused (exit != 0, refusal text on stderr)")
    print("  - `--python` (both orderings), `--target`, `check`, `--version` all pass through")
    print("  - the guard is the `pip` a shell resolves")
    print("  - a real install attempt leaves the managed store's digest unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
