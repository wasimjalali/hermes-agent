"""Burooj-owned config text, kept out of hermes_cli/config.py.

This fork stays upstream-mergeable (see AGENTS.md and the mode architecture
spec §2: "Hermes runtime, unchanged, upstream-mergeable"). Every Burooj
addition inside a shared Hermes module is a future merge conflict, so the
commented template lives here and ``config.py`` only imports it and appends
it. That keeps the diff against upstream at two lines instead of seventeen.

The keys themselves are schema defaults in ``config_defaults.DEFAULT_CONFIG``
and are never written to disk: ``_persist_migration`` forbids materialising
pure defaults, and ``load_config()`` deep-merges them at read time. This block
is how a user discovers the keys exist.
"""

BUROOJ_COMMENT = """
# ── Burooj mode routing ───────────────────────────────────────────────
# Build ("coding") and Design ("vision") model overrides. Empty or omitted
# hints fall back to the default model. vlm_critique is opt-in advisory
# only; it never blocks the design gate. Defaults live in DEFAULT_CONFIG
# and are not written to disk until you set a real value.
#
# burooj:
#   model_hints:
#     coding: ""    # e.g. anthropic/claude-sonnet-4-5
#     vision: ""    # e.g. openrouter/google/gemini-2.5-flash
#   vlm_critique: false
"""
