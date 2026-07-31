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

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import BuildManifest, ManifestError, load_manifest
from agent.build_runtime import ProcessHandle, get_runtime
from agent.build_workspace import resolve_workspace

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
    network_errors: list[str] = field(default_factory=list)
    status: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "screenshot": self.screenshot,
            "console_errors": self.console_errors,
            "network_errors": self.network_errors,
            "status": self.status,
        }


async def _capture_route(
    page: Any, base_url: str, route_path: str, screenshot_dir: Path
) -> RouteResult:
    """Navigate to a route and capture screenshot + errors."""
    result = RouteResult(path=route_path)
    console_errors: list[str] = []
    network_errors: list[str] = []

    # Listen for console errors.
    def on_console(msg: Any) -> None:
        if msg.type in ("error", "warning"):
            console_errors.append(f"[{msg.type}] {msg.text}")

    page.on("console", on_console)

    # Listen for network failures.
    def on_request_failed(request: Any) -> None:
        network_errors.append(f"{request.method} {request.url} - {request.failure}")

    page.on("requestfailed", on_request_failed)

    url = f"{base_url}{route_path}"
    try:
        response = await page.goto(url, wait_until="networkidle", timeout=15000)
        result.status = response.status if response else 0
    except Exception as exc:
        result.status = 0
        network_errors.append(f"Navigation failed: {exc}")

    # Take screenshot.
    screenshot_name = route_path.strip("/").replace("/", "_") or "index"
    screenshot_path = screenshot_dir / f"{screenshot_name}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=False)
        result.screenshot = str(screenshot_path)
    except Exception as exc:
        logger.warning("Screenshot failed for %s: %s", route_path, exc)

    result.console_errors = console_errors
    result.network_errors = network_errors
    return result


async def _run_preview(
    manifest: BuildManifest,
    workspace: Path,
    routes: list[str],
) -> dict[str, Any]:
    """Async implementation of preview."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "routes": [],
            "server_healthy": False,
            "error": "playwright not installed. Run: pip install playwright && python -m playwright install chromium",
        }

    # Ensure screenshot directory exists.
    screenshot_dir = workspace / _PREVIEW_DIR
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Start the dev server via the Runtime seam (continuous stdout/stderr capture).
    runtime = get_runtime()
    env = {"PORT": str(manifest.dev.port)}
    handle: ProcessHandle = runtime.start_process(
        command=manifest.dev.command,
        cwd=workspace,
        env=env,
    )

    try:
        # Wait for server to be ready via the Runtime seam.
        ready = runtime.wait_for_port(
            port=manifest.dev.port,
            timeout=_SERVER_READY_TIMEOUT,
            path=manifest.dev.ready,
        )
        if not ready:
            # Collect whatever the server printed before dying.
            server_stdout = handle.get_stdout()
            server_stderr = handle.get_stderr()
            return {
                "routes": [],
                "server_healthy": False,
                "error": f"Dev server did not become ready within {_SERVER_READY_TIMEOUT}s",
                "server_stdout": server_stdout[-50:] if server_stdout else [],
                "server_stderr": server_stderr[-50:] if server_stderr else [],
            }

        base_url = f"http://localhost:{manifest.dev.port}"

        # Drive Playwright through each route.
        route_results: list[dict[str, Any]] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT}
            )
            page = await context.new_page()

            for route_path in routes:
                result = await _capture_route(page, base_url, route_path, screenshot_dir)
                route_results.append(result.to_dict())

            await browser.close()

        # Collect server output captured during the run.
        server_stdout = handle.get_stdout()
        server_stderr = handle.get_stderr()

        return {
            "routes": route_results,
            "server_healthy": handle.is_running,
            "server_stdout": server_stdout[-100:] if server_stdout else [],
            "server_stderr": server_stderr[-100:] if server_stderr else [],
        }
    finally:
        runtime.stop_process(handle)


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

    # Load the manifest.
    try:
        manifest = load_manifest(workspace)
    except ManifestError as exc:
        return {
            "routes": [],
            "server_healthy": False,
            "error": str(exc),
        }

    # Use manifest routes if none specified.
    if routes is None:
        routes = manifest.routes

    # Run the async preview.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If already in an async context, create a new loop in a thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run_preview(manifest, workspace, routes))
                return future.result(timeout=60)
        else:
            return loop.run_until_complete(_run_preview(manifest, workspace, routes))
    except RuntimeError:
        return asyncio.run(_run_preview(manifest, workspace, routes))
