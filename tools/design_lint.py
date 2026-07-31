"""design_lint tool - catches design system bypasses in source files.

Scans workspace source files for:
- Raw hex colors in TSX/CSS (not inside burooj.design/)
- Raw pixel values in inline styles or className
- Arbitrary Tailwind values that bypass the configured token system

Tool schema:
    design_lint(workspace: Path) -> { violations: list[Violation], passed: bool }

Each Violation: { file: str, line: int, column: int, rule: str, value: str, message: str }
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.build_workspace import resolve_workspace

logger = logging.getLogger("hermes.design_lint")

# Extensions to scan.
_SCAN_EXTENSIONS = frozenset({".tsx", ".jsx", ".ts", ".js", ".css", ".scss"})

# Directories to skip.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", ".next", ".turbo", ".burooj", "coverage", "burooj.design",
})

# Regex: raw hex color (3, 4, 6, or 8 hex digits after #).
# Avoids matching inside comments or strings that are clearly token refs.
_RAW_HEX_RE = re.compile(
    r"""(?<![&$])\#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b"""
)

# Regex: arbitrary Tailwind values like bg-[#ff0000], p-[13px], text-[17px].
_ARBITRARY_TW_RE = re.compile(
    r"""(?:bg|text|border|ring|shadow|p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|"""
    r"""gap|space|w|h|min-w|min-h|max-w|max-h|rounded|inset|top|left|right|bottom)"""
    r"""-\[([^\]]+)\]"""
)

# Regex: raw pixel values in style attributes or CSS properties.
# Matches things like: 12px, 100px (but not 0px which is fine).
_RAW_PX_RE = re.compile(r"""(?<!\$value["\s:])(?<![a-zA-Z-])([1-9]\d*px)\b""")

# Files/dirs that are part of the design system (never lint these).
_DESIGN_SYSTEM_PATHS = {"burooj.design", "tokens.json", "tokens.css", "tailwind.tokens.js"}


@dataclass
class Violation:
    """A single design lint violation."""

    file: str
    line: int
    column: int
    rule: str
    value: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "rule": self.rule,
            "value": self.value,
            "message": self.message,
        }


def _is_design_system_path(rel_path: str) -> bool:
    """Check if a file path is part of the design system itself."""
    parts = rel_path.split(os.sep)
    return any(part in _DESIGN_SYSTEM_PATHS for part in parts)


def _is_in_comment_or_config(line: str) -> bool:
    """Rough check: is this line a comment or config line we should skip?"""
    stripped = line.lstrip()
    # Skip comment lines.
    if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
        return True
    # Skip import lines (hex in import paths is fine).
    if stripped.startswith("import "):
        return True
    return False


def _check_raw_hex(content: str, rel_path: str) -> list[Violation]:
    """Check for raw hex colors."""
    violations: list[Violation] = []
    for line_num, line in enumerate(content.splitlines(), start=1):
        if _is_in_comment_or_config(line):
            continue
        for match in _RAW_HEX_RE.finditer(line):
            # Skip if it's inside a CSS variable reference.
            before = line[:match.start()]
            if "var(--" in before and ")" not in before[before.rfind("var(--"):]:
                continue
            # Skip if it's in a CSS custom property definition (token output).
            if re.match(r"\s*--", line):
                continue
            violations.append(Violation(
                file=rel_path,
                line=line_num,
                column=match.start() + 1,
                rule="raw-hex",
                value=match.group(0),
                message=f"Raw hex color '{match.group(0)}'. Use a design token instead.",
            ))
    return violations


def _check_arbitrary_tailwind(content: str, rel_path: str) -> list[Violation]:
    """Check for arbitrary Tailwind values."""
    violations: list[Violation] = []
    for line_num, line in enumerate(content.splitlines(), start=1):
        if _is_in_comment_or_config(line):
            continue
        for match in _ARBITRARY_TW_RE.finditer(line):
            violations.append(Violation(
                file=rel_path,
                line=line_num,
                column=match.start() + 1,
                rule="arbitrary-tailwind",
                value=match.group(0),
                message=f"Arbitrary Tailwind value '{match.group(0)}'. Use token-based classes.",
            ))
    return violations


def _check_raw_px(content: str, rel_path: str) -> list[Violation]:
    """Check for raw pixel values in style attributes."""
    violations: list[Violation] = []
    # Only check lines that look like inline styles or CSS properties.
    for line_num, line in enumerate(content.splitlines(), start=1):
        if _is_in_comment_or_config(line):
            continue
        # Look for style= or CSS property patterns.
        has_style_context = (
            "style=" in line
            or "style:" in line
            or re.search(r":\s*['\"]?\d+px", line)
        )
        if not has_style_context:
            continue
        # Skip CSS custom property definitions.
        if re.match(r"\s*--", line):
            continue
        for match in _RAW_PX_RE.finditer(line):
            violations.append(Violation(
                file=rel_path,
                line=line_num,
                column=match.start() + 1,
                rule="raw-px",
                value=match.group(1),
                message=f"Raw pixel value '{match.group(1)}'. Use a spacing/sizing token.",
            ))
    return violations


def _walk_source_files(workspace: Path) -> list[tuple[Path, str]]:
    """Walk workspace and yield (path, rel_path) for lintable source files."""
    results: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SCAN_EXTENSIONS:
                full_path = Path(dirpath) / fname
                rel_path = str(full_path.relative_to(workspace))
                if not _is_design_system_path(rel_path):
                    results.append((full_path, rel_path))
    return results


def design_lint(workspace: Optional[Path] = None) -> dict[str, Any]:
    """Lint workspace source files for design system bypasses.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.

    Returns
    -------
    dict with keys:
        violations : list[dict] - each violation with file, line, rule, value, message
        passed : bool - True if zero violations
        files_scanned : int
    """
    if workspace is None:
        workspace = resolve_workspace()

    source_files = _walk_source_files(workspace)
    all_violations: list[Violation] = []

    for file_path, rel_path in source_files:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        all_violations.extend(_check_raw_hex(content, rel_path))
        all_violations.extend(_check_arbitrary_tailwind(content, rel_path))
        all_violations.extend(_check_raw_px(content, rel_path))

    return {
        "violations": [v.to_dict() for v in all_violations],
        "passed": len(all_violations) == 0,
        "files_scanned": len(source_files),
    }
