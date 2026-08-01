"""Tests for the Burooj Build harness (B2): manifest, workspace, runtime, verify.

Every test here corresponds to a defect found in review. The suite exists
because the B2/B3 tools shipped with no coverage at all, and each of the
high-severity findings was reachable in a single function call.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from agent.build_manifest import ManifestError, load_manifest
from agent.build_workspace import (
    WorkspaceBoundaryError,
    assert_within_workspace,
    is_within_workspace,
    resolve_workspace,
)


def write_manifest(root: Path, data: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "burooj.build.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── Manifest ────────────────────────────────────────────────────────────────


class TestManifest:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ManifestError, match="No burooj.build.json"):
            load_manifest(tmp_path)

    def test_invalid_json_raises(self, tmp_path):
        (tmp_path / "burooj.build.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ManifestError, match="Invalid JSON"):
            load_manifest(tmp_path)

    def test_commands_are_optional(self, tmp_path):
        """A project with no lint step must still produce a valid manifest.

        The first cut required install/typecheck/lint/build all as non-empty
        strings, so a project without one of them could not be verified at all.
        """
        write_manifest(tmp_path, {"typecheck": "tsc --noEmit"})
        manifest = load_manifest(tmp_path)
        assert manifest.typecheck == "tsc --noEmit"
        assert manifest.lint == ""
        assert manifest.test is None
        assert manifest.dev is None

    def test_empty_manifest_rejected(self, tmp_path):
        """A manifest that checks nothing would report green for any change."""
        write_manifest(tmp_path, {})
        with pytest.raises(ManifestError, match="declares no commands"):
            load_manifest(tmp_path)

    def test_wrong_type_is_an_error_not_an_omission(self, tmp_path):
        """A typo'd value must fail loudly, not read as 'no such step'."""
        write_manifest(tmp_path, {"typecheck": "tsc", "lint": 123})
        with pytest.raises(ManifestError, match="'lint' must be a string"):
            load_manifest(tmp_path)

    @pytest.mark.parametrize("port", [0, -1, 70000, "3000", 3.5])
    def test_bad_port_rejected(self, tmp_path, port):
        write_manifest(tmp_path, {"typecheck": "tsc", "dev": {"command": "x", "port": port}})
        with pytest.raises(ManifestError, match="dev.port"):
            load_manifest(tmp_path)

    def test_fix_and_guard_parsed(self, tmp_path):
        write_manifest(tmp_path, {
            "test": {"command": "npm test", "fix": ["a.test.ts"], "guard": ["b.test.ts"]},
        })
        manifest = load_manifest(tmp_path)
        assert manifest.test.fix == ["a.test.ts"]
        assert manifest.test.guard == ["b.test.ts"]

    def test_routes_default_and_validation(self, tmp_path):
        write_manifest(tmp_path, {"typecheck": "tsc"})
        assert load_manifest(tmp_path).routes == ["/"]
        write_manifest(tmp_path, {"typecheck": "tsc", "routes": [1, 2]})
        with pytest.raises(ManifestError, match="routes"):
            load_manifest(tmp_path)


# ── Workspace resolution and boundary ───────────────────────────────────────


