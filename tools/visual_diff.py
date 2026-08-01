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

import json
import logging
import struct
import zlib
from dataclasses import dataclass
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

logger = logging.getLogger("hermes.visual_diff")

# Default diff threshold: 0.1% pixel difference is acceptable.
_DEFAULT_THRESHOLD = 0.1

# Per-channel tolerance for anti-aliasing (out of 255).
_CHANNEL_TOLERANCE = 10

# Viewport width used for baseline naming when the caller does not pick
# breakpoints (legacy single-cell callers).
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 800

# The capture matrix, per spec §5.3: route × breakpoint × theme. Every cell
# gets its own baseline so a responsive or theme regression is caught where
# it happens, not averaged away across the matrix.
DEFAULT_BREAKPOINTS = (390, 768, 1280)
DEFAULT_THEMES = ("light", "dark")

# Server timeout.
_SERVER_TIMEOUT = 20


@dataclass
class RouteDiff:
    """Diff result for a single route × breakpoint × theme cell."""

    path: str
    baseline: str
    diff_pct: float
    threshold: float
    passed: bool
    new_baseline: bool
    breakpoint: int = 0
    theme: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "path": self.path,
            "baseline": self.baseline,
            "diff_pct": round(self.diff_pct, 3),
            "threshold": self.threshold,
            "passed": self.passed,
            "new_baseline": self.new_baseline,
            "breakpoint": self.breakpoint,
            "theme": self.theme,
        }
        if self.note:
            data["note"] = self.note
        return data


class PngDecodeError(ValueError):
    """Raised when a PNG cannot be decoded. Never let this escape as zlib.error."""


def _decode_with_pillow(png_bytes: bytes) -> Optional[tuple[int, int, bytes]]:
    """Decode via Pillow when it is installed. Returns None when it is not.

    Pillow does this in C. The pure-Python fallback below spends about a
    second per 1280x800 Paeth-filtered screenshot, and the design gate decodes
    two of those per route.
    """
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            rgb = img.convert("RGB")
            return rgb.width, rgb.height, rgb.tobytes()
    except Exception as exc:
        raise PngDecodeError(f"Pillow could not decode the image: {exc}") from exc


