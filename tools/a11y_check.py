"""a11y_check tool - axe-core accessibility check via Playwright.

Boots the dev server, injects axe-core into each declared route via
Playwright, and returns structured accessibility violations.

Only critical and serious violations cause a hard fail. Moderate and minor
are reported but advisory.

Tool schema:
    a11y_check(workspace: Path, routes: list[str] | None = None) -> { routes: list[RouteA11y], passed: bool }

Each RouteA11y: { path: str, violations: list[A11yViolation], error_count: int }
Each A11yViolation: { id: str, impact: str, description: str, nodes: int, help_url: str }
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import BuildManifest, ManifestError, load_manifest
from agent.build_runtime import get_runtime
from agent.build_workspace import resolve_workspace

logger = logging.getLogger("hermes.a11y_check")

# axe-core CDN URL (minified, stable version).
_AXE_CORE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"

# Impacts that cause a hard fail.
_FAIL_IMPACTS = frozenset({"critical", "serious"})

# Server ready timeout.
_SERVER_TIMEOUT = 20


@dataclass
class A11yViolation:
    """A single axe-core violation."""

    id: str
    impact: str
    description: str
    nodes: int
    help_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "impact": self.impact,
            "description": self.description,
            "nodes": self.nodes,
            "help_url": self.help_url,
        }


@dataclass
class RouteA11y:
    """Accessibility results for a single route."""

    path: str
    violations: list[A11yViolation] = field(default_factory=list)
    error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "violations": [v.to_dict() for v in self.violations],
            "error_count": self.error_count,
        }


async def _check_route(page: Any, base_url: str, route_path: str) -> RouteA11y:
    """Navigate to a route, inject axe-core, and run the check."""
    result = RouteA11y(path=route_path)

    url = f"{base_url}{route_path}"
    try:
        await page.goto(url, wait_until="networkidle", timeout=15000)
    except Exception as exc:
        result.violations.append(A11yViolation(
            id="navigation-error",
            impact="critical",
            description=f"Could not navigate to {route_path}: {exc}",
            nodes=0,
            help_url="",
        ))
        result.error_count = 1
        return result

    # Inject axe-core.
    try:
        await page.add_script_tag(url=_AXE_CORE_CDN)
        # Wait for axe to be available.
        await page.wait_for_function("typeof window.axe !== 'undefined'", timeout=10000)
    except Exception as exc:
        # If CDN fails, try injecting axe-core inline (fallback).
        logger.warning("axe-core CDN injection failed: %s. Trying inline.", exc)
        try:
            # Fetch axe-core script content.
            import urllib.request
            resp = urllib.request.urlopen(_AXE_CORE_CDN, timeout=10)
            axe_script = resp.read().decode("utf-8")
            await page.evaluate(axe_script)
        except Exception as fallback_exc:
            result.violations.append(A11yViolation(
                id="axe-injection-error",
                impact="serious",
                description=f"Could not inject axe-core: {fallback_exc}",
                nodes=0,
                help_url="https://github.com/dequelabs/axe-core",
            ))
            result.error_count = 1
            return result

    # Run axe-core.
    try:
        axe_results = await page.evaluate("""
            () => axe.run(document, {
                runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] }
            }).then(r => r.violations.map(v => ({
                id: v.id,
                impact: v.impact,
                description: v.description,
                nodes: v.nodes.length,
                helpUrl: v.helpUrl
            })))
        """)
    except Exception as exc:
        result.violations.append(A11yViolation(
            id="axe-run-error",
            impact="serious",
            description=f"axe.run() failed: {exc}",
            nodes=0,
            help_url="",
        ))
        result.error_count = 1
        return result

    # Process results.
    for v in axe_results:
        violation = A11yViolation(
            id=v.get("id", "unknown"),
            impact=v.get("impact", "minor"),
            description=v.get("description", ""),
            nodes=v.get("nodes", 0),
            help_url=v.get("helpUrl", ""),
        )
        result.violations.append(violation)
        if violation.impact in _FAIL_IMPACTS:
            result.error_count += 1

    return result


async def _run_a11y_check(
    manifest: BuildManifest,
    workspace: Path,
    routes: list[str],
) -> dict[str, Any]:
    """Async implementation of a11y_check."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "routes": [],
            "passed": False,
            "error": "playwright not installed. Run: pip install playwright && python -m playwright install chromium",
        }

    runtime = get_runtime()

    # Start the dev server.
    env = {"PORT": str(manifest.dev.port)}
    handle = runtime.start_process(
        command=manifest.dev.command,
        cwd=workspace,
        env=env,
    )

    try:
        ready = runtime.wait_for_port(
            port=manifest.dev.port,
            timeout=_SERVER_TIMEOUT,
            path=manifest.dev.ready,
        )
        if not ready:
            stderr_lines = handle.get_stderr()
            return {
                "routes": [],
                "passed": False,
                "error": f"Dev server did not become ready within {_SERVER_TIMEOUT}s",
                "server_stderr": stderr_lines[-20:] if stderr_lines else [],
            }

        base_url = f"http://localhost:{manifest.dev.port}"

        # Drive Playwright through each route.
        route_results: list[dict[str, Any]] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            for route_path in routes:
                result = await _check_route(page, base_url, route_path)
                route_results.append(result.to_dict())

            await browser.close()

        # Determine pass/fail (only critical/serious fail).
        all_passed = all(r["error_count"] == 0 for r in route_results)

        return {
            "routes": route_results,
            "passed": all_passed,
        }
    finally:
        runtime.stop_process(handle)


def a11y_check(
    workspace: Optional[Path] = None,
    routes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run axe-core accessibility checks on workspace routes.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    routes : list[str], optional
        Routes to check. Defaults to manifest's declared routes.

    Returns
    -------
    dict with keys:
        routes : list[dict] - per-route a11y results
        passed : bool - True if no critical/serious violations
    """
    if workspace is None:
        workspace = resolve_workspace()

    try:
        manifest = load_manifest(workspace)
    except ManifestError as exc:
        return {
            "routes": [],
            "passed": False,
            "error": str(exc),
        }

    if routes is None:
        routes = manifest.routes

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run_a11y_check(manifest, workspace, routes))
                return future.result(timeout=60)
        else:
            return loop.run_until_complete(_run_a11y_check(manifest, workspace, routes))
    except RuntimeError:
        return asyncio.run(_run_a11y_check(manifest, workspace, routes))
