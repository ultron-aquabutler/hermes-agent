"""A failed config load hands ``_parse_compression_config`` ``{}``; every key must fall back to
DEFAULT_CONFIG, and an explicit ``threshold_tokens: null`` must stay the ratio-only opt-out."""

from types import SimpleNamespace

import pytest

from agent.agent_init import _parse_compression_config
from hermes_cli.config import DEFAULT_CONFIG


def _agent():
    return SimpleNamespace(model="m", provider="openrouter", api_mode="chat_completions", quiet_mode=True)


@pytest.mark.parametrize(
    ("agent_cfg", "expected"),
    [
        ({}, DEFAULT_CONFIG["compression"]["threshold_tokens"]),  # config-load failure → shipped default
        ({"compression": {"threshold_tokens": None}}, None),  # explicit null → ratio-only opt-out
    ],
)
def test_threshold_tokens_default_and_null_opt_out(agent_cfg, expected):
    cs = _parse_compression_config(_agent(), agent_cfg)
    assert cs.threshold_tokens == expected
    assert cs.threshold == DEFAULT_CONFIG["compression"]["threshold"]


def test_min_interval_seconds_falls_back_to_the_shipped_default():
    """A failed config load must yield the shipped floor, not an unlimited one.

    ``min_interval_seconds`` is the floor between two automatic compactions. A
    missing key must NOT mean "no floor" — that would silently remove the
    cadence brake from every profile that failed its config load.
    """
    cs = _parse_compression_config(_agent(), {})
    assert cs.min_interval_seconds == DEFAULT_CONFIG["compression"]["min_interval_seconds"]
    assert cs.min_interval_seconds == 120


def test_min_interval_seconds_is_read_from_the_config():
    """A profile that sets the floor gets it; the value reaches the settings object."""
    cs = _parse_compression_config(_agent(), {"compression": {"min_interval_seconds": 600}})
    assert cs.min_interval_seconds == 600


def test_a_negative_min_interval_seconds_is_clamped_to_zero():
    """A negative floor is meaningless; 0 means "no floor", not "always blocked"."""
    cs = _parse_compression_config(_agent(), {"compression": {"min_interval_seconds": -30}})
    assert cs.min_interval_seconds == 0
