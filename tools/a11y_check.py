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

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import ManifestError
from agent.build_session import BuildSession, get_session
from agent.build_workspace import resolve_workspace
from tools.burooj_browser import (
    _CAPTURE_TIMEOUT,
    _NAV_TIMEOUT_MS,
    _WAIT_UNTIL,
    PLAYWRIGHT_MISSING,
    new_page,
    run_async,
)
from tools.registry import registry

logger = logging.getLogger("hermes.a11y_check")

# Where axe-core is looked for, in order. A deterministic gate should not
# depend on a CDN being reachable, nor inject unpinned third-party script into
# the user's running app on every run, so a local copy always wins.
#
#   1. vendor/axe.min.js next to the Hermes checkout (offline, reproducible)
#   2. the workspace's own node_modules/axe-core (matches what the app tests
#      against, and npm already verified its integrity hash)
#
# When neither exists the check SKIPS with instructions rather than reaching
# for the network. Install it once with either of:
#   npm i -D axe-core          (in the workspace)
#   curl -o vendor/axe.min.js https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js
_VENDORED_AXE = Path(__file__).resolve().parent.parent / "vendor" / "axe.min.js"
_WORKSPACE_AXE_RELATIVE = Path("node_modules") / "axe-core" / "axe.min.js"

_AXE_INSTALL_HINT = (
    "axe-core not found. Install it in the workspace with 'npm i -D axe-core', "
    "or vendor it once into hermes-agent/vendor/axe.min.js. The a11y gate does "
    "not fetch it from a CDN: an accessibility gate that depends on the network "
    "is not deterministic."
)

# Impacts that cause a hard fail.
_FAIL_IMPACTS = frozenset({"critical", "serious"})

# Server ready timeout.
_SERVER_TIMEOUT = 45


def _load_axe_source(workspace: Path) -> Optional[str]:
    """Return the axe-core script text from the first local source available."""
    for candidate in (_VENDORED_AXE, workspace / _WORKSPACE_AXE_RELATIVE):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read axe-core at %s: %s", candidate, exc)
    return None


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


async def _check_route(
    page: Any, base_url: str, route_path: str, axe_source: str
) -> RouteA11y:
    """Navigate to a route, inject axe-core, and run the check."""
    result = RouteA11y(path=route_path)

    url = f"{base_url}{route_path}"
    try:
        await page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS)
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

    # Inject axe-core from the local copy. add_script_tag with `content`
    # evaluates the script as a script tag, which is what a UMD bundle expects;
    # page.evaluate would treat it as an expression and fail on some builds.
    try:
        await page.add_script_tag(content=axe_source)
        await page.wait_for_function("typeof window.axe !== 'undefined'", timeout=10000)
    except Exception as exc:
        result.violations.append(A11yViolation(
            id="axe-injection-error",
            impact="serious",
            description=f"Could not inject axe-core: {exc}",
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
    session: BuildSession, routes: list[str], axe_source: str
) -> dict[str, Any]:
    """Async implementation of a11y_check, against the shared dev server."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"routes": [], "passed": True, "status": "skip", "reason": PLAYWRIGHT_MISSING}

    status = session.ensure_server(timeout=_SERVER_TIMEOUT)
    if not status.ready:
        # A server that will not start is not an accessible app. The first cut
        # reported this as a SKIP, which let the design gate pass green while
        # nothing had been checked.
        return {
            "routes": [],
            "passed": False,
            "status": "error",
            "error": status.reason,
            "server_stderr": status.stderr[-20:],
        }

    route_results: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for route_path in routes:
                # Fresh page per route so nothing leaks between them.
                page = await new_page(browser)
                result = await _check_route(page, status.base_url, route_path, axe_source)
                route_results.append(result.to_dict())
                await page.close()
        finally:
            await browser.close()

    all_passed = all(r["error_count"] == 0 for r in route_results)
    total = sum(r["error_count"] for r in route_results)
    return {
        "routes": route_results,
        "passed": all_passed,
        "status": "pass" if all_passed else "fail",
        "summary": (
            f"{len(route_results)} route(s) clean"
            if all_passed
            else f"{total} critical/serious violation(s)"
        ),
    }


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

    result = _a11y_check_impl(Path(workspace), routes)

    # The desktop Design panel shows the last a11y run. Record it here so a
    # model-driven run inside a session is visible to the panel.
    from agent.burooj_status import record_design_check

    record_design_check(workspace, "a11y_check", result)

    return result


def _a11y_check_impl(workspace: Path, routes: Optional[list[str]]) -> dict[str, Any]:
    """Shared a11y implementation; ``a11y_check`` records the result."""
    try:
        session = get_session(workspace)
    except ManifestError as exc:
        return {"routes": [], "passed": False, "status": "skip", "reason": str(exc)}

    if session.manifest.dev is None:
        return {
            "routes": [],
            "passed": True,
            "status": "skip",
            "reason": "no 'dev' section in burooj.build.json, nothing to serve",
        }

    axe_source = _load_axe_source(session.workspace)
    if axe_source is None:
        return {"routes": [], "passed": True, "status": "skip", "reason": _AXE_INSTALL_HINT}

    if routes is None:
        routes = session.manifest.routes

    try:
        return run_async(
            _run_a11y_check(session, routes, axe_source), timeout=_CAPTURE_TIMEOUT
        )
    except TimeoutError as exc:
        return {"routes": [], "passed": False, "status": "error", "error": str(exc)}


# ── Tool registration ───────────────────────────────────────────────────────

A11Y_CHECK_SCHEMA = {
    "name": "a11y_check",
    "description": (
        "Run axe-core over every declared route and report WCAG 2.1 AA "
        "violations. Only critical and serious violations fail; moderate and "
        "minor are advisory. Rung 3 of the design gate. Needs a dev server and "
        "a local axe-core (npm i -D axe-core)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "routes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Routes to check. Omit to use the manifest's routes.",
            },
        },
        "required": [],
    },
}


def handle_a11y_check(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for a11y_check."""
    routes = args.get("routes")
    if routes is not None and (
        not isinstance(routes, list) or not all(isinstance(r, str) for r in routes)
    ):
        return json.dumps({"error": "'routes' must be an array of strings."})
    try:
        return json.dumps(a11y_check(routes=routes), indent=2)
    except Exception as exc:
        logger.exception("a11y_check failed")
        return json.dumps(
            {"error": f"a11y_check crashed: {type(exc).__name__}: {exc}", "passed": False}
        )


registry.register(
    name="a11y_check",
    toolset="burooj_design",
    schema=A11Y_CHECK_SCHEMA,
    handler=handle_a11y_check,
    emoji="♿",
)
