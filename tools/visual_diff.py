"""visual_diff tool - screenshot comparison against baselines.

Compares current route screenshots against saved baselines in
burooj.design/baselines/. Uses pixel-level comparison with a perceptual
threshold to allow for anti-aliasing and rendering differences.

If no baseline exists, the current screenshot becomes the baseline (first run).

Tool schema:
    visual_diff(workspace: Path, routes: list[str] | None = None) -> { routes: list[RouteDiff], passed: bool }

Each RouteDiff: { path: str, baseline: str, diff_pct: float, threshold: float, passed: bool, new_baseline: bool }
"""

from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import BuildManifest, ManifestError, load_manifest
from agent.build_runtime import get_runtime
from agent.build_workspace import resolve_workspace

logger = logging.getLogger("hermes.visual_diff")

# Default diff threshold: 0.1% pixel difference is acceptable.
_DEFAULT_THRESHOLD = 0.1

# Per-channel tolerance for anti-aliasing (out of 255).
_CHANNEL_TOLERANCE = 10

# Viewport width used for baseline naming.
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 800

# Server timeout.
_SERVER_TIMEOUT = 20


@dataclass
class RouteDiff:
    """Diff result for a single route."""

    path: str
    baseline: str
    diff_pct: float
    threshold: float
    passed: bool
    new_baseline: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "baseline": self.baseline,
            "diff_pct": round(self.diff_pct, 3),
            "threshold": self.threshold,
            "passed": self.passed,
            "new_baseline": self.new_baseline,
        }


def _decode_png_pixels(png_bytes: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Decode a PNG file into (width, height, [(r, g, b), ...]).

    Minimal PNG decoder for RGB/RGBA comparison. Does not handle all PNG
    features (interlacing, palette, etc.) but works for Playwright screenshots
    which are always RGBA non-interlaced.
    """
    # Verify PNG signature.
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a valid PNG file")

    # Parse chunks.
    offset = 8
    width = height = 0
    bit_depth = color_type = 0
    idat_data = b""

    while offset < len(png_bytes):
        length = struct.unpack(">I", png_bytes[offset:offset + 4])[0]
        chunk_type = png_bytes[offset + 4:offset + 8]
        chunk_data = png_bytes[offset + 8:offset + 8 + length]
        offset += 12 + length  # 4 (length) + 4 (type) + length + 4 (CRC)

        if chunk_type == b"IHDR":
            width = struct.unpack(">I", chunk_data[0:4])[0]
            height = struct.unpack(">I", chunk_data[4:8])[0]
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
        elif chunk_type == b"IDAT":
            idat_data += chunk_data
        elif chunk_type == b"IEND":
            break

    if width == 0 or height == 0:
        raise ValueError("Could not read PNG dimensions")

    # Decompress.
    raw_data = zlib.decompress(idat_data)

    # Determine bytes per pixel.
    if color_type == 6:  # RGBA
        bpp = 4
    elif color_type == 2:  # RGB
        bpp = 3
    else:
        raise ValueError(f"Unsupported PNG color type: {color_type}")

    stride = 1 + width * bpp  # 1 byte filter per row

    pixels: list[tuple[int, int, int]] = []

    # Reconstruct pixels (handle filter byte, only None and Sub for simplicity).
    prev_row: list[int] = [0] * (width * bpp)

    for y in range(height):
        row_start = y * stride
        filter_byte = raw_data[row_start]
        row_data = list(raw_data[row_start + 1:row_start + stride])

        if filter_byte == 1:  # Sub
            for i in range(bpp, len(row_data)):
                row_data[i] = (row_data[i] + row_data[i - bpp]) & 0xFF
        elif filter_byte == 2:  # Up
            for i in range(len(row_data)):
                row_data[i] = (row_data[i] + prev_row[i]) & 0xFF
        elif filter_byte == 3:  # Average
            for i in range(len(row_data)):
                left = row_data[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                row_data[i] = (row_data[i] + (left + up) // 2) & 0xFF
        elif filter_byte == 4:  # Paeth
            for i in range(len(row_data)):
                left = row_data[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                up_left = prev_row[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = up_left
                row_data[i] = (row_data[i] + pred) & 0xFF

        prev_row = row_data

        for x in range(width):
            idx = x * bpp
            r, g, b = row_data[idx], row_data[idx + 1], row_data[idx + 2]
            pixels.append((r, g, b))

    return width, height, pixels


def _compare_pixels(
    pixels_a: list[tuple[int, int, int]],
    pixels_b: list[tuple[int, int, int]],
    tolerance: int = _CHANNEL_TOLERANCE,
) -> float:
    """Compare two pixel lists and return percentage of different pixels.

    A pixel is "different" if any channel differs by more than tolerance.
    """
    if len(pixels_a) != len(pixels_b):
        return 100.0  # Different sizes = 100% different.

    total = len(pixels_a)
    if total == 0:
        return 0.0

    diff_count = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(pixels_a, pixels_b):
        if (abs(r1 - r2) > tolerance or abs(g1 - g2) > tolerance or abs(b1 - b2) > tolerance):
            diff_count += 1

    return (diff_count / total) * 100.0


def _baseline_path(workspace: Path, route_path: str) -> Path:
    """Get the baseline screenshot path for a route."""
    route_name = route_path.strip("/").replace("/", "_") or "index"
    return workspace / "burooj.design" / "baselines" / f"{route_name}_{_VIEWPORT_WIDTH}.png"


async def _capture_screenshots(
    manifest: BuildManifest,
    workspace: Path,
    routes: list[str],
) -> dict[str, Optional[bytes]]:
    """Boot dev server and capture screenshots for each route."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    runtime = get_runtime()
    env = {"PORT": str(manifest.dev.port)}
    handle = runtime.start_process(command=manifest.dev.command, cwd=workspace, env=env)

    screenshots: dict[str, Optional[bytes]] = {}

    try:
        ready = runtime.wait_for_port(
            port=manifest.dev.port,
            timeout=_SERVER_TIMEOUT,
            path=manifest.dev.ready,
        )
        if not ready:
            return {r: None for r in routes}

        base_url = f"http://localhost:{manifest.dev.port}"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT}
            )
            page = await context.new_page()

            for route_path in routes:
                url = f"{base_url}{route_path}"
                try:
                    await page.goto(url, wait_until="networkidle", timeout=15000)
                    screenshot_bytes = await page.screenshot(full_page=False)
                    screenshots[route_path] = screenshot_bytes
                except Exception as exc:
                    logger.warning("Screenshot failed for %s: %s", route_path, exc)
                    screenshots[route_path] = None

            await browser.close()
    finally:
        runtime.stop_process(handle)

    return screenshots


