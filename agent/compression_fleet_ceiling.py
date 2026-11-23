"""Fleet ceiling on the compaction trigger — the guard that outlives any config.

**Why this exists.** A 1M-token window (MiniMax-M3) with the ratio trigger at 0.5 puts the
compaction trigger at ~500,000 tokens. Measured over 2026-11-11: 100% of sampled long Kanban
sessions had ZERO compaction events, because their observed per-call context peaked at
130k-168k tokens and never reached the 500k trigger. Every subsequent API call then re-read the
entire growing context from the prompt cache — 84% of fleet spend was cache-read, ~$87/day.

The config-level fix (``compression.threshold_tokens = 120000`` on every profile) stopped the burn,
but a config value is only as durable as the config file: a ``config reset``, a new profile, or a
hand-edited YAML reopens the hole. This module makes the ceiling a property of the CODE, so the
trigger can never exceed it no matter what any config says.

**How it binds.** ``ContextCompressor._effective_threshold_cap`` clamps the effective cap to
``min(configured_cap, FLEET_CEILING)``. Because every compressor — the main agent, a delegated
child, a re-created agent after a model switch — resolves its trigger through that one method, the
ceiling holds on every path. A config value may only ever *tighten* the trigger below the
ceiling; it can never raise it above.

**The number.** 120,000 tokens. Chosen against the fleet's measured spend curve (24h fleet kanban
spend $80.59): 150k -> $64.43 (-20%), 120k -> $56.24 (-30%), 100k -> $49.87 (-38%). 120k keeps
the largest headroom that still clears ``MINIMUM_CONTEXT_LENGTH`` (64,000): post-compaction
footprint ~57,192 against a 120,000 trigger leaves ~62,808 tokens of headroom, so a compacted
session cannot immediately re-trip the trigger (no compaction thrash).

**Verified against the live fleet after the cap landed (2026-11-16).** Fleet-wide average
cache-read per API call fell from 145,090 (2026-11-11) and 170,871 (2026-11-12) to 87,648
(2026-11-16); the gateway logged 14 compaction events in 24h with a zero event count before
the cap. See the Obsidian page ``Homelab/10-Services/hermes/compaction-fleet-ceiling.md``.
"""

from __future__ import annotations

# The absolute ceiling on the compaction trigger, in tokens. A configured
# ``compression.threshold_tokens`` may only lower this, never raise it.
FLEET_COMPRESSION_CEILING_TOKENS = 120_000

#: The same ceiling under the name the config uses, for messages and tests.
FLEET_CEILING = FLEET_COMPRESSION_CEILING_TOKENS


def fleet_compression_ceiling(context_length: int | None = None) -> int | None:
    """The fleet ceiling, clamped to the model's window.

    ``None`` when the window is unknown or smaller than the ceiling — a small window's own
    trigger is already below the ceiling, so clamping it there would only shrink an already
    tight budget for no benefit. The compressor clamps to the window separately.
    """
    if not context_length or context_length <= 0:
        return FLEET_COMPRESSION_CEILING_TOKENS
    return min(FLEET_COMPRESSION_CEILING_TOKENS, int(context_length))


def clamp_to_fleet_ceiling(cap: int | None, context_length: int | None = None) -> int | None:
    """Clamp a configured absolute cap to the fleet ceiling.

    ``None`` in, ``None`` out: no configured cap means the ceiling still applies, but this
    function only ever *tightens an existing cap* — the caller applies the ceiling itself
    (the ceiling is a floor on the trigger, not a cap the operator can remove).

    A configured cap ABOVE the ceiling is clamped down to it; a configured cap below it is left
    alone. That is the whole contract: config may tighten, never loosen.
    """
    if cap is None:
        return None
    ceiling = fleet_compression_ceiling(context_length)
    assert ceiling is not None  # fleet_compression_ceiling never returns None
    return min(int(cap), ceiling)
