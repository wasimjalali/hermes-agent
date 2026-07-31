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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.build_workspace import resolve_workspace

logger = logging.getLogger("hermes.contrast_check")

# WCAG 2.1 AA thresholds.
_NORMAL_TEXT_RATIO = 4.5
_LARGE_TEXT_RATIO = 3.0
_UI_COMPONENT_RATIO = 3.0

# Known foreground/background pairs to check.
# Each entry: (foreground_path, background_path, required_ratio, description)
_TOKEN_PAIRS: list[tuple[str, str, float, str]] = [
    ("color.primary-foreground", "color.primary", _NORMAL_TEXT_RATIO, "primary text on primary bg"),
    ("color.secondary-foreground", "color.secondary", _NORMAL_TEXT_RATIO, "secondary text on secondary bg"),
    ("color.destructive-foreground", "color.destructive", _NORMAL_TEXT_RATIO, "destructive text on destructive bg"),
    ("color.accent-foreground", "color.accent", _NORMAL_TEXT_RATIO, "accent text on accent bg"),
    ("color.text.primary", "color.background.default", _NORMAL_TEXT_RATIO, "primary text on default bg"),
    ("color.text.secondary", "color.background.default", _NORMAL_TEXT_RATIO, "secondary text on default bg"),
    ("color.text.muted", "color.background.default", _NORMAL_TEXT_RATIO, "muted text on default bg"),
    ("color.text.primary", "color.background.muted", _NORMAL_TEXT_RATIO, "primary text on muted bg"),
    ("color.text.secondary", "color.background.muted", _NORMAL_TEXT_RATIO, "secondary text on muted bg"),
    ("color.text.primary", "color.background.subtle", _NORMAL_TEXT_RATIO, "primary text on subtle bg"),
    ("color.border.default", "color.background.default", _UI_COMPONENT_RATIO, "border on default bg"),
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


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    """Convert a hex color string to normalized RGB (0-1 range)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    elif len(hex_color) == 4:
        hex_color = "".join(c * 2 for c in hex_color[:3])
    elif len(hex_color) == 8:
        hex_color = hex_color[:6]  # Strip alpha.

    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


def _linearize(channel: float) -> float:
    """Linearize an sRGB channel value."""
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(r: float, g: float, b: float) -> float:
    """Compute WCAG relative luminance."""
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """Compute WCAG 2.1 contrast ratio between two hex colors."""
    fg_rgb = _hex_to_rgb(fg_hex)
    bg_rgb = _hex_to_rgb(bg_hex)

    l1 = _relative_luminance(*fg_rgb)
    l2 = _relative_luminance(*bg_rgb)

    # Lighter luminance goes on top.
    lighter = max(l1, l2)
    darker = min(l1, l2)

    return (lighter + 0.05) / (darker + 0.05)


def _resolve_token_path(tokens: dict[str, Any], path: str) -> Optional[str]:
    """Resolve a dot-separated token path to its $value.

    Returns the hex color string or None if not found.
    """
    parts = path.split(".")
    current: Any = tokens
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None

    # If we landed on a token object with $value.
    if isinstance(current, dict) and "$value" in current:
        value = current["$value"]
        if isinstance(value, str) and value.startswith("#"):
            return value
    # If we landed directly on a string value.
    if isinstance(current, str) and current.startswith("#"):
        return current

    return None


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
        return {
            "pairs": [],
            "passed": True,
            "error": "No burooj.design/tokens.json found. Skipping contrast check.",
        }

    try:
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "pairs": [],
            "passed": False,
            "error": f"Cannot read tokens.json: {exc}",
        }

    results: list[PairResult] = []
    all_passed = True

    for fg_path, bg_path, required, description in _TOKEN_PAIRS:
        fg_value = _resolve_token_path(tokens, fg_path)
        bg_value = _resolve_token_path(tokens, bg_path)

        if fg_value is None or bg_value is None:
            # Skip pairs where tokens don't exist.
            continue

        ratio = _contrast_ratio(fg_value, bg_value)
        passed = ratio >= required

        if not passed:
            all_passed = False

        results.append(PairResult(
            foreground=fg_path,
            background=bg_path,
            fg_value=fg_value,
            bg_value=bg_value,
            ratio=ratio,
            required=required,
            passed=passed,
            description=description,
        ))

    return {
        "pairs": [r.to_dict() for r in results],
        "passed": all_passed,
    }
