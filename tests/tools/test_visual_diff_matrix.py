"""Tests for the visual_diff capture matrix (spec §5.3).

Baselines are per route x breakpoint x theme. Each cell is independent:
a missing baseline for one cell saves that cell and never fails the others,
and `update_baselines` stays an explicit user action, never an automatic
acceptance of a drifted screenshot.
"""

from __future__ import annotations

from pathlib import Path

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