def _decode_png_pixels(png_bytes: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG into (width, height, packed RGB bytes).

    Minimal decoder for RGB/RGBA comparison. Does not handle every PNG feature
    (interlacing, palette) but covers Playwright screenshots, which are always
    8-bit RGBA non-interlaced. Every failure path raises
    :class:`PngDecodeError` so callers can catch one type: the original let
    ``zlib.error`` and ``struct.error`` escape, and a half-written baseline
    crashed the whole verify ladder with a traceback.
    """
    if (pillow := _decode_with_pillow(png_bytes)) is not None:
        return pillow

    # Verify PNG signature.
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngDecodeError("Not a valid PNG file")

    # Parse chunks.
    offset = 8
    width = height = 0
    bit_depth = color_type = 0
    idat_data = b""

    try:
        while offset < len(png_bytes):
            if offset + 8 > len(png_bytes):
                raise PngDecodeError("Truncated PNG: incomplete chunk header")
            length = struct.unpack(">I", png_bytes[offset:offset + 4])[0]
            chunk_type = png_bytes[offset + 4:offset + 8]
            if offset + 8 + length > len(png_bytes):
                raise PngDecodeError("Truncated PNG: chunk extends past end of file")
            chunk_data = png_bytes[offset + 8:offset + 8 + length]
            offset += 12 + length  # 4 (length) + 4 (type) + length + 4 (CRC)

            if chunk_type == b"IHDR":
                if len(chunk_data) < 10:
                    raise PngDecodeError("Malformed PNG: short IHDR")
                width = struct.unpack(">I", chunk_data[0:4])[0]
                height = struct.unpack(">I", chunk_data[4:8])[0]
                bit_depth = chunk_data[8]
                color_type = chunk_data[9]
            elif chunk_type == b"IDAT":
                idat_data += chunk_data
            elif chunk_type == b"IEND":
                break
    except struct.error as exc:
        raise PngDecodeError(f"Malformed PNG structure: {exc}") from exc

    if width == 0 or height == 0:
        raise PngDecodeError("Could not read PNG dimensions")
    if bit_depth != 8:
        raise PngDecodeError(
            f"Unsupported PNG bit depth {bit_depth}; this decoder handles 8-bit only"
        )

    try:
        raw_data = zlib.decompress(idat_data)
    except zlib.error as exc:
        raise PngDecodeError(f"Corrupt PNG image data: {exc}") from exc

    # Determine bytes per pixel.
    if color_type == 6:  # RGBA
        bpp = 4
    elif color_type == 2:  # RGB
        bpp = 3
    else:
        raise PngDecodeError(f"Unsupported PNG color type: {color_type}")

    if len(raw_data) < height * (1 + width * bpp):
        raise PngDecodeError("Truncated PNG: image data shorter than declared size")

    stride = 1 + width * bpp  # 1 byte filter per row

    pixels = bytearray()

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

        if bpp == 3:
            pixels += bytes(row_data)
        else:  # drop the alpha channel
            for x in range(0, width * bpp, bpp):
                pixels += bytes(row_data[x:x + 3])

    return width, height, bytes(pixels)


async def capture_page(page: Any) -> bytes:
    """Screenshot the whole document, not just the viewport.

    Viewport-only capture meant the gate compared the top ``_VIEWPORT_HEIGHT``
    pixels and nothing else. On the B6 landing page that was 800 of 2234
    pixels, so roughly two thirds of every route was outside the check whose
    only job is noticing that something changed.

    A height change now shows up as a size mismatch, which ``_compare_pixels``
    scores 100%. That is the intended reading: the page got longer.
    """
    return await page.screenshot(full_page=True)


def _compare_pixels(
    pixels_a: bytes,
    pixels_b: bytes,
    tolerance: int = _CHANNEL_TOLERANCE,
) -> float:
    """Return the percentage of pixels that differ beyond *tolerance*.

    Both inputs are packed RGB triples. A pixel counts as different when any
    channel differs by more than the tolerance, which absorbs anti-aliasing.
    """
    if len(pixels_a) != len(pixels_b):
        return 100.0  # Different sizes = 100% different.

    total = len(pixels_a) // 3
    if total == 0:
        return 0.0

    # Fast path: identical bytes is the common case for an unchanged route.
    if pixels_a == pixels_b:
        return 0.0

    diff_count = 0
    for i in range(0, len(pixels_a), 3):
        if (
            abs(pixels_a[i] - pixels_b[i]) > tolerance
            or abs(pixels_a[i + 1] - pixels_b[i + 1]) > tolerance
            or abs(pixels_a[i + 2] - pixels_b[i + 2]) > tolerance
        ):
            diff_count += 1

    return (diff_count / total) * 100.0


def _route_slug(route_path: str) -> str:
    """Filesystem-safe route name. Slashes and other separators collapse so
    every dimension of the matrix is encoded in the filename below."""
    name = route_path.strip("/").replace("/", "_") or "index"
    for ch in ("\\", ":", "?", "#", "%", " "):
        name = name.replace(ch, "_")
    return name


def _baseline_path(
    workspace: Path,
    route_path: str,
    breakpoint: int = _VIEWPORT_WIDTH,
    theme: str = "",
) -> Path:
    """Get the baseline screenshot path for a route × breakpoint × theme cell.

    The filename encodes all three dimensions: ``<route>_<breakpoint>_<theme>.png``
    for themed cells, ``<route>_<breakpoint>.png`` for the legacy theme-less
    form. A route captured at two breakpoints or in two themes never collides.
    """
    slug = _route_slug(route_path)
    if theme:
        name = f"{slug}_{breakpoint}_{theme}.png"
    else:
        name = f"{slug}_{breakpoint}.png"
    return workspace / "burooj.design" / "baselines" / name


async def _capture_screenshots(
    session: BuildSession,
    routes: list[str],
    breakpoints: tuple[int, ...],
    themes: tuple[str, ...],
) -> dict[str, Any]:
    """Capture a screenshot per route × breakpoint × theme cell.

    Keys are ``(route_path, breakpoint, theme)``. The theme is applied
    through Playwright's ``color_scheme`` so ``prefers-color-scheme`` resolves
    the way a real browser would resolve it.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": PLAYWRIGHT_MISSING, "screenshots": {}}

    status = session.ensure_server(timeout=_SERVER_TIMEOUT)
    if not status.ready:
        return {"error": status.reason, "screenshots": {}}

    screenshots: dict[tuple[str, int, str], Optional[bytes]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for breakpoint in breakpoints:
                for theme in themes:
                    context = await browser.new_context(
                        viewport={"width": breakpoint, "height": _VIEWPORT_HEIGHT},
                        color_scheme=theme,
                    )
                    try:
                        page = await context.new_page()
                        for route_path in routes:
                            url = f"{status.base_url}{route_path}"
                            try:
                                await page.goto(
                                    url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS
                                )
                                screenshots[(route_path, breakpoint, theme)] = (
                                    await capture_page(page)
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Screenshot failed for %s @%s/%s: %s",
                                    route_path, breakpoint, theme, exc,
                                )
                                screenshots[(route_path, breakpoint, theme)] = None
                    finally:
                        await context.close()
        finally:
            await browser.close()

    return {"screenshots": screenshots}


def visual_diff(
    workspace: Optional[Path] = None,
    routes: Optional[list[str]] = None,
    threshold: float = _DEFAULT_THRESHOLD,
    update_baselines: bool = False,
    breakpoints: Optional[list[int]] = None,
    themes: Optional[list[str]] = None,
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
    breakpoints : list[int], optional
        Viewport widths to capture. Defaults to (390, 768, 1280).
    themes : list[str], optional
        Color schemes to capture ("light", "dark"). Defaults to both.

    Returns
    -------
    dict with keys:
        routes : list[dict] - per-cell diff results (route × breakpoint × theme)
        passed : bool - True if all cells with a baseline are within threshold
    """
    if workspace is None:
        workspace = resolve_workspace()

    bps = _normalize_breakpoints(breakpoints)
    ths = _normalize_themes(themes)

    result = _visual_diff_impl(
        Path(workspace), routes, threshold, update_baselines, bps, ths
    )

    # The desktop Design panel shows the last visual diff. Record it here so
    # a model-driven run inside a session is visible to the panel.
    from agent.burooj_status import record_design_check

    record_design_check(workspace, "visual_diff", result)

    return result


def _normalize_breakpoints(breakpoints: Optional[list[int]]) -> tuple[int, ...]:
    """Validate and deduplicate the breakpoint list.

    A caller-supplied list must be ints and widths in 1..65535. Anything
    invalid falls back to the default matrix: a typo'd matrix must not
    silently shrink the coverage.
    """
    if not breakpoints:
        return DEFAULT_BREAKPOINTS
    cleaned: list[int] = []
    for bp in breakpoints:
        if isinstance(bp, bool) or not isinstance(bp, int) or not 1 <= bp <= 65535:
            return DEFAULT_BREAKPOINTS
        if bp not in cleaned:
            cleaned.append(bp)
    return tuple(sorted(cleaned))


def _normalize_themes(themes: Optional[list[str]]) -> tuple[str, ...]:
    """Validate the theme list against the supported schemes.

    Playwright's ``color_scheme`` accepts "light" and "dark". Anything else
    is a caller error; falling back to the default matrix keeps the gate
    from checking fewer cells than it should. Output keeps the canonical
    light-then-dark order regardless of input order.
    """
    if not themes:
        return DEFAULT_THEMES
    cleaned: list[str] = []
    for theme in DEFAULT_THEMES:
        if theme in themes and theme not in cleaned:
            cleaned.append(theme)
    if not cleaned or len(cleaned) != len(set(themes)):
        return DEFAULT_THEMES
    return tuple(cleaned)


def _visual_diff_impl(
    workspace: Path,
    routes: Optional[list[str]],
    threshold: float,
    update_baselines: bool,
    breakpoints: tuple[int, ...],
    themes: tuple[str, ...],
) -> dict[str, Any]:
    """Shared visual diff implementation; ``visual_diff`` records the result."""
    try:
        session = get_session(workspace)
    except ManifestError as exc:
        return {"routes": [], "passed": False, "status": "skip", "reason": str(exc)}

    manifest = session.manifest
    if manifest.dev is None:
        return {
            "routes": [],
            "passed": True,
            "status": "skip",
            "reason": "no 'dev' section in burooj.build.json, nothing to screenshot",
        }
    if routes is None:
        routes = manifest.routes

    capture = run_async(
        _capture_screenshots(session, routes, breakpoints, themes),
        timeout=_CAPTURE_TIMEOUT,
    )
    if capture.get("error"):
        error = capture["error"]
        # Playwright simply not being installed is a legitimate skip. Anything
        # else means the check could not run, which is an error, not a pass.
        is_missing_dep = error == PLAYWRIGHT_MISSING
        return {
            "routes": [],
            "passed": is_missing_dep,
            "status": "skip" if is_missing_dep else "error",
            ("reason" if is_missing_dep else "error"): error,
        }
    screenshots = capture["screenshots"]

    # Ensure baselines directory exists.
    baselines_dir = workspace / "burooj.design" / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)

    results: list[RouteDiff] = []
    all_passed = True
    errors: list[str] = []

    for route_path in routes:
        for breakpoint in breakpoints:
            for theme in themes:
                current_bytes = screenshots.get((route_path, breakpoint, theme))
                baseline_file = _baseline_path(workspace, route_path, breakpoint, theme)
                cell = dict(breakpoint=breakpoint, theme=theme)

                if current_bytes is None:
                    results.append(RouteDiff(
                        path=route_path, baseline="", diff_pct=100.0, threshold=threshold,
                        passed=False, new_baseline=False,
                        note="could not capture a screenshot for this cell", **cell,
                    ))
                    all_passed = False
                    continue

                # Explicit re-baseline, or the genuine first run for this
                # cell. A missing baseline for one cell saves that cell and
                # never fails the others: each cell carries its own history.
                if update_baselines or not baseline_file.exists():
                    baseline_file.write_bytes(current_bytes)
                    results.append(RouteDiff(
                        path=route_path, baseline=str(baseline_file), diff_pct=0.0,
                        threshold=threshold, passed=True, new_baseline=True,
                        note=(
                            "baseline updated" if update_baselines
                            else "first run, baseline saved"
                        ),
                        **cell,
                    ))
                    continue

                # Compare against the baseline. A baseline that cannot be
                # decoded is reported, never silently overwritten:
                # auto-rebaselining on a decode failure quietly promoted a
                # corrupt or broken screenshot to the new truth, and every
                # later run then passed against it.
                try:
                    _, _, baseline_pixels = _decode_png_pixels(baseline_file.read_bytes())
                except (PngDecodeError, OSError) as exc:
                    errors.append(f"{route_path}@{breakpoint}/{theme}: unreadable baseline ({exc})")
                    all_passed = False
                    results.append(RouteDiff(
                        path=route_path, baseline=str(baseline_file), diff_pct=0.0,
                        threshold=threshold, passed=False, new_baseline=False,
                        note=(
                            f"baseline could not be decoded ({exc}). Delete it or "
                            f"re-run with update_baselines to accept the current "
                            f"screenshot."
                        ),
                        **cell,
                    ))
                    continue

                try:
                    _, _, current_pixels = _decode_png_pixels(current_bytes)
                except PngDecodeError as exc:
                    errors.append(f"{route_path}@{breakpoint}/{theme}: undecodable screenshot ({exc})")
                    all_passed = False
                    results.append(RouteDiff(
                        path=route_path, baseline=str(baseline_file), diff_pct=0.0,
                        threshold=threshold, passed=False, new_baseline=False,
                        note=f"screenshot could not be decoded ({exc})",
                        **cell,
                    ))
                    continue

                diff_pct = _compare_pixels(baseline_pixels, current_pixels)
                passed = diff_pct <= threshold
                if not passed:
                    all_passed = False

                results.append(RouteDiff(
                    path=route_path, baseline=str(baseline_file), diff_pct=diff_pct,
                    threshold=threshold, passed=passed, new_baseline=False,
                    note=(
                        "size changed, treated as fully different"
                        if diff_pct == 100.0 and len(baseline_pixels) != len(current_pixels)
                        else ""
                    ),
                    **cell,
                ))

    new_count = sum(1 for r in results if r.new_baseline)
    drifted = sum(1 for r in results if not r.passed)
    if drifted:
        # The old summary read "N cell(s) within threshold" whenever no
        # baseline was new, so a failing result carried a summary asserting
        # everything was fine. The desktop panel reads this string.
        summary = f"{drifted} of {len(results)} cell(s) drifted"
    elif new_count:
        summary = f"{len(results)} cell(s), {new_count} new baseline(s)"
    else:
        summary = f"{len(results)} cell(s) within threshold"

    result: dict[str, Any] = {
        "routes": [r.to_dict() for r in results],
        "passed": all_passed,
        "status": "error" if errors else ("pass" if all_passed else "fail"),
        "matrix": {
            "breakpoints": list(breakpoints),
            "themes": list(themes),
        },
        "summary": summary,
    }
    if errors:
        result["error"] = "; ".join(errors)
    return result


# ── Tool registration ───────────────────────────────────────────────────────

VISUAL_DIFF_SCHEMA = {
    "name": "visual_diff",
    "description": (
        "Screenshot every declared route across the capture matrix "
        "(breakpoints x themes) and compare against the baselines in "
        "burooj.design/baselines/. Each cell (route, breakpoint, theme) has "
        "its own baseline file. Answers 'did I break something', not 'is "
        "this good'. Rung 4 of the design gate. Pass update_baselines=true "
        "only when the visual change is intended."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "routes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Routes to diff. Omit to use the manifest's routes.",
            },
            "breakpoints": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Viewport widths to capture. Defaults to 390, 768, 1280. "
                    "Use a single width to check one breakpoint fast."
                ),
            },
            "themes": {
                "type": "array",
                "items": {"type": "string", "enum": ["light", "dark"]},
                "description": "Color schemes to capture. Defaults to light and dark.",
            },
            "update_baselines": {
                "type": "boolean",
                "description": (
                    "Accept the current screenshots as the new baselines. Use "
                    "after an intentional visual change, never to clear a "
                    "failure you have not looked at."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


def handle_visual_diff(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for visual_diff."""
    routes = args.get("routes")
    if routes is not None and (
        not isinstance(routes, list) or not all(isinstance(r, str) for r in routes)
    ):
        return json.dumps({"error": "'routes' must be an array of strings."})
    try:
        result = visual_diff(
            routes=routes,
            update_baselines=bool(args.get("update_baselines", False)),
            breakpoints=args.get("breakpoints"),
            themes=args.get("themes"),
        )
    except Exception as exc:
        logger.exception("visual_diff failed")
        return json.dumps(
            {"error": f"visual_diff crashed: {type(exc).__name__}: {exc}", "passed": False}
        )
    return json.dumps(result, indent=2)


registry.register(
    name="visual_diff",
    toolset="burooj_design",
    schema=VISUAL_DIFF_SCHEMA,
    handler=handle_visual_diff,
    emoji="🖼️",
)
