"""burooj.build.json manifest loader and schema validation.

Loads and validates the per-workspace Build manifest that tells the verify
ladder which commands to run. The manifest lives at ``<workspace>/burooj.build.json``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.build_manifest")

MANIFEST_FILENAME = "burooj.build.json"

# Command keys. All optional: a project with no lint step or no tests must
# still be able to produce a valid manifest. A missing or empty command makes
# its ladder rung report "skip" rather than failing the whole verification.
_COMMAND_KEYS = ("install", "typecheck", "lint", "build")


@dataclass(frozen=True)
class TestConfig:
    """Test configuration from the manifest."""

    command: str
    fix: list[str] = field(default_factory=list)
    guard: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DevConfig:
    """Dev server configuration from the manifest."""

    command: str
    port: int = 3000
    ready: str = "/"


@dataclass(frozen=True)
class BuildManifest:
    """Parsed and validated burooj.build.json.

    Every command is optional. An empty string means "this project has no such
    step", and the corresponding ladder rung reports ``skip``. ``dev`` is None
    when the project declares no dev server, which makes the render, preview,
    a11y and visual-diff paths skip rather than fail.
    """

    install: str = ""
    typecheck: str = ""
    lint: str = ""
    test: Optional[TestConfig] = None
    build: str = ""
    dev: Optional[DevConfig] = None
    routes: list[str] = field(default_factory=lambda: ["/"])


class ManifestError(Exception):
    """Raised when a manifest is missing, malformed, or invalid."""


def _validate_optional_string(data: dict[str, Any], key: str) -> str:
    """Return a trimmed command string, or "" when the key is absent.

    A key present but not a string is still an error: that is a typo, not an
    intentional omission, and silently treating it as "no such step" would let
    a broken manifest report a green ladder.
    """
    if key not in data or data[key] is None:
        return ""
    value = data[key]
    if not isinstance(value, str):
        raise ManifestError(f"'{key}' must be a string when present")
    return value.strip()


def _validate_test(data: dict[str, Any]) -> Optional[TestConfig]:
    """Validate and parse the 'test' section. None when absent."""
    raw = data.get("test")
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        return TestConfig(command=raw)
    if not isinstance(raw, dict):
        raise ManifestError("'test' must be a string or object")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ManifestError("'test.command' must be a non-empty string")
    fix = raw.get("fix", [])
    if not isinstance(fix, list) or not all(isinstance(t, str) for t in fix):
        raise ManifestError("'test.fix' must be a list of strings")
    guard = raw.get("guard", [])
    if not isinstance(guard, list) or not all(isinstance(t, str) for t in guard):
        raise ManifestError("'test.guard' must be a list of strings")
    return TestConfig(command=command.strip(), fix=fix, guard=guard)


def _validate_dev(data: dict[str, Any]) -> Optional[DevConfig]:
    """Validate and parse the 'dev' section. None when absent."""
    raw = data.get("dev")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("'dev' must be an object")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ManifestError("'dev.command' must be a non-empty string")
    port = raw.get("port", 3000)
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ManifestError("'dev.port' must be an integer 1-65535")
    ready = raw.get("ready", "/")
    if not isinstance(ready, str):
        raise ManifestError("'dev.ready' must be a string")
    return DevConfig(command=command.strip(), port=port, ready=ready)


def _validate_routes(data: dict[str, Any]) -> list[str]:
    """Validate and parse the 'routes' list."""
    raw = data.get("routes", ["/"])
    if not isinstance(raw, list):
        raise ManifestError("'routes' must be a list of strings")
    if not all(isinstance(r, str) for r in raw):
        raise ManifestError("'routes' entries must be strings")
    if not raw:
        return ["/"]
    return raw


def load_manifest(workspace: Path) -> BuildManifest:
    """Load and validate burooj.build.json from the workspace root.

    Parameters
    ----------
    workspace : Path
        The workspace root directory.

    Returns
    -------
    BuildManifest
        The validated manifest.

    Raises
    ------
    ManifestError
        If the manifest is missing, unreadable, or invalid.
    """
    manifest_path = workspace / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ManifestError(
            f"No {MANIFEST_FILENAME} found at {workspace}"
        )

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Cannot read {manifest_path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Invalid JSON in {manifest_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"{MANIFEST_FILENAME} must be a JSON object")

    # Command fields. All optional; absent means the rung skips.
    commands = {key: _validate_optional_string(data, key) for key in _COMMAND_KEYS}

    # Structured sections, also optional.
    test = _validate_test(data)
    dev = _validate_dev(data)
    routes = _validate_routes(data)

    manifest = BuildManifest(
        install=commands["install"],
        typecheck=commands["typecheck"],
        lint=commands["lint"],
        test=test,
        build=commands["build"],
        dev=dev,
        routes=routes,
    )

    if not any((manifest.install, manifest.typecheck, manifest.lint,
                manifest.build, manifest.test, manifest.dev)):
        raise ManifestError(
            f"{MANIFEST_FILENAME} declares no commands at all. A manifest that "
            f"verifies nothing would report a green ladder for any change."
        )

    return manifest
