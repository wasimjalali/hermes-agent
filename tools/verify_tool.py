"""verify tool - runs the Build mode verification ladder.

Ladder order (cheap to expensive, fail-fast):
1. typecheck - zero errors
2. lint - clean
3. fix - tests that must flip from failing to passing
4. guard - tests that must stay passing
5. build - succeeds
6. render - dev server up, routes screenshot, zero console errors
7. design_gate - delegated to Design profile (B3, stub)

Tool schema:
    verify(rungs: list[str] | None = None) -> { results: list[Rung], passed: bool }

Each Rung: { name: str, status: "pass"|"fail"|"skip", output: str, duration_ms: int }
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import BuildManifest, ManifestError, load_manifest
from agent.build_workspace import resolve_workspace

logger = logging.getLogger("hermes.verify_tool")

# All ladder rungs in order.
LADDER_RUNGS = ("typecheck", "lint", "fix", "guard", "build", "render", "design_gate")

# Rungs that run without the manifest's test config.
_SIMPLE_RUNGS = {"typecheck", "lint", "build"}

# Timeout for each command (seconds).
_RUNG_TIMEOUT = 120


@dataclass
class RungResult:
    """Result of a single verification rung."""

    name: str
    status: str  # "pass", "fail", "skip"
    output: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "output": self.output,
            "duration_ms": self.duration_ms,
        }


def _run_command(
    command: str, cwd: Path, timeout: int = _RUNG_TIMEOUT
) -> tuple[int, str]:
    """Run a shell command and return (exit_code, combined output).

    Never raises on command failure. Returns the exit code and output.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr if output else result.stderr
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, f"Command timed out after {timeout}s: {command}"
    except OSError as exc:
        return 1, f"Failed to run command: {exc}"


def _run_rung_typecheck(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the typecheck rung."""
    start = time.monotonic()
    code, output = _run_command(manifest.typecheck, workspace)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if code == 0 else "fail"
    return RungResult(name="typecheck", status=status, output=output, duration_ms=duration)


def _run_rung_lint(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the lint rung."""
    start = time.monotonic()
    code, output = _run_command(manifest.lint, workspace)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if code == 0 else "fail"
    return RungResult(name="lint", status=status, output=output, duration_ms=duration)


def _run_rung_fix(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the fix tests (must pass to prove the change works)."""
    start = time.monotonic()
    fix_tests = manifest.test.fix
    if not fix_tests:
        return RungResult(name="fix", status="skip", output="No fix tests defined", duration_ms=0)

    # Run only the specified test files/patterns.
    test_args = " ".join(fix_tests)
    command = f"{manifest.test.command} -- {test_args}"
    code, output = _run_command(command, workspace)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if code == 0 else "fail"
    return RungResult(name="fix", status=status, output=output, duration_ms=duration)


def _run_rung_guard(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the guard tests (must stay passing to prove nothing broke)."""
    start = time.monotonic()
    guard_tests = manifest.test.guard
    if not guard_tests:
        # If no guard tests specified, run all tests.
        command = manifest.test.command
    else:
        test_args = " ".join(guard_tests)
        command = f"{manifest.test.command} -- {test_args}"

    code, output = _run_command(command, workspace)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if code == 0 else "fail"
    return RungResult(name="guard", status=status, output=output, duration_ms=duration)


def _run_rung_build(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the build rung."""
    start = time.monotonic()
    code, output = _run_command(manifest.build, workspace)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if code == 0 else "fail"
    return RungResult(name="build", status=status, output=output, duration_ms=duration)


def _run_rung_render(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the render rung (dev server + route screenshots).

    This is a placeholder that checks the dev server can start and serve
    the ready route. Full Playwright integration is in preview_tool.py.
    """
    start = time.monotonic()
    # For the verify ladder, render just checks the build output exists
    # or that a dev server can start. Full preview is a separate tool.
    # For now, if build passed, render passes (the preview tool does the
    # actual screenshot work).
    duration = int((time.monotonic() - start) * 1000)
    return RungResult(
        name="render",
        status="skip",
        output="Render check delegated to preview tool",
        duration_ms=duration,
    )


def _run_rung_design_gate(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the design gate rung (B3 stub)."""
    return RungResult(
        name="design_gate",
        status="skip",
        output="Design gate not yet implemented (B3)",
        duration_ms=0,
    )


# Map rung names to their runners.
_RUNG_RUNNERS = {
    "typecheck": _run_rung_typecheck,
    "lint": _run_rung_lint,
    "fix": _run_rung_fix,
    "guard": _run_rung_guard,
    "build": _run_rung_build,
    "render": _run_rung_render,
    "design_gate": _run_rung_design_gate,
}


def verify(
    workspace: Optional[Path] = None,
    rungs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run the verification ladder on the workspace.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    rungs : list[str], optional
        Specific rungs to run. When None, runs all in order, stopping at
        the first failure.

    Returns
    -------
    dict with keys:
        results : list[dict] - per-rung results
        passed : bool - True if all ran rungs passed or were skipped
    """
    if workspace is None:
        workspace = resolve_workspace()

    # Load the manifest.
    try:
        manifest = load_manifest(workspace)
    except ManifestError as exc:
        return {
            "results": [RungResult(name="manifest", status="fail", output=str(exc)).to_dict()],
            "passed": False,
        }

    # Determine which rungs to run.
    if rungs is not None:
        selected = [r for r in rungs if r in _RUNG_RUNNERS]
    else:
        selected = list(LADDER_RUNGS)

    results: list[dict[str, Any]] = []
    all_passed = True

    for rung_name in selected:
        runner = _RUNG_RUNNERS[rung_name]
        result = runner(manifest, workspace)
        results.append(result.to_dict())

        if result.status == "fail":
            all_passed = False
            # Fail-fast: stop at first failure when running the full ladder.
            if rungs is None:
                break

    return {"results": results, "passed": all_passed}
