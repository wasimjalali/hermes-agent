"""Per-workspace status store for the Burooj mode panels.

The Build and Design desktop panels show what the last tool run produced.
The tools record their results here as a side effect of a normal call, so
a model-driven ``verify()`` or ``preview()`` inside a session is visible to
the panel without the panel re-running anything.

Nothing here executes checks. It is a cache, and the panels treat it as
one: a missing entry means "not run yet in this process", never "pass".
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
_verify: dict[str, dict[str, Any]] = {}
_preview: dict[str, dict[str, Any]] = {}
_design: dict[str, dict[str, Any]] = {}


def _key(workspace: Path | str) -> str:
    return str(Path(workspace).expanduser().resolve())


def record_verify(workspace: Path | str, result: dict[str, Any]) -> None:
    """Record the most recent ``verify`` ladder result for *workspace*."""
    with _lock:
        _verify[_key(workspace)] = result


def get_verify(workspace: Path | str) -> Optional[dict[str, Any]]:
    with _lock:
        return _verify.get(_key(workspace))


def record_preview(workspace: Path | str, result: dict[str, Any]) -> None:
    """Record the most recent ``preview`` result for *workspace*."""
    with _lock:
        _preview[_key(workspace)] = result


def get_preview(workspace: Path | str) -> Optional[dict[str, Any]]:
    with _lock:
        return _preview.get(_key(workspace))


def record_design_check(workspace: Path | str, check: str, result: dict[str, Any]) -> None:
    """Record one design-gate check result (visual_diff, a11y_check, ...)."""
    with _lock:
        key = _key(workspace)
        bucket = _design.setdefault(key, {})
        bucket[check] = result


def get_design_checks(workspace: Path | str) -> dict[str, dict[str, Any]]:
    with _lock:
        return dict(_design.get(_key(workspace), {}))
