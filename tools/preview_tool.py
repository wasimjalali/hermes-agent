"""preview tool - dev server boot, Playwright screenshots, error capture.

Boots the dev server from the manifest, waits for it to be ready, then
drives Playwright through each declared route to capture screenshots and
collect console/network errors.

The dev server's stdout/stderr are captured continuously via the Runtime
seam so mid-session crashes surface in the tool output.

Tool schema:
    preview(routes: list[str] | None = None) -> { routes: list[RouteResult], server_healthy: bool }

Each RouteResult: { path: str, screenshot: str, console_errors: list, network_errors: list, status: int }
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

logger = logging.getLogger("hermes.preview_tool")

# How long to wait for the dev server to become ready.
_SERVER_READY_TIMEOUT = 30

# Playwright viewport.
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 800

# Directory within workspace for preview artifacts.
_PREVIEW_DIR = ".burooj/preview"


@dataclass
class RouteResult:
    """Result of previewing a single route."""

    path: str
    screenshot: str = ""
    console_errors: list[str] = field(default_factory=list)
    console_warnings: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    status: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "screenshot": self.screenshot,
            "console_errors": self.console_errors,
            "console_warnings": self.console_warnings,
            "network_errors": self.network_errors,
            "status": self.status,
        }


async def _capture_route(
    page: Any, base_url: str, route_path: str, screenshot_dir: Path
) -> RouteResult:
    """Navigate to a route and capture screenshot + errors.

    The caller supplies a page dedicated to this route. Listeners registered
    here therefore stop mattering once the page closes. On a shared page they
    did not: each route added another handler, and an earlier route's error
    list kept growing while later routes loaded, long after it was returned.
    """
    result = RouteResult(path=route_path)
    console_errors: list[str] = []
    console_warnings: list[str] = []
    network_errors: list[str] = []

    def on_console(msg: Any) -> None:
        # Errors and warnings are not the same signal and must not share a
        # field: a dev server warns routinely, and folding those into
        # console_errors made every route look broken.
        if msg.type == "error":
            console_errors.append(msg.text)
        elif msg.type == "warning":
            console_warnings.append(msg.text)

    page.on("console", on_console)

    def on_page_error(exc: Any) -> None:
        console_errors.append(f"Uncaught: {exc}")

    page.on("pageerror", on_page_error)

    def on_request_failed(request: Any) -> None:
        network_errors.append(f"{request.method} {request.url} - {request.failure}")

    page.on("requestfailed", on_request_failed)

    url = f"{base_url}{route_path}"
    try:
        response = await page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS)
        result.status = response.status if response else 0
    except Exception as exc:
        result.status = 0
        network_errors.append(f"Navigation failed: {exc}")

    # Take screenshot.
    screenshot_name = route_path.strip("/").replace("/", "_") or "index"
    screenshot_path = screenshot_dir / f"{screenshot_name}.png"
    try:
        # Full page, not the viewport. A fold-only screenshot means the
        # model reviewing its own work sees the top 800px of a page it just
        # built, which is where the fewest mistakes are.
        await page.screenshot(path=str(screenshot_path), full_page=True)
        result.screenshot = str(screenshot_path)
    except Exception as exc:
        logger.warning("Screenshot failed for %s: %s", route_path, exc)

    result.console_errors = console_errors
    result.console_warnings = console_warnings
    result.network_errors = network_errors
    return result


async def _run_preview(session: BuildSession, routes: list[str]) -> dict[str, Any]:
    """Async implementation of preview, against the shared dev server."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"routes": [], "server_healthy": False, "error": PLAYWRIGHT_MISSING}

    screenshot_dir = session.workspace / _PREVIEW_DIR
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    status = session.ensure_server(timeout=_SERVER_READY_TIMEOUT)
    if not status.ready:
        return {
            "routes": [],
            "server_healthy": False,
            "error": status.reason,
            "server_stdout": status.stdout[-50:],
            "server_stderr": status.stderr[-50:],
        }

    route_results: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for route_path in routes:
                page = await new_page(browser, _VIEWPORT_WIDTH, _VIEWPORT_HEIGHT)
                result = await _capture_route(
                    page, status.base_url, route_path, screenshot_dir
                )
                route_results.append(result.to_dict())
                await page.close()
        finally:
            await browser.close()

    # The dev server deliberately stays up. Preview is called repeatedly during
    # a session, and paying a cold Next.js boot on every call was the single
    # biggest source of latency in the loop.
    server_stdout, server_stderr = session.server_output()
    result = {
        "routes": route_results,
        "server_healthy": session.server_running,
        "server_reused": status.reused,
        "server_stdout": server_stdout[-100:],
        "server_stderr": server_stderr[-100:],
    }

    # The desktop Build panel shows the most recent preview. Record it here
    # so a model-driven preview inside a session is visible to the panel.
    from agent.burooj_status import record_preview

    record_preview(session.workspace, result)

    return result


def preview(
    workspace: Optional[Path] = None,
    routes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Boot dev server, capture route screenshots, report errors.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    routes : list[str], optional
        Routes to preview. Defaults to manifest's declared routes.

    Returns
    -------
    dict with keys:
        routes : list[dict] - per-route results
        server_healthy : bool - True if server was still running after capture
        server_stdout : list[str] - last 100 lines of dev server stdout
        server_stderr : list[str] - last 100 lines of dev server stderr
    """
    if workspace is None:
        workspace = resolve_workspace()

    try:
        session = get_session(Path(workspace))
    except ManifestError as exc:
        return {"routes": [], "server_healthy": False, "error": str(exc)}

    if session.manifest.dev is None:
        return {
            "routes": [],
            "server_healthy": False,
            "error": "No 'dev' section in burooj.build.json, so there is no server to preview.",
        }

    if routes is None:
        routes = session.manifest.routes

    try:
        return run_async(_run_preview(session, routes), timeout=_CAPTURE_TIMEOUT)
    except TimeoutError as exc:
        return {"routes": [], "server_healthy": session.server_running, "error": str(exc)}


# ── Tool registration ───────────────────────────────────────────────────────

PREVIEW_SCHEMA = {
    "name": "preview",
    "description": (
        "Boot the dev server and drive a real browser over each declared "
        "route: screenshot, console errors, uncaught exceptions and failed "
        "requests, plus the server's own stdout/stderr. This is how you see "
        "whether the app actually renders, not just whether it compiled. The "
        "server stays up between calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "routes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Routes to capture. Omit to use the manifest's routes.",
            },
        },
        "required": [],
    },
}


def handle_preview(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for the preview tool."""
    routes = args.get("routes")
    if routes is not None and (
        not isinstance(routes, list) or not all(isinstance(r, str) for r in routes)
    ):
        return json.dumps({"error": "'routes' must be an array of strings."})
    try:
        return json.dumps(preview(routes=routes), indent=2)
    except Exception as exc:
        logger.exception("preview failed")
        return json.dumps({"error": f"preview crashed: {type(exc).__name__}: {exc}"})


registry.register(
    name="preview",
    toolset="burooj_build",
    schema=PREVIEW_SCHEMA,
    handler=handle_preview,
    emoji="🔍",
)
