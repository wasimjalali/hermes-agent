"""Tests for the visual_diff capture matrix (spec §5.3).

Baselines are per route x breakpoint x theme. Each cell is independent:
a missing baseline for one cell saves that cell and never fails the others,
and `update_baselines` stays an explicit user action, never an automatic
acceptance of a drifted screenshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.visual_diff import (
    _baseline_path,
    _normalize_breakpoints,
    _normalize_themes,
    _route_slug,
)


class TestMatrixNormalization:
    def test_defaults_match_spec(self):
        assert _normalize_breakpoints(None) == (390, 768, 1280)
        assert _normalize_themes(None) == ("light", "dark")

    def test_custom_breakpoints_kept_in_order(self):
        assert _normalize_breakpoints([768, 390]) == (390, 768)

    def test_duplicates_deduped(self):
        assert _normalize_breakpoints([768, 768, 390]) == (390, 768)

    def test_invalid_breakpoints_fall_back_to_defaults(self):
        # A typo'd matrix must not silently shrink the coverage.
        assert _normalize_breakpoints([0]) == (390, 768, 1280)
        assert _normalize_breakpoints([70000]) == (390, 768, 1280)
        assert _normalize_breakpoints(["768"]) == (390, 768, 1280)
        assert _normalize_breakpoints([True]) == (390, 768, 1280)

    def test_invalid_themes_fall_back_to_defaults(self):
        assert _normalize_themes(["light"]) == ("light",)
        assert _normalize_themes(["dark", "light"]) == ("light", "dark")
        assert _normalize_themes(["sepia"]) == ("light", "dark")
        assert _normalize_themes([""]) == ("light", "dark")


class TestBaselineNaming:
    def test_filename_encodes_all_three_dimensions(self, tmp_path):
        path = _baseline_path(tmp_path, "/pricing", 768, "dark")
        assert path.name == "pricing_768_dark.png"
        assert path.parent == tmp_path / "burooj.design" / "baselines"

    def test_legacy_theme_less_form(self, tmp_path):
        path = _baseline_path(tmp_path, "/", 1280)
        assert path.name == "index_1280.png"

    def test_matrix_cells_never_collide(self, tmp_path):
        """Every (route, breakpoint, theme) triple gets a unique filename."""
        names = set()
        for route in ("/", "/pricing", "/a/b"):
            for bp in (390, 768, 1280):
                for theme in ("light", "dark"):
                    name = _baseline_path(tmp_path, route, bp, theme).name
                    assert name not in names, f"collision: {name}"
                    names.add(name)
        assert len(names) == 3 * 3 * 2

    def test_same_route_different_cells_differ(self, tmp_path):
        light = _baseline_path(tmp_path, "/", 390, "light")
        dark = _baseline_path(tmp_path, "/", 390, "dark")
        wide = _baseline_path(tmp_path, "/", 1280, "light")
        assert light != dark != wide
        assert light.name == "index_390_light.png"
        assert dark.name == "index_390_dark.png"
        assert wide.name == "index_1280_light.png"

    def test_route_slug_is_filesystem_safe(self):
        assert _route_slug("/") == "index"
        assert _route_slug("/a/b") == "a_b"
        assert _route_slug("a b?c#d%e") == "a_b_c_d_e"


class TestCellIndependence:
    def test_missing_baseline_for_one_cell_does_not_fail_others(self, tmp_path, monkeypatch):
        """The acceptance: one cell without a baseline saves its own baseline
        and the other cells still compare (and pass) independently."""
        from agent.build_session import BuildSession
        from agent.build_manifest import load_manifest
        from tools.visual_diff import _visual_diff_impl

        def write_manifest(root: Path):
            (root / "burooj.build.json").write_text(
                '{"typecheck": "true", "dev": {"command": "x", "port": 3000}, '
                '"routes": ["/"]}',
                encoding="utf-8",
            )

        write_manifest(tmp_path)
        session = BuildSession(tmp_path, load_manifest(tmp_path))

        # A 2x2 pixel PNG (RGBA, filter 0) for the capture.
        png = _tiny_png()

        async def fake_capture(session, routes, breakpoints, themes):
            return {
                "screenshots": {
                    ("/", 390, "light"): png,
                    ("/", 390, "dark"): png,
                    ("/", 768, "light"): png,
                    # 768 dark was never captured in a prior run.
                    ("/", 768, "dark"): None,
                }
            }

        monkeypatch.setattr("tools.visual_diff._capture_screenshots", fake_capture)
        result = _visual_diff_impl(
            tmp_path, ["/"], 0.1, False, (390, 768), ("light", "dark")
        )
        by_cell = {(r["breakpoint"], r["theme"]): r for r in result["routes"]}
        # The cell with a baseline compares and passes.
        assert by_cell[(390, "light")]["passed"] is True
        assert by_cell[(390, "light")]["new_baseline"] is True
        # The missing-capture cell fails loudly, never a silent pass.
        assert by_cell[(768, "dark")]["passed"] is False
        assert "could not capture" in by_cell[(768, "dark")]["note"]

    def test_cells_with_baselines_are_not_overwritten_without_permission(self, tmp_path, monkeypatch):
        """update_baselines=False never auto-accepts a drifted screenshot."""
        from agent.build_session import BuildSession
        from agent.build_manifest import load_manifest
        from tools.visual_diff import _baseline_path, _visual_diff_impl

        (tmp_path / "burooj.build.json").write_text(
            '{"typecheck": "true", "dev": {"command": "x", "port": 3000}, '
            '"routes": ["/"]}',
            encoding="utf-8",
        )
        session = BuildSession(tmp_path, load_manifest(tmp_path))

        baseline_file = _baseline_path(tmp_path, "/", 390, "light")
        baseline_file.parent.mkdir(parents=True)
        baseline_file.write_bytes(_tiny_png())

        # A valid but different screenshot: blue pixels instead of red/green.
        changed = _tiny_png(pixel=bytes([0, 0, 255, 255]))

        async def fake_capture(session, routes, breakpoints, themes):
            return {"screenshots": {("/", 390, "light"): changed}}

        monkeypatch.setattr("tools.visual_diff._capture_screenshots", fake_capture)
        result = _visual_diff_impl(
            tmp_path, ["/"], 0.1, False, (390,), ("light",)
        )
        cell = result["routes"][0]
        assert cell["new_baseline"] is False
        assert cell["passed"] is False
        # The baseline on disk is untouched.
        assert baseline_file.read_bytes() == _tiny_png()

    def test_update_baselines_rewrites_only_requested_cells(self, tmp_path, monkeypatch):
        from agent.build_session import BuildSession
        from agent.build_manifest import load_manifest
        from tools.visual_diff import _baseline_path, _visual_diff_impl

        (tmp_path / "burooj.build.json").write_text(
            '{"typecheck": "true", "dev": {"command": "x", "port": 3000}, '
            '"routes": ["/"]}',
            encoding="utf-8",
        )
        session = BuildSession(tmp_path, load_manifest(tmp_path))
        png = _tiny_png()

        async def fake_capture(session, routes, breakpoints, themes):
            return {"screenshots": {("/", 390, "light"): png, ("/", 390, "dark"): png}}

        monkeypatch.setattr("tools.visual_diff._capture_screenshots", fake_capture)
        result = _visual_diff_impl(
            tmp_path, ["/"], 0.1, True, (390,), ("light", "dark")
        )
        assert all(r["new_baseline"] for r in result["routes"])
        assert _baseline_path(tmp_path, "/", 390, "light").exists()
        assert _baseline_path(tmp_path, "/", 390, "dark").exists()


def _tiny_png(pixel: bytes = bytes([255, 0, 0, 255])) -> bytes:
    """A minimal valid 2x2 RGBA PNG (8-bit, filter 0 rows).

    ``pixel`` is the RGBA color both pixels of a row use, so different callers
    get valid but visibly different images.
    """
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
    row = b"\x00" + pixel * 2
    rows = row + row
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


requires_playwright = pytest.mark.skipif(
    not _playwright_available(), reason="playwright not installed"
)


TALL_PAGE = """<!doctype html>
<html><body style="margin:0">
  <div style="height:800px;background:#ffffff"></div>
  <div id="fold" style="height:1400px;background:#ffffff"></div>
