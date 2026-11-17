#!/usr/bin/env python3
"""Audit the retained Hermes logs for Telegram sends made under the WRONG bot.

WHY THIS EXISTS (t_380891bd, phase 3, step 6)
------------------------------------------------
The t_43eff563 incident: a ``default``-profile worker posted to Telegram using
ULTRON's bot token, so a message that should have read as "Vision said X"
reached Bryan as "Ultron said X". The Identity Lock ("who may message whom")
depends on knowing WHICH bot spoke.

This script answers the question the card asks — "how many other messages went
out under the wrong bot?" — from whatever the retained logs can actually
prove, and is explicit about what they CANNOT prove.

WHAT THE LOGS CAN AND CANNOT SHOW
----------------------------------
Telegram request URLs are logged by httpx as::

    INFO httpx: HTTP Request: POST https://api.telegram.org/bot<id>:***/sendMessage

so a bot id appears in a log ONLY for a profile whose logging captures the
httpx request line. That is a per-profile property of the logging setup, not
a guarantee — so "no bot id in this log" means "this log does not record
outbound bot ids", NOT "no mis-post happened".

The audit therefore reports, per log file:
  * the bot ids that DID appear, with their sendMessage counts;
  * whether the file records outbound bot ids AT ALL;
  * a CROSS-CHECK: a bot id that is NOT the owning profile's own bot id is a
    candidate mis-post (the strong signal).

The default gateway's ``logs/gateway.log`` records Telegram NETWORK errors
but no httpx request lines, so it cannot answer "which bot posted" for its own
window. That is exactly the blind spot that let the incident go unnoticed,
and it is why the fix now LOGS the bot id on every send.

USAGE
-----
    python3 tools/audit_telegram_bot_identity.py [--json]

Exit codes: 0 = audit ran; 1 = a CROSS-CHECK hit (a candidate mis-post).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# A Telegram bot id is the numeric prefix of the token: ``123456789:AAH...``.
# The request URL form is ``/bot<id>:***/<verb>`` (httpx redacts the secret).
BOT_URL_RE = re.compile(r"bot(\d{6,12}):\*\*\*/([A-Za-z_]+)")

# The verbs that actually PUT something into a chat. getUpdates/getMe are
# INBOUND or metadata calls and say nothing about who posted.
OUTBOUND_VERBS = {
    "sendMessage", "sendPhoto", "sendDocument", "sendVideo", "sendAnimation",
    "sendAudio", "sendVoice", "sendMediaGroup", "sendRichMessage",
    "sendMessageDraft", "sendRichMessageDraft", "sendChatAction", "editMessageText",
}

# The bot id that is the profile's OWN, per profile home.
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _read_bot_id(home: Path) -> str | None:
    """The profile's own bot id, from its ``.env`` (the numeric prefix only)."""
    env = home / ".env"
    try:
        text = env.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(TOKEN_ENV + "="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            head = value.split(":", 1)[0].strip()
            if head.isdigit():
                return head
    return None


def _iter_log_files(hermes_home: Path) -> list[Path]:
    """Every retained log file under the Hermes home, newest first."""
    found: list[Path] = []
    logs = hermes_home / "logs"
    if logs.is_dir():
        found.extend(sorted(logs.glob("*.log*")))
    profiles = hermes_home / "profiles"
    if profiles.is_dir():
        for profile in sorted(profiles.iterdir()):
            plogs = profile / "logs"
            if plogs.is_dir():
                found.extend(sorted(plogs.glob("*.log*")))
    return found


def _owner_profile(path: Path, hermes_home: Path) -> str | None:
    """The profile a log belongs to, or None for the root/default home."""
    try:
        rel = path.relative_to(hermes_home)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "profiles":
        return parts[1]
    return "default"


def audit(hermes_home: Path) -> dict:
    """Scan every retained log and report the bot ids seen in each."""
    files = _iter_log_files(hermes_home)
    per_file = []
    hits = []
    total_send_calls = 0

    for path in files:
        owner = _owner_profile(path, hermes_home)
        owner_bot = None
        if owner:
            owner_home = (hermes_home if owner == "default"
                          else hermes_home / "profiles" / owner)
            owner_bot = _read_bot_id(owner_home)

        counts: dict[str, int] = {}
        verbs: dict[str, set] = {}
        first_seen = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    # Only a REAL API request line counts. A tool result that merely QUOTES
                    # a Telegram URL (an agent grepping the logs) is logged at WARNING by
                    # ``agent.tool_executor`` and would otherwise read as a mis-post.
                    if "HTTP Request:" not in line:
                        continue
                    for bot_id, verb in BOT_URL_RE.findall(line):
                        counts[bot_id] = counts.get(bot_id, 0) + 1
                        verbs.setdefault(bot_id, set()).add(verb)
                        if verb in OUTBOUND_VERBS:
                            total_send_calls += 1
                            if first_seen is None:
                                first_seen = line[:19]
                            if owner_bot and bot_id != owner_bot:
                                hits.append({
                                    "file": str(path),
                                    "owner_profile": owner,
                                    "owner_bot_id": owner_bot,
                                    "observed_bot_id": bot_id,
                                    "verb": verb,
                                    "timestamp": line[:19],
                                })
        except OSError:
            continue

        if counts:
            per_file.append({
                "file": str(path),
                "owner_profile": owner,
                "owner_bot_id": owner_bot,
                "records_outbound_bot_ids": True,
                "first_timestamp": first_seen,
                "bot_ids": {bid: {
                    "calls": n,
                    "verbs": sorted(verbs.get(bid, ())),
                } for bid, n in sorted(counts.items())},
            })

    return {
        "hermes_home": str(hermes_home),
        "files_scanned": len(files),
        "files_recording_bot_ids": len(per_file),
        "total_outbound_calls_seen": total_send_calls,
        "cross_check_hits": hits,
        "per_file": per_file,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    args = parser.parse_args()

    hermes_home = Path(args.hermes_home).resolve()
    report = audit(hermes_home)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1 if report["cross_check_hits"] else 0

    print("Telegram outbound-identity audit")
    print(f"  hermes home      : {report['hermes_home']}")
    print(f"  log files scanned: {report['files_scanned']}")
    print(f"  files recording outbound bot ids: {report['files_recording_bot_ids']}")
    print(f"  outbound calls seen: {report['total_outbound_calls_seen']}")
    print()

    if not report["per_file"]:
        print("  No retained log records an outbound Telegram bot id.")
        print("  => The retained logs CANNOT answer 'which bot posted' for their window.")
        print("     The blind spot is why the fix now logs the bot id on every send.")
    for entry in report["per_file"]:
        print(f"  {entry['file']}")
        print(f"    owner profile      : {entry['owner_profile']} (own bot id: {entry['owner_bot_id']})")
        for bot_id, info in entry["bot_ids"].items():
            marker = ""
            if entry["owner_bot_id"] and bot_id != entry["owner_bot_id"]:
                marker = "   <-- NOT the owning profile's bot"
            print(f"    bot {bot_id}: {info['calls']} calls, verbs={','.join(info['verbs'])}{marker}")
    print()
    if report["cross_check_hits"]:
        print(f"  CROSS-CHECK HITS: {len(report['cross_check_hits'])}")
        for hit in report["cross_check_hits"]:
            print(f"    {hit['timestamp']}  {hit['owner_profile']} posted under bot {hit['observed_bot_id']}"
                  f" (its own is {hit['owner_bot_id']})  {hit['verb']}  {hit['file']}")
        return 1
    print("  CROSS-CHECK HITS: 0  (no log shows a profile posting under another profile's bot)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
