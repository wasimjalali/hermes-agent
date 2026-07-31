"""Workspace root resolution and safety boundary for Build mode.

Resolution order (first hit wins):
1. Explicit ``cwd`` from the session (desktop passes project root)
2. Nearest ancestor with ``burooj.build.json``
3. Nearest git root
4. The session cwd

All file operations in Build mode are confined to the resolved workspace root.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from agent.build_manifest import MANIFEST_FILENAME

logger = logging.getLogger("hermes.build_workspace")

# Default workspace parent for projects Build creates from scratch.
WORKSPACES_DIR = Path.home() / "Burooj" / "workspaces"


def _nearest_manifest(start: Path, limit: int = 10) -> Optional[Path]:
    """Walk up from *start* looking for burooj.build.json.

    Returns the directory containing the manifest, or None.
    Stops after *limit* ancestors to avoid walking to /.
    """
    current = start.resolve()
    for _depth in range(limit):
        if (current / MANIFEST_FILENAME).is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _nearest_git_root(start: Path, limit: int = 10) -> Optional[Path]:
    """Walk up from *start* looking for a .git directory or file.

    Returns the git root, or None. Skips $HOME as a git root (dotfiles).
    """
    current = start.resolve()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for _depth in range(limit):
        if (current / ".git").exists():
            if home is not None and current == home:
                # Dotfiles repo at $HOME is not a workspace signal.
                pass
            else:
                return current
        parent = current.parent
        if parent == current:
            break
        current = parent
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
    if session_cwd is not None:
        cwd = Path(session_cwd).expanduser().resolve()
    else:
        cwd = Path(os.getcwd()).resolve()

    # 1. If the cwd itself has a manifest, it's the workspace.
    manifest_root = _nearest_manifest(cwd)
    if manifest_root is not None:
        logger.debug("Workspace from manifest: %s", manifest_root)
        return manifest_root

    # 2. Nearest git root.
    git_root = _nearest_git_root(cwd)
    if git_root is not None:
        logger.debug("Workspace from git root: %s", git_root)
        return git_root

    # 3. Fall through to the session cwd itself.
    logger.debug("Workspace from session cwd: %s", cwd)
    return cwd


def is_within_workspace(path: Path, workspace: Path) -> bool:
    """Check if *path* is contained within the *workspace* boundary.

    Used by file operation guards to prevent Build mode from writing
    outside the resolved workspace.
    """
    try:
        resolved = path.resolve()
        ws_resolved = workspace.resolve()
        resolved.relative_to(ws_resolved)
        return True
    except ValueError:
        return False


def ensure_workspaces_dir() -> Path:
    """Create and return the default workspaces parent directory."""
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    return WORKSPACES_DIR
