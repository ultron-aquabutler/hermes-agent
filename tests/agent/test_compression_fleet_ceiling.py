"""Regression tests for the fleet compression ceiling.

The ceiling exists because a 1M-token window (MiniMax-M3) with the ratio trigger at 0.5
compacts at ~500,000 tokens — a point no long session ever reached, so compaction never fired
and every call re-read the whole growing context from the prompt cache (84% of fleet spend,
~$87/day). The config-level fix (``compression.threshold_tokens = 120000``) stopped the burn,
but a config value is only as durable as the config file: a reset, a new profile, or a
hand-edited YAML reopens the hole.

These tests pin the CODE-level guarantee: the trigger can never exceed the ceiling, whatever
any config says.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.compression_fleet_ceiling import (
    FLEET_COMPRESSION_CEILING_TOKENS,
    clamp_to_fleet_ceiling,
    fleet_compression_ceiling,
)


# --- the ceiling itself -------------------------------------------------------

def test_ceiling_is_120k():
    """120K: the value measured against the fleet's spend curve and MINIMUM_CONTEXT_LENGTH."""
    assert FLEET_COMPRESSION_CEILING_TOKENS == 120_000


def test_ceiling_is_below_the_ratio_trigger_of_a_1m_window():
    """The whole point: the ratio trigger on a 1M window is ~500K, far above the ceiling."""
    ratio_trigger = int(1_000_000 * 0.5)
    assert ratio_trigger == 500_000
    assert FLEET_COMPRESSION_CEILING_TOKENS < ratio_trigger


def test_ceiling_clears_minimum_context_length():
    """A compacted session must not immediately re-trip the trigger (no compaction thrash)."""
    MINIMUM_CONTEXT_LENGTH = 64_000
    assert FLEET_COMPRESSION_CEILING_TOKENS > MINIMUM_CONTEXT_LENGTH


def test_fleet_ceiling_clamps_to_a_small_window():
    assert fleet_compression_ceiling(64_000) == 64_000
    assert fleet_compression_ceiling(200_000) == FLEET_COMPRESSION_CEILING_TOKENS
    assert fleet_compression_ceiling(1_000_000) == FLEET_COMPRESSION_CEILING_TOKENS


def test_fleet_ceiling_with_unknown_window_returns_the_ceiling():
    assert fleet_compression_ceiling(None) == FLEET_COMPRESSION_CEILING_TOKENS
    assert fleet_compression_ceiling(0) == FLEET_COMPRESSION_CEILING_TOKENS


def test_clamp_only_ever_tightens():
    """A configured cap above the ceiling is clamped down; one below is left alone."""
    assert clamp_to_fleet_ceiling(500_000) == FLEET_COMPRESSION_CEILING_TOKENS
    assert clamp_to_fleet_ceiling(256_000) == FLEET_COMPRESSION_CEILING_TOKENS
    assert clamp_to_fleet_ceiling(60_000) == 60_000
    assert clamp_to_fleet_ceiling(None) is None


# --- the compressor integration ------------------------------------------------

def _compressor(cap):
    """A minimal stand-in exposing the real ``_effective_threshold_cap`` logic."""
    from agent.context_compressor import ContextCompressor

    obj = ContextCompressor.__new__(ContextCompressor)
    obj.threshold_tokens_cap = cap
    return obj


@pytest.mark.parametrize("configured", [None, 0, 256_000, 500_000, 1_000_000, 2_000_000])
def test_effective_cap_never_exceeds_the_ceiling(configured):
    """Whatever the config says, the effective trigger is at or below the ceiling."""
    cap = _compressor(configured)._effective_threshold_cap(1_000_000)
    assert cap is not None
    assert cap <= FLEET_COMPRESSION_CEILING_TOKENS


def test_effective_cap_applies_the_ceiling_even_with_no_configured_cap():
    """No configured cap must NOT mean 'no ceiling' — that was the original bug."""
    assert _compressor(None)._effective_threshold_cap(1_000_000) == FLEET_COMPRESSION_CEILING_TOKENS
    assert _compressor(0)._effective_threshold_cap(1_000_000) == FLEET_COMPRESSION_CEILING_TOKENS


def test_effective_cap_respects_a_tighter_configured_cap():
    """A config may tighten the trigger below the ceiling, never raise it."""
    assert _compressor(60_000)._effective_threshold_cap(1_000_000) == 60_000
    assert _compressor(90_000)._effective_threshold_cap(1_000_000) == 90_000


def test_effective_cap_is_clamped_to_the_window():
    """A 200K window cannot have a 120K... it can, but a 64K window cannot have 120K."""
    assert _compressor(None)._effective_threshold_cap(64_000) == 64_000
    assert _compressor(None)._effective_threshold_cap(200_000) == FLEET_COMPRESSION_CEILING_TOKENS


# --- the config default --------------------------------------------------------

def test_default_config_carries_the_ceiling():
    """DEFAULT_CONFIG is the base every profile config merges onto, so it closes the
    'profile configs do not inherit root config' gap for present AND future profiles."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["threshold_tokens"] == FLEET_COMPRESSION_CEILING_TOKENS


def test_default_config_hygiene_hard_message_limit_is_tightened():
    """The 'no token data' backstop: 5000 let a session grow unbounded."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["hygiene_hard_message_limit"] == 400


def test_proactive_prune_tokens_stays_off():
    """Friday measured summary-failure risk with the prune on: it stays off."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["proactive_prune_tokens"] == 0


def test_runaway_guard_keys_are_registered_in_defaults():
    """AGENTS.md: every DEFAULT_CONFIG key needs a runtime reader AND a registry entry."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    compression = DEFAULT_CONFIG["compression"]
    for key in (
        "runaway_max_segment_hours",
        "runaway_max_live_chars",
        "runaway_max_session_messages",
    ):
        assert key in compression, f"{key} missing from DEFAULT_CONFIG['compression']"
        assert isinstance(compression[key], int)


def test_every_new_default_key_has_a_runtime_reader():
    """The drift check AGENTS.md asks for: each new key is actually read somewhere."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    for key in (
        "runaway_max_segment_hours",
        "runaway_max_live_chars",
        "runaway_max_session_messages",
    ):
        assert f'"{key}"' in (Path(__file__).resolve().parents[2] / "gateway" / "runaway_session_guard.py").read_text()
