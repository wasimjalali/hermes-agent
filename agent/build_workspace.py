"""Workspace root resolution and safety boundary for Build mode.

Resolution order (first hit wins):
1. Explicit ``cwd`` from the session, when it carries its own manifest
2. Nearest ancestor with ``burooj.build.json`` (bounded walk, see below)
3. Nearest git root
4. The session cwd

All file operations in Build mode are confined to the resolved workspace root.

**Why the ancestor walk is short.** The manifest supplies shell commands that
``verify`` executes. A ten-level walk meant a ``burooj.build.json`` sitting in
any shared parent (a checkouts directory, an unpacked archive, ``~/Downloads``)
could capture a session the user believed was scoped to their project, and
supply the commands for it. An explicit session cwd that has its own manifest
now wins outright, and the walk is capped at ``_ANCESTOR_LIMIT`` so a manifest
must be close enough to plausibly belong to the project.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import MANIFEST_FILENAME

logger = logging.getLogger("hermes.build_workspace")

# Default workspace parent for projects Build creates from scratch.
WORKSPACES_DIR = Path.home() / "Burooj" / "workspaces"

# How far up to look for a manifest or git root. Deliberately short: see the
# module docstring on why a long walk is a command-execution hazard.
_ANCESTOR_LIMIT = 2


def _ancestors(start: Path, limit: int) -> list[Path]:
    """Return *start* and up to *limit* ancestors, stopping at / and $HOME.

    ``$HOME`` itself is included as a candidate but nothing above it is: a
    workspace is never the home directory's parent, and walking past $HOME is
    how a stray file in a shared location captures an unrelated session.
    """
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None

    out: list[Path] = []
    current = start
    for _depth in range(limit + 1):
        out.append(current)
        if home is not None and current == home:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return out


def _nearest_manifest(start: Path, limit: int = _ANCESTOR_LIMIT) -> Optional[Path]:
    """Walk up from *start* looking for burooj.build.json.

    Returns the directory containing the manifest, or None.
    """
    for candidate in _ancestors(start.resolve(), limit):
        if (candidate / MANIFEST_FILENAME).is_file():
            return candidate
    return None


def _nearest_git_root(start: Path, limit: int = _ANCESTOR_LIMIT) -> Optional[Path]:
    """Walk up from *start* looking for a .git directory or file.

    Returns the git root, or None. ``$HOME`` is never a git root for this
    purpose: a dotfiles repo there is not a workspace signal.
    """
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for candidate in _ancestors(start.resolve(), limit):
        if home is not None and candidate == home:
            continue
        if (candidate / ".git").exists():
            return candidate
    return None


def resolve_workspace(session_cwd: Optional[str | Path] = None) -> Path:
    """Resolve the Build mode workspace root.

    Parameters
    ----------
    session_cwd : str or Path, optional
        The session's working directory (explicit project root from the desktop
        or CLI). When None, falls back to os.getcwd().

    Returns
    -------
    Path
        The resolved workspace root directory.
    """
    explicit = session_cwd is not None
    if explicit:
        cwd = Path(session_cwd).expanduser().resolve()
    else:
        cwd = Path(os.getcwd()).resolve()

    # 1. An explicit session cwd carrying its own manifest wins outright. An
    #    ancestor manifest must never override the directory the user pinned.
    if explicit and (cwd / MANIFEST_FILENAME).is_file():
        logger.debug("Workspace from explicit session cwd manifest: %s", cwd)
        return cwd

    # 2. Nearest ancestor with a manifest, within the bounded walk.
    manifest_root = _nearest_manifest(cwd)
    if manifest_root is not None:
        if manifest_root != cwd:
            logger.info(
                "Build workspace resolved to ancestor %s via %s (session cwd was %s)",
                manifest_root, MANIFEST_FILENAME, cwd,
            )
        return manifest_root

    # 3. Nearest git root.
    git_root = _nearest_git_root(cwd)
    if git_root is not None:
        logger.debug("Workspace from git root: %s", git_root)
        return git_root

    # 3. Fall through to the session cwd itself.
    logger.debug("Workspace from session cwd: %s", cwd)
    return cwd


def is_within_workspace(path: Path, workspace: Path) -> bool:
    """Check if *path* is contained within the *workspace* boundary.

    Used by file operation guards to prevent Build mode from writing outside
    the resolved workspace. ``resolve()`` follows symlinks, so a link pointing
    out of the workspace is correctly rejected rather than accepted on its
    literal path. The workspace root itself counts as inside.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        ws_resolved = Path(workspace).expanduser().resolve()
    except (OSError, RuntimeError):
        # Unresolvable path (broken symlink loop, permission denied on a
        # parent). Fail closed: an unverifiable path is not inside.
        return False
    try:
        resolved.relative_to(ws_resolved)
        return True
    except ValueError:
        return False


def assert_within_workspace(path: Path, workspace: Path, *, operation: str = "access") -> Path:
    """Return the resolved *path*, or raise :class:`WorkspaceBoundaryError`.

    The loud counterpart to :func:`is_within_workspace`, for call sites that
    should abort rather than branch.
    """
    if not is_within_workspace(path, workspace):
        raise WorkspaceBoundaryError(
            f"Build mode refused to {operation} {path}: outside the workspace "
            f"root {workspace}."
        )
    return Path(path).expanduser().resolve()


class WorkspaceBoundaryError(PermissionError):
    """Raised when a Build mode operation targets a path outside the workspace."""


# ── Active workspace pin ────────────────────────────────────────────────────
#
# Build mode confines writes to its workspace. File tools are called far away
# from the session object, so the pin lives in a ContextVar: the gateway sets it
# when it builds a Build-profile agent, and ``agent/file_safety.py`` consults it
# from inside ``get_write_denied_error``. Unset (the default, and every non-Build
# mode) means no extra confinement, so nothing changes for base Hermes.

_active_workspace: ContextVar[Optional[str]] = ContextVar(
    "burooj_active_build_workspace", default=None
)


def set_active_workspace(workspace: Optional[str | Path]) -> Any:
    """Pin the Build workspace for the current context. Returns a reset token."""
    value = str(Path(workspace).expanduser().resolve()) if workspace else None
    return _active_workspace.set(value)


def get_active_workspace() -> Optional[Path]:
    """Return the pinned Build workspace, or None when not in Build mode."""
    raw = _active_workspace.get()
    return Path(raw) if raw else None


def reset_active_workspace(token: Any) -> None:
    """Undo a :func:`set_active_workspace` call."""
    try:
        _active_workspace.reset(token)
    except (ValueError, LookupError):
        _active_workspace.set(None)


def ensure_workspaces_dir() -> Path:
    """Create and return the default workspaces parent directory."""
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    return WORKSPACES_DIR