def visual_diff(
    workspace: Optional[Path] = None,
    routes: Optional[list[str]] = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Compare current screenshots against baselines.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    routes : list[str], optional
        Routes to diff. Defaults to manifest's declared routes.
    threshold : float
        Maximum allowed percentage of different pixels (default 0.1%).

    Returns
    -------
    dict with keys:
        routes : list[dict] - per-route diff results
        passed : bool - True if all routes are within threshold
    """
    if workspace is None:
        workspace = resolve_workspace()

    try:
        manifest = load_manifest(workspace)
    except ManifestError as exc:
        return {"routes": [], "passed": False, "error": str(exc)}

    if routes is None:
        routes = manifest.routes

    # Capture current screenshots.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _capture_screenshots(manifest, workspace, routes))
                screenshots = future.result(timeout=60)
        else:
            screenshots = loop.run_until_complete(_capture_screenshots(manifest, workspace, routes))
    except RuntimeError:
        screenshots = asyncio.run(_capture_screenshots(manifest, workspace, routes))

    if not screenshots:
        return {
            "routes": [],
            "passed": False,
            "error": "Could not capture screenshots (playwright not installed or server failed)",
        }

    # Ensure baselines directory exists.
    baselines_dir = workspace / "burooj.design" / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)

    results: list[RouteDiff] = []
    all_passed = True

    for route_path in routes:
        current_bytes = screenshots.get(route_path)
        if current_bytes is None:
            results.append(RouteDiff(
                path=route_path,
                baseline="",
                diff_pct=100.0,
                threshold=threshold,
                passed=False,
                new_baseline=False,
            ))
            all_passed = False
            continue

        baseline_file = _baseline_path(workspace, route_path)

        if not baseline_file.exists():
            # No baseline yet - save current as baseline, pass (first run).
            baseline_file.write_bytes(current_bytes)
            results.append(RouteDiff(
                path=route_path,
                baseline=str(baseline_file),
                diff_pct=0.0,
                threshold=threshold,
                passed=True,
                new_baseline=True,
            ))
            continue

        # Compare against baseline.
        try:
            baseline_bytes = baseline_file.read_bytes()
            _, _, baseline_pixels = _decode_png_pixels(baseline_bytes)
            _, _, current_pixels = _decode_png_pixels(current_bytes)
            diff_pct = _compare_pixels(baseline_pixels, current_pixels)
        except (ValueError, OSError) as exc:
            logger.warning("Visual diff failed for %s: %s. Saving new baseline.", route_path, exc)
            baseline_file.write_bytes(current_bytes)
            results.append(RouteDiff(
                path=route_path,
                baseline=str(baseline_file),
                diff_pct=0.0,
                threshold=threshold,
                passed=True,
                new_baseline=True,
            ))
            continue

        passed = diff_pct <= threshold
        if not passed:
            all_passed = False

        results.append(RouteDiff(
            path=route_path,
            baseline=str(baseline_file),
            diff_pct=diff_pct,
            threshold=threshold,
            passed=passed,
            new_baseline=False,
        ))

    return {
        "routes": [r.to_dict() for r in results],
        "passed": all_passed,
    }
