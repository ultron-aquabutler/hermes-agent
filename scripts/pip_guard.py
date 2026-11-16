#!/usr/bin/env python3
"""The pip guard: refuse to install into Hermes' managed Python store.

The managed store (``~/.hermes/tools/<package>-<version>-<platform>/``) is a
PUBLISHED artifact.  Its realized content is hashed and the hash is recorded
in ``~/.hermes/tools/facts.json``, so every ``hermes pm doctor`` run verifies
the store against that digest.  A bare ``pip install`` against the store's own
interpreter writes into the published tree and invalidates the digest -- which
has already happened twice on this host (psycopg 2026-10-24, 26 files;
pyyaml 2026-10-25, 124 files).

This wrapper is installed at ``~/.local/bin/pip``, which precedes the store's
``bin/`` on PATH.  It refuses the unscoped install and passes everything else
through unchanged.

Contract (see the Obsidian wiki page
``Homelab/Hermes/Services/hermes-pm-dependency-environment``):

======================================  =========================================
Invocation                              Behaviour
======================================  =========================================
``pip install <pkg>`` (bare)             REFUSED, with the supported alternatives
``pip --python <venv>/bin/python install <pkg>``   passes through
``pip install --python <venv>/bin/python <pkg>``   passes through
``pip install --target <dir> <pkg>``     passes through (deliberately scoped)
``pip check`` / ``pip list`` / ``pip --version`` / ``pip download`` / ...  pass through
======================================  =========================================

Note pip's own ordering rule: ``--python`` is a GENERAL option and must come
BEFORE the subcommand (``pip --python <path> install ...``).  pip also accepts
it after the subcommand, so this guard accepts both orderings.

Exit status on refusal: 2 (pip's own usage-error code), with the message on
stderr.
"""

from __future__ import annotations

import os
import sys

# Subcommands that mutate an environment.  Only `install` needs a scope
# check; `uninstall` removes packages (it cannot ADD bytes to the published
# tree, but it can still change the digest) -- see _SCOPED_MUTATORS.
_INSTALL = "install"
_UNINSTALL = "uninstall"

# General options that scope the operation to somewhere other than the
# interpreter this wrapper would otherwise mutate.
_SCOPE_OPTIONS = ("--python", "--target", "--prefix", "--root")

# How to find the real pip to delegate to.  Prefer the interpreter pip is a
# sibling of, so `pip --version` keeps reporting the store's own pip.
def _find_real_pip() -> str:
    """The pip this wrapper shadows, or the one on PATH behind it."""
    candidates = []

    # pip next to the interpreter that would run it, resolved from PATH.
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        cand = os.path.join(entry, "pip")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            candidates.append(cand)

    # Skip ourselves (and any wrapper that is simply this file).
    me = os.path.realpath(__file__)
    for cand in candidates:
        if os.path.realpath(cand) != me:
            return cand

    # Fall back to the module the wrapper's own interpreter provides.
    return ""


def _has_scope(argv: list[str]) -> str | None:
    """The scope option present in argv, or None if the install is unscoped."""
    for arg in argv:
        for opt in _SCOPE_OPTIONS:
            if arg == opt or arg.startswith(opt + "="):
                return opt
    return None


def _subcommand(argv: list[str]) -> str | None:
    """The pip subcommand, skipping general options and their values.

    pip's general options take a value (``--python <path>``), so a naive
    "first non-dash token" scan can mistake the option's value for the
    subcommand.
    """
    value_opts = {"--python", "--target", "--prefix", "--root", "--index-url",
                  "-i", "--extra-index-url", "--proxy", "--timeout", "--retries",
                  "--log", "--exists-action", "--implementation", "--abi",
                  "--platform", "--python-version", "--use-feature"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in value_opts:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return None


def _refuse() -> int:
    sys.stderr.write(
        "pip: refusing to install into the Hermes managed Python store.\n"
        "\n"
        "The managed store (~/.hermes/tools/<package>-<version>-<platform>/) is a\n"
        "published artifact: its content is hashed and recorded in\n"
        "~/.hermes/tools/facts.json, and every `hermes pm doctor` verifies that\n"
        "digest. Installing into it invalidates the digest for every install on\n"
        "this host.\n"
        "\n"
        "Supported alternatives:\n"
        "  pip --python <other-venv>/bin/python install <pkg>   install into that venv\n"
        "  pip install --target <dir> <pkg>                    install into a directory\n"
        "  hermes pm install                                   sync a checkout's dependencies\n"
        "  hermes pm repair                                    rebuild a damaged install\n"
        "\n"
        "Read-only commands (check, list, download, show, freeze, ...) pass through.\n"
    )
    return 2


def main(argv: list[str]) -> int:
    sub = _subcommand(argv)

    if sub == _INSTALL:
        scope = _has_scope(argv)
        if scope is None:
            return _refuse()
    elif sub == _UNINSTALL:
        # Uninstall also changes the store's content. It must be scoped the
        # same way an install is.
        scope = _has_scope(argv)
        if scope is None:
            return _refuse()

    real = _find_real_pip()
    if not real:
        sys.stderr.write("pip: no real pip found to delegate to\n")
        return 2

    os.execv(real, [real] + argv)
    return 2  # unreachable unless execv fails


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
