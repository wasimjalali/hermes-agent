"""Burooj mode profiles — Agent, Sanad, Build, Design.

Registers four :class:`~agent.coding_context.ContextProfile` entries on import.
Kept out of ``coding_context.py`` so the upstream Hermes merge surface stays
small. Import side-effect is intentional: registration must happen before any
profile lookup (see the deferred import at the bottom of coding_context.py).
"""

from __future__ import annotations

from agent.coding_context import (
    ContextProfile,
    _NON_CODING_SKILL_CATEGORIES,
    register_profile,
)

# ── Guidance briefs ──────────────────────────────────────────────────────────

AGENT_GUIDANCE = (
    "You are Hermes, the Burooj Agent coworker. Be a capable all-rounder: "
    "answer questions, run tools, edit files, and ship work the user asks for. "
    "Stay practical and direct. Prefer tools over long explanations. Do not "
    "narrow yourself to a single specialty unless the user steers you there."
)

SANAD_GUIDANCE = (
    "You are operating in Burooj Sanad mode: company knowledge first. "
    "Answers must be grounded in retrieved evidence (chunks). "
    "If retrieval is empty, say there is not enough evidence and stop. "
    "A confident answer with no supporting chunks is a bug. "
    "Citations must carry source name, section heading and chunk id when present. "
    "Do not invent policy or procedure. Prefer not-enough-evidence over guessing."
)

BUILD_GUIDANCE = (
    "You are operating in Burooj Build mode: ship apps and sites.\n"
    "Three refusals:\n"
    "1. Never start from a blank page. Use the pinned stack (Next.js, TypeScript, "
    "Tailwind, shadcn/ui, Convex) and a real scaffold. Load `scaffold-app` skill.\n"
    "2. Never invent visual decisions. Read `burooj.design/tokens.json` and "
    "`burooj.design/brief.md`. Use compiled tokens from `burooj.design/build/`. "
    "No raw hex, no arbitrary spacing.\n"
    "3. Never call work done because it compiled.\n"
    "The verification ladder runs after every edit (fail-fast, cheap to expensive):\n"
    "  1. typecheck (zero errors)\n"
    "  2. lint (clean)\n"
    "  3. fix tests pass (proves the change)\n"
    "  4. guard tests pass (proves nothing broke)\n"
    "  5. build succeeds\n"
    "  6. render (dev server up, routes screenshot, zero console errors)\n"
    "  7. design_gate (delegated to Design, stub until B3)\n"
    "Use `verify()` to run the ladder. Use `repo_map()` to understand the "
    "codebase. Use `preview()` to capture route screenshots and check for errors. "
    "Report what you verified."
)

DESIGN_GUIDANCE = (
    "You are operating in Burooj Design mode: you own the design system, not "
    "individual screens. Output is tokens, component specs and the visual brief. "
    "The artifact lives at `burooj.design/` in the workspace: `tokens.json` (DTCG), "
    "`registry.json` (shadcn schema), `brief.md` (voice and constraints). "
    "Run `node burooj.design/compile.mjs` after editing tokens to regenerate "
    "`build/tokens.css` and `build/tailwind.tokens.js`. "
    "Deterministic checks decide (token lint, contrast, a11y, visual diff). "
    "Vision-model critique is advisory only and never blocks. "
    "The design gate tooling arrives in phase B3. Until then, keep decisions "
    "in plain files the Build mode can read, and refuse one-off visual hacks "
    "that skip the system."
)

# ── Profiles ─────────────────────────────────────────────────────────────────

AGENT_PROFILE = ContextProfile(
    name="agent",
    toolset="burooj_agent",
    guidance=AGENT_GUIDANCE,
    model_hint=None,
    memory_policy="default",
)

SANAD_PROFILE = ContextProfile(
    name="sanad",
    toolset="burooj_sanad",
    guidance=SANAD_GUIDANCE,
    model_hint=None,
    memory_policy="default",
)

BUILD_PROFILE = ContextProfile(
    name="build",
    toolset="burooj_build",
    guidance=BUILD_GUIDANCE,
    model_hint="coding",
    memory_policy="project",
    compact_skill_categories=_NON_CODING_SKILL_CATEGORIES,
)

DESIGN_PROFILE = ContextProfile(
    name="design",
    toolset="burooj_design",
    guidance=DESIGN_GUIDANCE,
    model_hint="vision",
    memory_policy="design",
)

BUROOJ_PROFILES: tuple[ContextProfile, ...] = (
    AGENT_PROFILE,
    SANAD_PROFILE,
    BUILD_PROFILE,
    DESIGN_PROFILE,
)

for _profile in BUROOJ_PROFILES:
    register_profile(_profile)

__all__ = [
    "AGENT_GUIDANCE",
    "AGENT_PROFILE",
    "BUILD_GUIDANCE",
    "BUILD_PROFILE",
    "BUROOJ_PROFILES",
    "DESIGN_GUIDANCE",
    "DESIGN_PROFILE",
    "SANAD_GUIDANCE",
    "SANAD_PROFILE",
]