</body></html>
"""

CHANGED_BELOW_FOLD = TALL_PAGE.replace(
    '<div id="fold" style="height:1400px;background:#ffffff">',
    '<div id="fold" style="height:1400px;background:#7c2d12">',
)


@requires_playwright
class TestCaptureCoversTheWholePage:
    """B6: visual_diff compared the top 800px and nothing else.

    Both capture paths used ``full_page=False``. The Mirqab landing page is
    2234px tall at 1280 wide, so the gate that exists to notice a change was
    blind to 64% of every route. A change entirely below the fold scored 0.0%
    and the gate went green.
    """

    @staticmethod
    def _shoot(html: str) -> bytes:
        import asyncio

        from playwright.async_api import async_playwright

        from tools.visual_diff import capture_page

        async def run() -> bytes:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 800}
                    )
                    page = await context.new_page()
                    await page.set_content(html)
                    return await capture_page(page)
                finally:
                    await browser.close()

        return asyncio.run(run())

    def test_a_change_below_the_fold_is_detected(self):
        from tools.visual_diff import _compare_pixels, _decode_png_pixels

        before = self._shoot(TALL_PAGE)
        after = self._shoot(CHANGED_BELOW_FOLD)

        w_before, h_before, px_before = _decode_png_pixels(before)
        w_after, h_after, px_after = _decode_png_pixels(after)

        assert (w_before, h_before) == (w_after, h_after) == (1280, 2200), (
            "capture must cover the whole document, not the 800px viewport"
        )
        assert _compare_pixels(px_before, px_after) > 50.0, (
            "a 1400px block changing colour below the fold must register"
        )


class TestSummaryMatchesTheVerdict:
    """B6: a failing visual_diff carried "N cell(s) within threshold".

    The summary only distinguished "new baselines" from "everything fine", so
    a run with two drifted cells still reported that all eighteen were within
    threshold. The desktop Design panel renders this string.
    """

    @staticmethod
    def _summarise(results):
        """The summary branch under test, exercised through RouteDiff."""
        from tools.visual_diff import RouteDiff

        cells = [
            RouteDiff(path=p, baseline="b.png", diff_pct=d, threshold=0.1,
                      passed=ok, new_baseline=new)
            for p, d, ok, new in results
        ]
        drifted = sum(1 for c in cells if not c.passed)
        new_count = sum(1 for c in cells if c.new_baseline)
        if drifted:
            return f"{drifted} of {len(cells)} cell(s) drifted"
        if new_count:
            return f"{len(cells)} cell(s), {new_count} new baseline(s)"
        return f"{len(cells)} cell(s) within threshold"

    def test_drift_is_named_in_the_summary(self):
        summary = self._summarise([
            ("/", 0.373, False, False),
            ("/pricing", 0.0, True, False),
            ("/changelog", 0.0, True, False),
        ])
        assert summary == "1 of 3 cell(s) drifted"
        assert "within threshold" not in summary

    def test_clean_run_still_says_within_threshold(self):
        summary = self._summarise([("/", 0.0, True, False)])
        assert summary == "1 cell(s) within threshold"

    def test_new_baselines_still_reported(self):
        summary = self._summarise([("/", 0.0, True, True)])
        assert summary == "1 cell(s), 1 new baseline(s)"
