"""contrast_check tool - WCAG 2.1 contrast ratio verification over design tokens.

Reads burooj.design/tokens.json and checks every foreground/background token
pair against WCAG 2.1 AA minimum contrast ratios.

Tool schema:
    contrast_check(workspace: Path) -> { pairs: list[PairResult], passed: bool }

Each PairResult: { foreground: str, background: str, ratio: float, required: float, passed: bool }
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.build_workspace import resolve_workspace
from tools.registry import registry

logger = logging.getLogger("hermes.contrast_check")

# WCAG 2.1 AA thresholds.
_NORMAL_TEXT_RATIO = 4.5
_LARGE_TEXT_RATIO = 3.0
_UI_COMPONENT_RATIO = 3.0

# Known foreground/background pairs to check.
# Each entry: (foreground_path, background_path, required_ratio, description)
# Note on what is deliberately NOT here: decorative borders.
#
# WCAG 2.1 SC 1.4.11 asks for 3:1 on visual boundaries *required to identify a
# control*, not on every separator. Holding `color.border.default` to 3:1
# against the page background fails essentially every real design system,
# including this project's own default tokens, and a gate that no correct input
# can pass gets switched off. `border.strong` is checked as advisory instead,
# because a design that intends its strong border to delineate a control does
# want to know.
_TOKEN_PAIRS: list[tuple[str, str, float, str]] = [
    ("color.primary-foreground", "color.primary", _NORMAL_TEXT_RATIO, "primary text on primary bg"),
    ("color.secondary-foreground", "color.secondary", _NORMAL_TEXT_RATIO, "secondary text on secondary bg"),
    ("color.destructive-foreground", "color.destructive", _NORMAL_TEXT_RATIO, "destructive text on destructive bg"),
    ("color.accent-foreground", "color.accent", _NORMAL_TEXT_RATIO, "accent text on accent bg"),
    ("color.text.primary", "color.background.default", _NORMAL_TEXT_RATIO, "primary text on default bg"),
    ("color.text.secondary", "color.background.default", _NORMAL_TEXT_RATIO, "secondary text on default bg"),
    ("color.text.primary", "color.background.muted", _NORMAL_TEXT_RATIO, "primary text on muted bg"),
    ("color.text.secondary", "color.background.muted", _NORMAL_TEXT_RATIO, "secondary text on muted bg"),
    ("color.text.primary", "color.background.subtle", _NORMAL_TEXT_RATIO, "primary text on subtle bg"),
]

# Checked and reported, but never fails the gate. `text.muted` is legitimately
# used for large or non-essential text where 4.5:1 does not apply, and the
# tool cannot see which.
_ADVISORY_PAIRS: list[tuple[str, str, float, str]] = [
    ("color.text.muted", "color.background.default", _NORMAL_TEXT_RATIO, "muted text on default bg"),
    ("color.border.strong", "color.background.default", _UI_COMPONENT_RATIO, "strong border on default bg"),
]


@dataclass
class PairResult:
    """Result of checking one foreground/background pair."""

    foreground: str
    background: str
    fg_value: str
    bg_value: str
    ratio: float
    required: float
    passed: bool
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "foreground": self.foreground,
            "background": self.background,
            "fg_value": self.fg_value,
            "bg_value": self.bg_value,
            "ratio": round(self.ratio, 2),
            "required": self.required,
            "passed": self.passed,
            "description": self.description,
        }


class ColorParseError(ValueError):
    """Raised when a token value is not a color this tool can evaluate."""


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3,8})$")


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    """Convert a hex color string to normalized RGB (0-1 range).

    Raises :class:`ColorParseError` on anything that is not a 3, 4, 6 or 8
    digit hex color. The first cut fed unvalidated text straight to ``int(...,
    16)``, so a single typo'd token raised a bare ValueError that escaped
    contrast_check, escaped the design gate, and took down the whole verify
    ladder with a traceback.
    """
    match = _HEX_RE.match(hex_color.strip())
    if not match:
        raise ColorParseError(f"not a hex color: {hex_color!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    elif len(digits) == 4:
        digits = "".join(c * 2 for c in digits[:3])
    elif len(digits) == 8:
        digits = digits[:6]  # Strip alpha.
    elif len(digits) != 6:
        raise ColorParseError(f"hex color must have 3, 4, 6 or 8 digits: {hex_color!r}")

    r = int(digits[0:2], 16) / 255.0
    g = int(digits[2:4], 16) / 255.0
    b = int(digits[4:6], 16) / 255.0
    return (r, g, b)


def _linearize(channel: float) -> float:
    """Linearize an sRGB channel value."""
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(r: float, g: float, b: float) -> float:
    """Compute WCAG relative luminance."""
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _contrast_ratio(fg: str, bg: str) -> float:
    """Compute WCAG 2.1 contrast ratio between two color values."""
    l1 = _relative_luminance(*_parse_color(fg))
    l2 = _relative_luminance(*_parse_color(bg))

    # Lighter luminance goes on top.
    lighter = max(l1, l2)
    darker = min(l1, l2)

    return (lighter + 0.05) / (darker + 0.05)


_ALIAS_RE = re.compile(r"^\{([^}]+)\}$")

# Color functions this tool understands well enough to convert.
_OKLCH_RE = re.compile(
    r"^oklch\(\s*([\d.]+%?)\s+([\d.]+)\s+([\d.]+)", re.IGNORECASE
)
_RGB_RE = re.compile(
    r"^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)", re.IGNORECASE
)


def _raw_token_value(tokens: dict[str, Any], path: str, _depth: int = 0) -> Optional[str]:
    """Resolve a dot-separated token path to its raw ``$value`` string.

    Follows DTCG alias references (``{color.brand.500}``) up to a small depth,
    so a token tree that uses aliases resolves instead of silently reporting
    nothing to check.
    """
    if _depth > 8:  # alias cycle
        return None

    current: Any = tokens
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None

    if isinstance(current, dict) and "$value" in current:
        current = current["$value"]
    if not isinstance(current, str):
        return None

    if (alias := _ALIAS_RE.match(current.strip())) is not None:
        return _raw_token_value(tokens, alias.group(1), _depth + 1)
    return current.strip()


def _oklch_to_rgb(l: float, c: float, h_deg: float) -> tuple[float, float, float]:
    """Convert OKLCH to linear-ish sRGB in 0-1, clamped.

    Tailwind v4 and current shadcn emit oklch by default, so a token tree on
    the pinned stack is all oklch. Treating those as unparseable meant the
    contrast gate checked nothing and reported a pass.
    """
    h = math.radians(h_deg)
    a, b = c * math.cos(h), c * math.sin(h)

    l_ = l + 0.3963377774 * a + 0.2158037573 * b
    m_ = l - 0.1055613458 * a - 0.0638541728 * b
    s_ = l - 0.0894841775 * a - 1.2914855480 * b
    l3, m3, s3 = l_ ** 3, m_ ** 3, s_ ** 3

    lr = +4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
    lg = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
    lb = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3

    def to_srgb(v: float) -> float:
        v = max(0.0, min(1.0, v))
        return 1.055 * (v ** (1 / 2.4)) - 0.055 if v > 0.0031308 else 12.92 * v

    return (to_srgb(lr), to_srgb(lg), to_srgb(lb))


def _parse_color(value: str) -> tuple[float, float, float]:
    """Parse a token color value into normalized sRGB. Raises ColorParseError."""
    text = value.strip()
    if text.startswith("#"):
        return _hex_to_rgb(text)
    if (m := _OKLCH_RE.match(text)) is not None:
        raw_l = m.group(1)
        lightness = float(raw_l.rstrip("%")) / (100.0 if raw_l.endswith("%") else 1.0)
        return _oklch_to_rgb(lightness, float(m.group(2)), float(m.group(3)))
    if (m := _RGB_RE.match(text)) is not None:
        return tuple(min(255, int(m.group(i))) / 255.0 for i in (1, 2, 3))  # type: ignore[return-value]
    if _HEX_RE.match(text):
        return _hex_to_rgb(text)
    raise ColorParseError(f"unsupported color format: {value!r}")


def contrast_check(workspace: Optional[Path] = None) -> dict[str, Any]:
    """Check WCAG 2.1 contrast ratios for design token pairs.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.

    Returns
    -------
    dict with keys:
        pairs : list[dict] - per-pair results
        passed : bool - True if all pairs meet their required ratio
    """
    if workspace is None:
        workspace = resolve_workspace()

    tokens_path = workspace / "burooj.design" / "tokens.json"
    if not tokens_path.is_file():
        # No design system means nothing to conform to. That is a skip, not a
        # pass: reporting "PASS (0 pairs)" told the user the colors were
        # checked when no check happened.
        return {
            "pairs": [],
            "passed": True,
            "status": "skip",
            "reason": "no burooj.design/tokens.json in this workspace",
        }

    try:
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "pairs": [],
            "passed": False,
            "status": "error",
            "error": f"Cannot read tokens.json: {exc}",
        }

    results: list[PairResult] = []
    advisories: list[dict[str, Any]] = []
    unresolved: list[str] = []
    unparseable: list[str] = []
    all_passed = True

    def evaluate(
        fg_path: str, bg_path: str, required: float, description: str
    ) -> Optional[PairResult]:
        fg_value = _raw_token_value(tokens, fg_path)
        bg_value = _raw_token_value(tokens, bg_path)
        if fg_value is None or bg_value is None:
            missing = [p for p, v in ((fg_path, fg_value), (bg_path, bg_value)) if v is None]
            unresolved.extend(missing)
            return None
        try:
            ratio = _contrast_ratio(fg_value, bg_value)
        except ColorParseError as exc:
            unparseable.append(f"{fg_path}/{bg_path}: {exc}")
            return None
        return PairResult(
            foreground=fg_path,
            background=bg_path,
            fg_value=fg_value,
            bg_value=bg_value,
            ratio=ratio,
            required=required,
            passed=ratio >= required,
            description=description,
        )

    for fg_path, bg_path, required, description in _TOKEN_PAIRS:
        pair = evaluate(fg_path, bg_path, required, description)
        if pair is None:
            continue
        if not pair.passed:
            all_passed = False
        results.append(pair)

    for fg_path, bg_path, required, description in _ADVISORY_PAIRS:
        pair = evaluate(fg_path, bg_path, required, description)
        if pair is not None:
            entry = pair.to_dict()
            entry["advisory"] = True
            advisories.append(entry)

    # A token value we cannot read is a broken design system, not a pass.
    status = "pass" if all_passed else "fail"
    if unparseable:
        status = "error"
        all_passed = False

    result: dict[str, Any] = {
        "pairs": [r.to_dict() for r in results],
        "passed": all_passed,
        "status": status,
        "summary": f"{len(results)} pairs checked",
    }
    if advisories:
        result["advisory"] = advisories
        failing_advisory = [a for a in advisories if not a["passed"]]
        if failing_advisory:
            result["summary"] += f", {len(failing_advisory)} advisory below threshold"
    if unresolved:
        # Reported, not silent: a renamed token means the gate stopped
        # checking something it used to check.
        result["unresolved_tokens"] = sorted(set(unresolved))
        result["summary"] += f", {len(set(unresolved))} token(s) not found"
    if unparseable:
        result["error"] = "; ".join(unparseable)
    if not results and status != "error":
        result["status"] = "skip"
        result["reason"] = "tokens.json has none of the expected color pairs"

    # The desktop Design panel shows the last contrast run. Record it here so
    # a model-driven run inside a session is visible to the panel.
    from agent.burooj_status import record_design_check

    record_design_check(workspace, "contrast_check", result)

    return result


# ── Tool registration ───────────────────────────────────────────────────────

CONTRAST_CHECK_SCHEMA = {
    "name": "contrast_check",
    "description": (
        "Check WCAG 2.1 AA contrast ratios over burooj.design/tokens.json "
        "foreground/background pairs. Pure math, deterministic, no server "
        "needed. Rung 2 of the design gate. Handles hex, oklch, rgb and DTCG "
        "alias values."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def handle_contrast_check(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for contrast_check."""
    try:
        return json.dumps(contrast_check(), indent=2)
    except Exception as exc:
        logger.exception("contrast_check failed")
        return json.dumps(
            {"error": f"contrast_check crashed: {type(exc).__name__}: {exc}", "passed": False}
        )


registry.register(
    name="contrast_check",
    toolset="burooj_design",
    schema=CONTRAST_CHECK_SCHEMA,
    handler=handle_contrast_check,
    emoji="🌗",
)
