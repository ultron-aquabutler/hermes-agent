"""The gateway must build a platform with the PROFILE'S OWN token, and say which bot it is.

Two properties, both about identity:

1. ``load_gateway_config`` re-asserts the profile's own ``.env`` token over an ambient value.
   The ambient environment is shared by every profile in the process, so a value another
   profile's ``.env`` wrote there can otherwise become THIS profile's bot token — the default
   profile builds its Telegram adapter with the ``ultron`` bot's token and posts to Bryan's DM
   as Ultron (t_43eff563).

2. The outbound adapter logs the numeric bot id it posts as. The bot id is NOT a secret (the
   token is), and it is the only way to answer "which bot spoke?" from a log. Before this
   landed, the only way was to count ``bot<id>/sendMessage`` URLs — which is exactly how the
   t_43eff563 mis-post went unnoticed for a day.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_home(tmp_path: Path, env_body: str, yaml_body: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(env_body, encoding="utf-8")
    if yaml_body:
        (home / "config.yaml").write_text(yaml_body, encoding="utf-8")
    return home


_TELEGRAM_YAML = "platforms:\n  telegram:\n    enabled: true\n"


def test_gateway_config_reasserts_the_profiles_own_token(tmp_path, monkeypatch):
    """An ambient token from another profile must not become this profile's platform token."""
    from gateway.config import Platform, load_gateway_config

    home = _write_home(tmp_path, "TELEGRAM_BOT_TOKEN=8851168207:AAA-own-bot\n", _TELEGRAM_YAML)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The shared process env holds a SIBLING profile's token.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8979456313:AAA-sibling-bot")

    cfg = load_gateway_config()

    assert cfg.platforms[Platform.TELEGRAM].token == "8851168207:AAA-own-bot", (
        "the gateway adopted a sibling profile's bot token from the shared environment — "
        "this profile would post as the wrong bot"
    )


def test_gateway_config_keeps_the_ambient_token_when_the_profile_defines_none(tmp_path, monkeypatch):
    """No own definition — the ambient value stands (the guard is narrow)."""
    from gateway.config import Platform, load_gateway_config

    home = _write_home(tmp_path, "HERMES_TEST_OTHER=value\n", _TELEGRAM_YAML)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8979456313:AAA-sibling-bot")

    cfg = load_gateway_config()

    assert cfg.platforms[Platform.TELEGRAM].token == "8979456313:AAA-sibling-bot"


def test_outbound_send_logs_the_bot_id(tmp_path, monkeypatch, caplog):
    """The adapter logs the bot id it posts as — the identity trail the t_43eff063 gap lacked."""
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, token="8851168207:AAA-own-bot")
    adapter.platform = Platform.TELEGRAM

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        adapter._note_outbound_send("-100123", "message")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "bot_id=8851168207" in text, f"the outbound log line did not name the bot id: {text}"
    # The token must never appear in the log.
    assert "AAA-own-bot" not in text, "the bot TOKEN leaked into the outbound log line"
