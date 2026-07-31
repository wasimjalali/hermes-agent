"""Burooj mode profiles — Agent, Sanad, Build, Design.

Registers four :class:`~agent.coding_context.ContextProfile` entries on import.
Kept out of ``coding_context.py`` so the upstream Hermes merge surface stays
small. Import side-effect is intentional: registration must happen before any
profile lookup (see the deferred import at the bottom of coding_context.py).
"""

from __future__ import annotations

from dataclasses import replace

from agent.coding_context import (
    ContextProfile,
    _NON_CODING_SKILL_CATEGORIES,
    register_profile,
)

# Skill categories demoted to names-only under Design's focus mode. Design is
# not a coding posture, so the coding deny-list is the wrong shape: keep the
# creative and media categories a designer reaches for, drop the rest.
_NON_DESIGN_SKILL_CATEGORIES = (
    "apple", "communication", "cooking", "email", "finance", "gaming",
    "health", "music", "note-taking", "shopping", "smart-home",
    "social-media", "travel", "yuanbao",
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
    "  1. install (dependencies present)\n"
    "  2. typecheck (zero errors)\n"
    "  3. lint (clean)\n"
    "  4. fix tests pass (proves the change)\n"
    "  5. guard tests pass (proves nothing broke)\n"
    "  6. build succeeds\n"
    "  7. render (dev server up, every route responds, no server errors)\n"
    "  8. design_gate (token lint, contrast, a11y, visual diff)\n"
    "Use `verify()` to run the ladder; pass `rungs` to re-check one thing fast. "
    "A rung reporting `skip` means the project declares no such step. A rung "
    "reporting `error` means the check could not run, which is never a pass.\n"
    "Use `repo_map()` first in an unfamiliar codebase, before reading files. "
    "Use `preview()` to see what a route actually renders and what the console "
    "and dev server report. "
    "Use `sanad_search()` to look up company standards, brand guidelines, or "
    "product requirements before guessing. "
    "Report what you verified, naming the rungs."
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
    "Use `element_map()` to stamp the running app with data-oid and "
    "`visual_edit()` to patch the JSX behind an element; edits land in source "
    "and are verified by re-parsing. "
    "Use `sanad_search()` to look up brand guidelines, accessibility policies, "
    "or design standards from the company knowledge base before making decisions. "
    "Refuse one-off visual hacks that skip the system."
)

# ── Profiles ─────────────────────────────────────────────────────────────────

# Agent is the odd one out and deliberately so.
#
# ``resolve_runtime_mode`` lets "agent" fall through to auto-detection rather
# than pinning this profile, because pinning it would strip the coding posture
# in a code workspace. The consequence is that this profile is never the
# resolved profile, and its guidance would never reach a prompt.
#
# Registering it anyway is not decoration: ``get_profile("agent")`` is a real
# lookup path (session resume, gateway introspection, tests), and the toolset
# name is what the desktop shows. What must not happen is Agent mode silently
# operating with no brief at all, which is what happened in a non-code
# directory where detection landed on ``general``. So the brief is appended to
# the two profiles detection can actually pick, below.
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
    compact_skill_categories=_NON_DESIGN_SKILL_CATEGORIES,
)

BUROOJ_PROFILES: tuple[ContextProfile, ...] = (
    AGENT_PROFILE,
    SANAD_PROFILE,
    BUILD_PROFILE,
    DESIGN_PROFILE,
)

for _profile in BUROOJ_PROFILES:
    register_profile(_profile)


def with_agent_brief(profile: ContextProfile) -> ContextProfile:
    """Return *profile* with the Burooj Agent brief appended to its guidance.

    Agent mode resolves through auto-detection, so it lands on ``coding`` or
    ``general``. Without this, selecting Agent in the desktop gave the user
    either plain base-Hermes coding guidance or, in a non-code directory, no
    guidance whatsoever, and AGENT_GUIDANCE was dead text.

    Appending rather than replacing is the point: the coding posture is what
    makes Agent good in a code workspace, and the Burooj brief adds the
    product framing on top instead of overwriting it.

    Returns a NEW profile and never mutates the registry. The shared ``coding``
    and ``general`` entries belong to base Hermes; re-registering modified
    copies of them changed behaviour for every non-Burooj caller and broke
    upstream identity assertions. ``resolve_runtime_mode`` calls this only on
    the Burooj Agent path.
    """
    existing = profile.guidance.strip()
    combined = f"{existing}\n\n{AGENT_GUIDANCE}" if existing else AGENT_GUIDANCE
    return replace(profile, guidance=combined)


def resolve_model_for_hint(
    hint: Optional[str], cfg: Optional[dict] = None
) -> str:
    """Map a profile ``model_hint`` to a configured model id.

    Reads ``burooj.model_hints.<hint>`` from config.yaml. Returns an empty
    string when the hint is unset, the config section is missing, or the hint
    is unmapped, so the caller falls back to the default model selection
    instead of erroring.

    Parameters
    ----------
    hint : str, optional
        The ``ContextProfile.model_hint`` value (e.g. "coding", "vision").
    cfg : dict, optional
        The config dict (``burooj`` root). When None, reads
        ``burooj.model_hints`` from the user config via the gateway loaders.
    """
    if not hint:
        return ""
    if cfg is None:
        return ""  # no config source supplied: never guess a model
    try:
        hints = (cfg.get("burooj") or {}).get("model_hints") or {}
    except AttributeError:
        return ""
    if not isinstance(hints, dict):
        return ""
    value = hints.get(hint)
    if not isinstance(value, str):
        return ""
    return value.strip()

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
