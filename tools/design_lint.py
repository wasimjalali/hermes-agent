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

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Optional

from agent.build_workspace import resolve_workspace
from tools.registry import registry

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

# Regex: arbitrary Tailwind values like bg-[#ff0000], p-[13px], grid-rows-[20px].
# Longest alternatives first so `min-w` wins over `w` and `grid-cols` over `col`.
_ARBITRARY_TW_RE = re.compile(
    r"""(?:min-w|min-h|max-w|max-h|grid-cols|grid-rows|col-span|row-span|"""
    r"""leading|tracking|translate|rounded|shadow|border|inset|bottom|"""
    r"""aspect|basis|content|opacity|scale|order|space|right|font|ring|"""
    r"""left|text|fill|flex|gap|top|bg|px|py|pt|pb|pl|pr|mx|my|mt|mb|ml|mr|"""
    r"""p|m|w|h|z)"""
    r"""-\[([^\]]+)\]"""
)

# Regex: raw pixel values in style attributes or CSS properties.
# Matches things like: 12px, 100px (but not 0px which is fine).
_RAW_PX_RE = re.compile(r"""(?<!\$value["\s:])(?<![a-zA-Z-])([1-9]\d*px)\b""")

# Files/dirs that are part of the design system (never lint these).
_DESIGN_SYSTEM_PATHS = {"burooj.design", "tokens.json", "tokens.css", "tailwind.tokens.js"}

# Inline escape hatches. A gate with no way to say "this one is deliberate"
# gets switched off wholesale by the first person who hits a false positive,
# which is strictly worse than a gate with a documented exception.
_DISABLE_FILE_RE = re.compile(r"burooj-design-lint-disable-file")
_DISABLE_LINE_RE = re.compile(r"burooj-design-lint-disable-line")
_DISABLE_NEXT_RE = re.compile(r"burooj-design-lint-disable-next-line")

# Optional per-workspace allowlist, JSON at burooj.design/lint-ignore.json:
#   { "paths": ["src/components/ui/**"], "rules": { "raw-px": ["src/legacy/**"] } }
_IGNORE_FILENAME = "lint-ignore.json"


@dataclass(frozen=True)
class IgnoreConfig:
    """Parsed lint-ignore.json."""

    paths: tuple[str, ...] = ()
    rules: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def covers(self, rel_path: str, rule: str) -> bool:
        """True when *rel_path* is exempt, globally or for this *rule*."""
        posix = PurePath(rel_path).as_posix()
        if any(PurePath(posix).match(pattern) for pattern in self.paths):
            return True
        for rule_name, patterns in self.rules:
            if rule_name != rule:
                continue
            if any(PurePath(posix).match(pattern) for pattern in patterns):
                return True
        return False


def _load_ignore_config(workspace: Path) -> IgnoreConfig:
    """Read burooj.design/lint-ignore.json. Absent or malformed means no exemptions."""
    path = workspace / "burooj.design" / _IGNORE_FILENAME
    if not path.is_file():
        return IgnoreConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring malformed %s: %s", path, exc)
        return IgnoreConfig()
    if not isinstance(data, dict):
        return IgnoreConfig()

    raw_paths = data.get("paths", [])
    paths = tuple(p for p in raw_paths if isinstance(p, str)) if isinstance(raw_paths, list) else ()

    raw_rules = data.get("rules", {})
    rules: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(raw_rules, dict):
        for rule_name, patterns in raw_rules.items():
            if isinstance(rule_name, str) and isinstance(patterns, list):
                rules.append(
                    (rule_name, tuple(p for p in patterns if isinstance(p, str)))
                )
    return IgnoreConfig(paths=paths, rules=tuple(rules))


