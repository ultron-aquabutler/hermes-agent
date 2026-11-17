#!/usr/bin/env python3
"""Audit every profile's Telegram bot identity — which bot would each profile post as?

Answers the question the t_43eff063 mis-post made urgent: "is the bot about to speak
actually ME?" The bot id is NOT a secret (the token is), so this script is safe to run and
safe to paste into a ticket.

For each profile under the hermes home it reports:

* the bot id that profile's OWN ``.env`` defines (its identity), and
* the bot id the ambient process environment would resolve to (what the gateway would use
  if the profile's own ``.env`` did not win), and
* whether the two AGREE.

A MISMATCH is the t_43eff063 signature: the profile's identity and the identity the process
would authenticate with point at different bots.

Usage:
    python tools/audit_telegram_bot_identity.py [--home ~/.hermes] [--json]

Exit codes:
    0 — every profile's identity matches (or there is nothing to audit)
    1 — at least one profile has an identity mismatch
    2 — the audit could not run (bad home, unreadable profile)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _profile_env_values(env_path: Path) -> dict[str, str]:
    """The values a profile's own ``.env`` defines."""
    try:
        from agent.secret_scope import load_env_file

        return load_env_file(env_path)
    except Exception:
        # Fall back to a minimal parse so the audit still works without the agent package.
        values: dict[str, str] = {}
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
        except Exception:
            pass
        return values


def _profile_dirs(home: Path) -> list[tuple[str, Path]]:
    """(name, env_path) for the default profile and every profile under ``profiles/``."""
    found: list[tuple[str, Path]] = []
    default_env = home / ".env"
    if default_env.exists():
        found.append(("default", default_env))
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            env_path = child / ".env"
            if child.is_dir() and env_path.exists():
                found.append((child.name, env_path))
    return found


def audit(home: Path, ambient: dict[str, str] | None = None) -> dict:
    """The audit result for every profile under ``home``."""
    from tools.telegram_identity import bot_id_from_token

    ambient = os.environ if ambient is None else ambient
    profiles: list[dict] = []
    for name, env_path in _profile_dirs(home):
        own = _profile_env_values(env_path)
        own_token = own.get(TELEGRAM_TOKEN_ENV)
        own_bot_id = bot_id_from_token(own_token)
        ambient_token = ambient.get(TELEGRAM_TOKEN_ENV)
        ambient_bot_id = bot_id_from_token(ambient_token)
        profiles.append({
            "profile": name,
            "env_path": str(env_path),
            "own_bot_id": own_bot_id,
            "ambient_bot_id": ambient_bot_id,
            # A mismatch is only meaningful when the profile HAS its own identity and the
            # ambient value points at a DIFFERENT bot.
            "mismatch": bool(own_bot_id and ambient_bot_id and own_bot_id != ambient_bot_id),
            "has_own_token": bool(own_token),
            "has_ambient_token": bool(ambient_token),
        })
    return {
        "home": str(home),
        "profiles": profiles,
        "mismatches": [p["profile"] for p in profiles if p["mismatch"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--home", default=None, help="the hermes home to audit (default: $HERMES_HOME)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    ).expanduser()
    if not home.is_dir():
        print(f"hermes home not found: {home}", file=sys.stderr)
        return 2

    try:
        result = audit(home)
    except Exception as exc:  # the audit reports, it does not crash the run
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Telegram bot identity audit — home: {result['home']}")
        if not result["profiles"]:
            print("  (no profiles with a .env — nothing to audit)")
        for p in result["profiles"]:
            own = p["own_bot_id"] or "(none)"
            ambient = p["ambient_bot_id"] or "(none)"
            flag = "  <-- MISMATCH" if p["mismatch"] else ""
            print(f"  {p['profile']:<12} own_bot_id={own:<14} ambient_bot_id={ambient}{flag}")

    return 1 if result["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