class TestWorkspace:
    def test_explicit_cwd_with_manifest_wins_over_ancestor(self, tmp_path):
        """An ancestor manifest must never capture an explicitly pinned cwd.

        The manifest supplies shell commands that verify executes, so a
        burooj.build.json in a shared parent directory taking precedence was a
        command-execution hazard, not just a resolution quirk.
        """
        parent = tmp_path / "shared"
        project = parent / "project"
        write_manifest(parent, {"typecheck": "echo ATTACKER"})
        write_manifest(project, {"typecheck": "echo MINE"})
        assert resolve_workspace(project) == project.resolve()

    def test_ancestor_manifest_still_found_when_cwd_has_none(self, tmp_path):
        project = tmp_path / "project"
        sub = project / "src" / "components"
        sub.mkdir(parents=True)
        write_manifest(project, {"typecheck": "tsc"})
        assert resolve_workspace(sub) == project.resolve()

    def test_ancestor_walk_is_bounded(self, tmp_path):
        """A manifest far above the cwd must not be picked up."""
        top = tmp_path / "top"
        deep = top / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        write_manifest(top, {"typecheck": "echo TOO_FAR"})
        assert resolve_workspace(deep) != top.resolve()

    def test_git_root_fallback(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        sub = repo / "src"
        sub.mkdir()
        assert resolve_workspace(sub) == repo.resolve()

    def test_is_within_workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        assert is_within_workspace(ws / "src" / "a.ts", ws)
        assert is_within_workspace(ws, ws)
        assert not is_within_workspace(tmp_path / "other" / "a.ts", ws)

    def test_dotdot_escape_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        assert not is_within_workspace(ws / ".." / "evil.txt", ws)

    def test_symlink_escape_rejected(self, tmp_path):
        """resolve() follows links, so a link out of the workspace is caught."""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        link = ws / "link"
        link.symlink_to(outside)
        assert not is_within_workspace(link / "secret.txt", ws)

    def test_assert_within_workspace_raises(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(WorkspaceBoundaryError):
            assert_within_workspace(tmp_path / "nope.txt", ws, operation="write")


class TestWriteConfinement:
    """The boundary must actually be enforced through Hermes' write guard."""

    def test_pin_blocks_outside_writes_and_unpin_restores(self, tmp_path):
        from agent.build_workspace import reset_active_workspace, set_active_workspace
        from agent.file_safety import get_write_denied_error

        ws = tmp_path / "ws"
        ws.mkdir()
        outside = str(tmp_path / "outside.txt")

        assert get_write_denied_error(outside) is None  # unpinned: no change
        token = set_active_workspace(ws)
        try:
            assert get_write_denied_error(str(ws / "src" / "a.ts")) is None
            denial = get_write_denied_error(outside)
            assert denial and "outside the Build mode workspace" in denial
        finally:
            reset_active_workspace(token)
        assert get_write_denied_error(outside) is None


# ── Runtime seam ────────────────────────────────────────────────────────────


class TestLocalRuntime:
    def test_exec_success_and_failure(self, tmp_path):
        from agent.build_runtime import LocalRuntime

        rt = LocalRuntime()
        assert rt.exec("echo hello", tmp_path).stdout.strip() == "hello"
        assert rt.exec("exit 3", tmp_path).exit_code == 3
        assert rt.exec("echo hello", tmp_path).ok

    @pytest.mark.live_system_guard_bypass
    def test_timeout_reports_and_terminates(self, tmp_path):
        """A timed-out command must return promptly and be marked timed out.

        subprocess.run(shell=True, timeout=...) killed only the shell, leaving
        the real build/test process running and holding the workspace. The
        process-group teardown is exercised here; the orphan-reaping assertion
        lives in the manual check below because the test suite's live-system
        guard intercepts os.killpg.
        """
        import time

        from agent.build_runtime import LocalRuntime

        start = time.monotonic()
        result = LocalRuntime().exec("sleep 30", tmp_path, timeout=2)
        elapsed = time.monotonic() - start

        assert result.timed_out
        assert not result.ok
        assert elapsed < 15, f"timeout did not return promptly ({elapsed:.1f}s)"

    def test_snapshot_does_not_fabricate_an_id(self, tmp_path):
        """Returning a fake snapshot id told callers they had a restore point."""
        from agent.build_runtime import LocalRuntime

        try:
            snapshot_id = LocalRuntime().snapshot(tmp_path)
        except (NotImplementedError, RuntimeError):
            return  # refusing is the correct outcome
        assert not snapshot_id.startswith("local:")


# ── verify ladder ───────────────────────────────────────────────────────────


class TestVerify:
    def test_unknown_rung_is_an_error_not_a_silent_pass(self, tmp_path):
        """The worst possible failure for a gate: green having run nothing."""
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=["typcheck"])
        assert result["passed"] is False
        assert result["results"][0]["status"] == "error"
        assert "Unknown rung" in result["results"][0]["output"]

    def test_empty_rung_list_is_an_error(self, tmp_path):
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=[])
        assert result["passed"] is False

    def test_missing_manifest_fails(self, tmp_path):
        from tools.verify_tool import verify

        result = verify(workspace=tmp_path)
        assert result["passed"] is False
        assert result["results"][0]["status"] == "error"

    def test_absent_commands_skip_without_failing(self, tmp_path):
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=["typecheck", "lint", "fix", "guard"])
        by_name = {r["name"]: r for r in result["results"]}
        assert by_name["typecheck"]["status"] == "pass"
        assert by_name["lint"]["status"] == "skip"
        assert by_name["guard"]["status"] == "skip"
        assert result["passed"] is True

    def test_failure_stops_the_full_ladder(self, tmp_path):
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "false", "lint": "true", "build": "true"})
        result = verify(workspace=tmp_path)
        names = [r["name"] for r in result["results"]]
        assert result["passed"] is False
        assert "lint" not in names, "fail-fast should stop after typecheck"

    def test_explicit_selection_runs_every_named_rung(self, tmp_path):
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "false", "lint": "false"})
        result = verify(workspace=tmp_path, rungs=["typecheck", "lint"])
        assert len(result["results"]) == 2

    def test_selection_runs_in_ladder_order(self, tmp_path):
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "true", "lint": "true", "build": "true"})
        result = verify(workspace=tmp_path, rungs=["build", "typecheck", "lint"])
        assert [r["name"] for r in result["results"]] == ["typecheck", "lint", "build"]

    def test_failing_rung_carries_a_hint(self, tmp_path):
        from tools.verify_tool import verify

        write_manifest(tmp_path, {"typecheck": "false"})
        result = verify(workspace=tmp_path, rungs=["typecheck"])
        assert result["results"][0].get("hint")

    def test_first_call_discloses_the_commands(self, tmp_path):
        """The user must see which manifest was picked and what it will run."""
        from tools.verify_tool import _disclosed, verify

        _disclosed.discard(str(tmp_path.resolve()))
        write_manifest(tmp_path, {"typecheck": "echo hi"})
        result = verify(workspace=tmp_path, rungs=["typecheck"])
        assert "disclosure" in result
        assert result["disclosure"]["commands"]["typecheck"] == "echo hi"
        # Only once per workspace.
        assert "disclosure" not in verify(workspace=tmp_path, rungs=["typecheck"])

    def test_test_targets_are_shell_quoted(self):
        """An unquoted path with a space broke the command and spliced args."""
        from agent.build_manifest import BuildManifest, TestConfig
        from tools.verify_tool import _test_command

        manifest = BuildManifest(test=TestConfig(command="npm test"))
        assert "'my test.spec.ts'" in _test_command(manifest, ["my test.spec.ts"])