def _disabled_lines(content: str) -> tuple[bool, set[int]]:
    """Return (whole file disabled, set of 1-indexed disabled line numbers)."""
    lines = content.splitlines()
    if any(_DISABLE_FILE_RE.search(line) for line in lines[:20]):
        return True, set()
    disabled: set[int] = set()
    for idx, line in enumerate(lines, start=1):
        if _DISABLE_NEXT_RE.search(line):
            disabled.add(idx + 1)
        elif _DISABLE_LINE_RE.search(line):
            disabled.add(idx)
    return False, disabled


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
        tw_spans = _tailwind_spans(line)
        for match in _RAW_HEX_RE.finditer(line):
            # Skip if it's inside a CSS variable reference.
            before = line[:match.start()]
            if "var(--" in before and ")" not in before[before.rfind("var(--"):]:
                continue
            # Skip if it's in a CSS custom property definition (token output).
            if re.match(r"\s*--", line):
                continue
            # `bg-[#ff0000]` is one mistake. Reporting it as both raw-hex and
            # arbitrary-tailwind doubled the violation count and buried the
            # signal under its own duplicates.
            if any(start <= match.start() < end for start, end in tw_spans):
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


def _tailwind_spans(line: str) -> list[tuple[int, int]]:
    """Character spans already reported as arbitrary Tailwind values.

    A `p-[13px]` is one mistake, not two. Without this the same span was
    reported once as `arbitrary-tailwind` and again as `raw-px`, which made
    every violation count roughly double and buried the real signal.
    """
    return [(m.start(), m.end()) for m in _ARBITRARY_TW_RE.finditer(line)]


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
        tw_spans = _tailwind_spans(line)
        for match in _RAW_PX_RE.finditer(line):
            # Already counted as an arbitrary Tailwind value.
            if any(start <= match.start() < end for start, end in tw_spans):
                continue
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
    ignore = _load_ignore_config(workspace)
    all_violations: list[Violation] = []
    suppressed = 0

    for file_path, rel_path in source_files:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_disabled, disabled_lines = _disabled_lines(content)
        if file_disabled:
            continue

        found = (
            _check_raw_hex(content, rel_path)
            + _check_arbitrary_tailwind(content, rel_path)
            + _check_raw_px(content, rel_path)
        )
        for violation in found:
            if violation.line in disabled_lines or ignore.covers(rel_path, violation.rule):
                suppressed += 1
                continue
            all_violations.append(violation)

    passed = not all_violations
    result: dict[str, Any] = {
        "violations": [v.to_dict() for v in all_violations],
        "passed": passed,
        "status": "pass" if passed else "fail",
        "files_scanned": len(source_files),
        "summary": f"{len(source_files)} files scanned",
    }
    if suppressed:
        result["suppressed"] = suppressed
        result["summary"] += f", {suppressed} suppressed"
    if not source_files:
        result["status"] = "skip"
        result["reason"] = "no lintable source files in the workspace"

    # The desktop Design panel shows the last lint run. Record it here so a
    # model-driven run inside a session is visible to the panel.
    from agent.burooj_status import record_design_check

    record_design_check(workspace, "design_lint", result)

    return result


# ── Tool registration ───────────────────────────────────────────────────────

DESIGN_LINT_SCHEMA = {
    "name": "design_lint",
    "description": (
        "Scan the workspace for code that bypasses the design system: raw hex "
        "colors, raw pixel values and arbitrary Tailwind values. Deterministic, "
        "no server needed. Rung 1 of the design gate. Suppress a deliberate "
        "exception with a 'burooj-design-lint-disable-next-line' comment or "
        "burooj.design/lint-ignore.json."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def handle_design_lint(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for design_lint."""
    try:
        return json.dumps(design_lint(), indent=2)
    except Exception as exc:
        logger.exception("design_lint failed")
        return json.dumps(
            {"error": f"design_lint crashed: {type(exc).__name__}: {exc}", "passed": False}
        )


registry.register(
    name="design_lint",
    toolset="burooj_design",
    schema=DESIGN_LINT_SCHEMA,
    handler=handle_design_lint,
    emoji="📐",
)
