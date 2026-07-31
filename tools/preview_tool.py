"""preview tool - dev server boot, Playwright screenshots, error capture.

Boots the dev server from the manifest, waits for it to be ready, then
drives Playwright through each declared route to capture screenshots and
collect console/network errors.

Tool schema:
    preview(routes: list[str] | None = None) -> { routes: list[RouteResult], server_healthy: bool }

Each RouteResult: { path: str, screenshot: str, console_errors: list, network_errors: list, status: int }
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import BuildManifest, ManifestError, load_manifest
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


class _DevServer:
    """Manages the dev server process lifecycle."""

    def __init__(self, command: str, port: int, cwd: Path):
        self.command = command
        self.port = port
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []

    def start(self) -> None:
        """Start the dev server process."""
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["BROWSER"] = "none"  # Prevent auto-opening browser.
        self.process = subprocess.Popen(
            self.command,
            shell=True,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
            env=env,
        )

    def wait_ready(self, ready_path: str, timeout: int = _SERVER_READY_TIMEOUT) -> bool:
        """Wait for the server to respond at the ready endpoint."""
        import urllib.request
        import urllib.error

        url = f"http://localhost:{self.port}{ready_path}"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(url, method="HEAD")
                resp = urllib.request.urlopen(req, timeout=2)
                if resp.status < 500:
                    return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.5)

        return False

    def stop(self) -> None:
        """Stop the dev server process."""
        if self.process is not None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            self.process = None

    @property
    def is_running(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None


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

    # Start the dev server.
    server = _DevServer(
        command=manifest.dev.command,
        port=manifest.dev.port,
        cwd=workspace,
    )
    server.start()

    try:
        # Wait for server to be ready.
        ready = server.wait_ready(manifest.dev.ready)
        if not ready:
            return {
                "routes": [],
                "server_healthy": False,
                "error": f"Dev server did not become ready within {_SERVER_READY_TIMEOUT}s",
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

        return {
            "routes": route_results,
            "server_healthy": server.is_running,
        }
    finally:
        server.stop()


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
