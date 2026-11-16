#!/usr/bin/env python3
"""Verify the managed store's integrity (AC4 of the worktree-install-state work).

AC4:
    "No new files inside the managed Python store tree
     (except __pycache__ bytecode, which tree_digest excludes)."

Two independent checks, both against the LIVE store:

  1. DIGEST — recompute pm.store.tree_digest over the store tree and compare
     it with the digest recorded in ~/.hermes/tools/facts.json.  Any added,
     removed or modified file changes the digest.

  2. MTIME — no file in the store tree may be newer than the store's own
     publication timestamp.  A post-publication write is a new file even if it
     happens not to change the recorded digest.

The store tree is resolved by GLOB, never by a hardcoded name: the build-date
segment of the directory name is rendered differently in different contexts,
and a hardcoded literal silently resolves to nothing (os.path.exists returns
False for it while the directory is plainly there).

Every managed store tree present on disk is checked, not just the active one.

Usage:
    python scripts/verify_managed_store.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from pm.store import tree_digest
except ImportError:  # pragma: no cover - pm may not be importable from a worktree
    tree_digest = None

TOOLS = Path.home() / ".hermes" / "tools"
FACTS = TOOLS / "facts.json"


def main() -> int:
    print("=" * 78)
    print("AC4 — the managed Python store")
    print("=" * 78)
    print()

    if tree_digest is None:
        print("AC4 RESULT: FAIL — pm.store.tree_digest is not importable from")
        print(f"            {REPO}")
        print("            run this from a checkout where pm/ is present")
        return 1

    if not FACTS.exists():
        print(f"AC4 RESULT: FAIL — {FACTS} does not exist")
        return 1

    facts = json.load(open(FACTS))
    recorded = facts["packages"]["python"]["digest"]
    entry_literal = facts["packages"]["python"]["entry"]

    # Resolve the active entry by GLOB (the literal in facts.json is redacted).
    active_matches = sorted(glob.glob(str(TOOLS / (entry_literal.split("+")[0] + "*"))))
    if not active_matches:
        print("AC4 RESULT: FAIL — could not resolve the active store entry")
        print("            (looked for %s)" % (entry_literal.split("+")[0] + "*"))
        return 1
    active = Path(active_matches[-1])

    # Every managed python store tree on disk.
    all_trees = sorted(Path(p) for p in glob.glob(str(TOOLS / "python-3.14.*")))

    print(f"facts.json packages.python.entry  (redacted literal): {entry_literal}")
    print(f"facts.json packages.python.digest                    : {recorded}")
    print(f"resolved ACTIVE store tree                          : {active.name}")
    print(f"all managed store trees on disk                      : {len(all_trees)}")
    print()

    guard_epoch = active.stat().st_mtime
    print(f"publication guard epoch (active tree dir mtime)      : {guard_epoch}")
    print()

    failures = []

    for tree in all_trees:
        print("-" * 78)
        print(f"TREE: {tree.name}")
        print("  path:", tree)
        if not tree.is_dir():
            print("  RESULT: not a directory — skipped")
            continue

        realized = tree_digest(tree)
        expect = recorded if tree == active else None
        if expect is None:
            print(f"  realized tree_digest = {realized}")
            print("  recorded digest      = (none — not the active store entry)")
        else:
            print(f"  realized tree_digest = {realized}")
            print(f"  recorded digest      = {expect}")
            ok = realized == expect
            print(f"  digest match         = {ok}")
            if not ok:
                failures.append(f"{tree.name} digest mismatch")

        newer = []
        for dirpath, dirnames, filenames in os.walk(tree):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fname in filenames:
                p = Path(dirpath) / fname
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime > guard_epoch:
                    newer.append((mtime, str(p)))
        newer.sort(reverse=True)
        print("  -- files newer than the publication epoch, excluding __pycache__ --")
        if newer:
            for _, p in newer[:40]:
                print(f"    NEWER: {p}")
            print(f"  count newer-than-guard: {len(newer)}")
            failures.append(f"{tree.name} has {len(newer)} file(s) newer than publication")
        else:
            print("    (none)")

        pyc = sum(
            len(filenames)
            for dirpath, dirnames, filenames in os.walk(tree)
            if os.path.basename(dirpath) == "__pycache__"
        )
        print(f"  __pycache__ files (excluded by tree_digest, allowed by AC4): {pyc}")
        print()

    print("=" * 78)
    if failures:
        print("AC4 RESULT: FAIL")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("AC4 RESULT: PASS")
    print("  - no store tree deviates from its recorded tree_digest")
    print("  - no file in any managed store tree is newer than the publication epoch")
    print("    (i.e. no NEW files outside __pycache__, which tree_digest excludes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