class TestServerLogHeuristic:
    """The render rung must not fail on a line that merely contains 'error'."""

    @pytest.mark.parametrize("line", [
        "webpack compiled with 0 errors",
        "✓ Compiled successfully",
        "info  - no errors found",
        "(node:1) DeprecationWarning: punycode is deprecated",
    ])
    def test_benign_lines_are_not_problems(self, line):
        from tools.verify_tool import _server_problems

        assert _server_problems([line]) == []

    @pytest.mark.parametrize("line", [
        "Error: listen EADDRINUSE: address already in use :::3000",
        "Cannot find module 'react'",
        "FATAL ERROR: JavaScript heap out of memory",
    ])
    def test_real_failures_are_caught(self, line):
        from tools.verify_tool import _server_problems

        assert _server_problems([line]) == [line]


# ── PNG decoding (visual_diff) ──────────────────────────────────────────────


def make_png(width: int, height: int, filter_type: int = 0, fill: int = 128) -> bytes:
    import struct

    raw = bytearray()
    for _y in range(height):
        raw.append(filter_type)
        raw += bytes([fill]) * (width * 4)
    idat = zlib.compress(bytes(raw), 1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class TestPngDecode:
    def test_roundtrip(self):
        from tools.visual_diff import _decode_png_pixels

        width, height, pixels = _decode_png_pixels(make_png(4, 3))
        assert (width, height) == (4, 3)
        assert len(pixels) == 4 * 3 * 3  # RGB triples, alpha dropped

    def test_identical_images_are_zero_percent(self):
        from tools.visual_diff import _compare_pixels, _decode_png_pixels

        _, _, a = _decode_png_pixels(make_png(8, 8, fill=100))
        _, _, b = _decode_png_pixels(make_png(8, 8, fill=100))
        assert _compare_pixels(a, b) == 0.0

    def test_tolerance_absorbs_antialiasing(self):
        from tools.visual_diff import _compare_pixels, _decode_png_pixels

        _, _, a = _decode_png_pixels(make_png(8, 8, fill=100))
        _, _, b = _decode_png_pixels(make_png(8, 8, fill=105))  # within tolerance
        _, _, c = _decode_png_pixels(make_png(8, 8, fill=200))  # well beyond
        assert _compare_pixels(a, b) == 0.0
        assert _compare_pixels(a, c) == 100.0

    def test_size_mismatch_is_fully_different(self):
        from tools.visual_diff import _compare_pixels, _decode_png_pixels

        _, _, a = _decode_png_pixels(make_png(8, 8))
        _, _, b = _decode_png_pixels(make_png(4, 4))
        assert _compare_pixels(a, b) == 100.0

    @pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
    def test_every_png_filter_decodes(self, filter_type):
        from tools.visual_diff import _decode_png_pixels

        width, height, pixels = _decode_png_pixels(make_png(6, 4, filter_type))
        assert (width, height) == (6, 4)
        assert len(pixels) == 6 * 4 * 3

    def test_truncated_png_raises_our_error_not_zlib_error(self):
        """zlib.error escaped the caller's except clause and crashed verify."""
        from tools.visual_diff import PngDecodeError, _decode_png_pixels

        png = make_png(16, 16)
        with pytest.raises(PngDecodeError):
            _decode_png_pixels(png[: len(png) // 2])

    def test_garbage_raises_our_error(self):
        from tools.visual_diff import PngDecodeError, _decode_png_pixels

        with pytest.raises(PngDecodeError):
            _decode_png_pixels(b"\x89PNG\r\n\x1a\n" + b"\xff" * 40)

    def test_not_a_png_raises_our_error(self):
        from tools.visual_diff import PngDecodeError, _decode_png_pixels

        with pytest.raises(PngDecodeError):
            _decode_png_pixels(b"definitely not a png")


class TestDevServerShutdown:
    """B6: verify() leaked a next-server past interpreter exit.

    ``stop_all_sessions`` existed but had no caller anywhere in the tree. Its
    only other mention was a comment in ``verify_tool.verify`` claiming it was
    "the shutdown path". So the first ladder run that reached the render rung
    left a dev server holding the port, and every later run in a fresh process
    failed the render rung with "Port N is already in use by a process this
    session did not start".
    """

    def test_stop_all_sessions_is_registered_with_atexit(self):
        import subprocess
        import sys

        # A fresh interpreter, with atexit.register wrapped so the import's
        # own registrations are recorded by name.
        probe = (
            "import atexit\n"
            "seen = []\n"
            "real = atexit.register\n"
            "def spy(fn, *a, **k):\n"
            "    seen.append(getattr(fn, '__qualname__', repr(fn)))\n"
            "    return real(fn, *a, **k)\n"
            "atexit.register = spy\n"
            "import agent.build_session  # noqa: F401\n"
            "atexit.register = real\n"
            "print('stop_all_sessions' in seen)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "True", (
            "agent.build_session must register stop_all_sessions with atexit, "
            "otherwise every dev server it starts outlives the process and "
            f"holds its port. stderr: {result.stderr}"
        )


class TestServerProblemClassifier:
    """B6: the render rung failed a healthy Next.js app on a warning line.

    ``_server_problems`` recognised a warning only when the line began with
    the literal words "warn", "info" or "debug". Next.js prefixes its log
    levels with glyphs instead, so the routine Turbopack cache warning below
    (which contains the word "error") was classified as a server crash and
    turned the render rung red on an app that served every route correctly.
    """

    TURBOPACK_WARNING = (
        "⚠ Turbopack's filesystem cache has been deleted because we "
        "previously detected an internal error in Turbopack. Builds or page "
        "loads may be slower as a result."
    )

    def test_glyph_prefixed_warning_is_not_a_server_problem(self):
        from tools.verify_tool import _server_problems

        assert _server_problems([self.TURBOPACK_WARNING]) == []

    def test_word_prefixed_warning_is_still_not_a_problem(self):
        from tools.verify_tool import _server_problems

        assert _server_problems(["warn  - an error occurred while probing"]) == []

    def test_glyph_prefixed_error_is_still_a_problem(self):
        """The fix must not swallow Next.js' error glyph along with the warning one."""
        from tools.verify_tool import _server_problems

        line = "⨯ Internal error: route handler threw"
        assert _server_problems([line]) == [line]

    def test_unprefixed_error_is_still_a_problem(self):
        from tools.verify_tool import _server_problems

        line = "Error: connect ECONNREFUSED 127.0.0.1:5432"
        assert _server_problems([line]) == [line]


class TestVisualDiffFailureSummary:
    """B6: six drifted cells printed as three identical lines saying "/".

    ``visual_diff`` captures a route x breakpoint x theme matrix so a
    responsive or theme regression is caught where it happens. The design
    gate's summary dropped the breakpoint and the theme, called cells
    "route(s)", and truncated at three with no note that it had.
    """

    @staticmethod
    def _cells():
        return {
            "routes": [
                {"path": "/", "breakpoint": 390, "theme": "light",
                 "diff_pct": 7.505, "threshold": 0.1, "passed": False},
                {"path": "/", "breakpoint": 390, "theme": "dark",
                 "diff_pct": 7.505, "threshold": 0.1, "passed": False},
                {"path": "/", "breakpoint": 1280, "theme": "light",
                 "diff_pct": 3.931, "threshold": 0.1, "passed": False},
                {"path": "/", "breakpoint": 1280, "theme": "dark",
                 "diff_pct": 3.931, "threshold": 0.1, "passed": False},
                {"path": "/pricing", "breakpoint": 390, "theme": "light",
                 "diff_pct": 1.2, "threshold": 0.1, "passed": False},
                {"path": "/pricing", "breakpoint": 1280, "theme": "light",
                 "diff_pct": 1.4, "threshold": 0.1, "passed": False},
                {"path": "/changelog", "breakpoint": 390, "theme": "light",
                 "diff_pct": 0.0, "threshold": 0.1, "passed": True},
            ]
        }

    def test_every_reported_cell_line_is_distinguishable(self):
        from tools.verify_tool import format_visual_diff_failure

        lines = format_visual_diff_failure(self._cells())
        cell_lines = [ln for ln in lines if ln.startswith("  ") and "more" not in ln]
        assert len(cell_lines) == len(set(cell_lines)), (
            f"cells must be individually identifiable, got: {cell_lines}"
        )

    def test_breakpoint_and_theme_appear_in_the_label(self):
        from tools.verify_tool import format_visual_diff_failure

        lines = format_visual_diff_failure(self._cells())
        assert any("@390px" in ln and "[dark]" in ln for ln in lines)

    def test_header_counts_cells_and_routes_separately(self):
        from tools.verify_tool import format_visual_diff_failure

        header = format_visual_diff_failure(self._cells())[0]
        assert "6 cell(s) drifted" in header
        assert "2 route(s)" in header

    def test_truncation_is_disclosed(self):
        from tools.verify_tool import format_visual_diff_failure

        lines = format_visual_diff_failure(self._cells(), limit=2)
        assert lines[-1] == "  ... and 4 more"
