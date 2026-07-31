"""verify tool - runs the Build mode verification ladder.

Ladder order (cheap to expensive, fail-fast):
1. typecheck - zero errors
2. lint - clean
3. fix - tests that must flip from failing to passing
4. guard - tests that must stay passing
5. build - succeeds
6. render - dev server up, routes respond, zero console errors
7. design_gate - delegated to Design profile (B3, stub)

Tool schema:
    verify(rungs: list[str] | None = None) -> { results: list[Rung], passed: bool }

Each Rung: { name: str, status: "pass"|"fail"|"skip", output: str, duration_ms: int }
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import BuildManifest, ManifestError, load_manifest
from agent.build_runtime import get_runtime
from agent.build_workspace import resolve_workspace

logger = logging.getLogger("hermes.verify_tool")

# All ladder rungs in order.
LADDER_RUNGS = ("typecheck", "lint", "fix", "guard", "build", "render", "design_gate")

# Timeout for each command (seconds).
_RUNG_TIMEOUT = 120

# Timeout for the dev server to become ready during render rung.
_RENDER_SERVER_TIMEOUT = 20


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


def _run_rung_typecheck(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the typecheck rung."""
    start = time.monotonic()
    runtime = get_runtime()
    result = runtime.exec(manifest.typecheck, workspace, timeout=_RUNG_TIMEOUT)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if result.ok else "fail"
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr if output else result.stderr
    return RungResult(name="typecheck", status=status, output=output.strip(), duration_ms=duration)


def _run_rung_lint(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the lint rung."""
    start = time.monotonic()
    runtime = get_runtime()
    result = runtime.exec(manifest.lint, workspace, timeout=_RUNG_TIMEOUT)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if result.ok else "fail"
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr if output else result.stderr
    return RungResult(name="lint", status=status, output=output.strip(), duration_ms=duration)


def _run_rung_fix(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the fix tests (must pass to prove the change works)."""
    start = time.monotonic()
    fix_tests = manifest.test.fix
    if not fix_tests:
        return RungResult(name="fix", status="skip", output="No fix tests defined", duration_ms=0)

    # Run only the specified test files/patterns.
    test_args = " ".join(fix_tests)
    command = f"{manifest.test.command} -- {test_args}"
    runtime = get_runtime()
    result = runtime.exec(command, workspace, timeout=_RUNG_TIMEOUT)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if result.ok else "fail"
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr if output else result.stderr
    return RungResult(name="fix", status=status, output=output.strip(), duration_ms=duration)


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

    runtime = get_runtime()
    result = runtime.exec(command, workspace, timeout=_RUNG_TIMEOUT)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if result.ok else "fail"
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr if output else result.stderr
    return RungResult(name="guard", status=status, output=output.strip(), duration_ms=duration)


def _run_rung_build(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the build rung."""
    start = time.monotonic()
    runtime = get_runtime()
    result = runtime.exec(manifest.build, workspace, timeout=_RUNG_TIMEOUT)
    duration = int((time.monotonic() - start) * 1000)
    status = "pass" if result.ok else "fail"
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr if output else result.stderr
    return RungResult(name="build", status=status, output=output.strip(), duration_ms=duration)


def _run_rung_render(manifest: BuildManifest, workspace: Path) -> RungResult:
    """Run the render rung: boot dev server, check routes respond, check for errors.

    This is a lighter check than the full preview tool. It verifies:
    1. The dev server starts and becomes ready.
    2. Each declared route returns a non-error HTTP status.
    3. No server stderr output indicates a crash.

    The full Playwright screenshot capture is done by the separate preview tool.
    """
    import urllib.error
    import urllib.request

    start = time.monotonic()
    runtime = get_runtime()

    # Start the dev server.
    env = {"PORT": str(manifest.dev.port)}
    handle = runtime.start_process(
        command=manifest.dev.command,
        cwd=workspace,
        env=env,
    )

    try:
        # Wait for the server to be ready.
        ready = runtime.wait_for_port(
            port=manifest.dev.port,
            timeout=_RENDER_SERVER_TIMEOUT,
            path=manifest.dev.ready,
        )

        if not ready:
            stderr_lines = handle.get_stderr()
            output = f"Dev server did not become ready within {_RENDER_SERVER_TIMEOUT}s"
            if stderr_lines:
                output += "\nServer stderr:\n" + "\n".join(stderr_lines[-20:])
            duration = int((time.monotonic() - start) * 1000)
            return RungResult(name="render", status="fail", output=output, duration_ms=duration)

        # Check each route responds with a non-error status.
        base_url = f"http://localhost:{manifest.dev.port}"
        failures: list[str] = []

        for route in manifest.routes:
            url = f"{base_url}{route}"
            try:
                req = urllib.request.Request(url, method="GET")
                resp = urllib.request.urlopen(req, timeout=10)
                if resp.status >= 400:
                    failures.append(f"{route}: HTTP {resp.status}")
            except urllib.error.HTTPError as exc:
                failures.append(f"{route}: HTTP {exc.code}")
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                failures.append(f"{route}: {exc}")

        # Check server stderr for crash indicators.
        stderr_lines = handle.get_stderr()
        crash_indicators = [
            line for line in stderr_lines
            if any(word in line.lower() for word in ("error", "unhandled", "crash", "fatal", "eaddrinuse"))
            and "deprecation" not in line.lower()
            and "experimentalwarning" not in line.lower()
        ]

        duration = int((time.monotonic() - start) * 1000)

        if failures:
            output = "Route failures:\n" + "\n".join(failures)
            if crash_indicators:
                output += "\nServer errors:\n" + "\n".join(crash_indicators[-10:])
            return RungResult(name="render", status="fail", output=output, duration_ms=duration)

        if crash_indicators:
            output = "Server errors detected:\n" + "\n".join(crash_indicators[-10:])
            return RungResult(name="render", status="fail", output=output, duration_ms=duration)

        output = f"All {len(manifest.routes)} route(s) responded OK"
        if not handle.is_running:
            output += " (server exited after responding)"
        return RungResult(name="render", status="pass", output=output, duration_ms=duration)

    finally:
        runtime.stop_process(handle)


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
