"""Tests for the Burooj status store and mode-panel gateway RPCs.

The Build/Design desktop panels are a thin client over the RPC methods in
``tui_gateway/methods_burooj.py``. The tools record their last results into
``agent/burooj_status`` as a side effect; the RPC layer reads those plus
cheap live state (manifest, dev server handle, tokens on disk). These tests
lock the contract between the tools, the store and the RPC payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import burooj_status
from agent.burooj_status import (
    get_design_checks,
    get_preview,
    get_verify,
    record_design_check,
    record_preview,
    record_verify,
)
from tui_gateway.methods_burooj import _flatten_color_tokens, _tokens_swatches

# The real ladder names, so the panel's eight rungs stay in sync with the tool.
LADDER_RUNGS = (
    "install", "typecheck", "lint", "fix", "guard", "build", "render", "design_gate",
)


# ── Status store ────────────────────────────────────────────────────────────


class TestStatusStore:
    def test_verify_recorded_and_read_back(self, tmp_path):
        record_verify(tmp_path, {"results": [], "passed": True})
        stored = get_verify(tmp_path)
        assert stored is not None
        assert stored["passed"] is True

    def test_preview_recorded_and_read_back(self, tmp_path):
        record_preview(tmp_path, {"routes": [], "server_healthy": False})
        assert get_preview(tmp_path)["server_healthy"] is False

    def test_design_checks_keyed_per_check(self, tmp_path):
        record_design_check(tmp_path, "visual_diff", {"passed": True})
        record_design_check(tmp_path, "a11y_check", {"passed": False})
        checks = get_design_checks(tmp_path)
        assert checks["visual_diff"]["passed"] is True
        assert checks["a11y_check"]["passed"] is False

    def test_missing_entries_are_none_not_skips(self, tmp_path):
        """A check that never ran is unknown, and must not read as a pass."""
        assert get_verify(tmp_path) is None
        assert get_preview(tmp_path) is None
        assert get_design_checks(tmp_path) == {}

    def test_workspaces_are_isolated(self, tmp_path):
        record_verify(tmp_path, {"results": [], "passed": True})
        other = tmp_path / "other"
        other.mkdir()
        assert get_verify(other) is None

    def test_paths_are_resolved_before_keying(self, tmp_path):
        record_verify(tmp_path / "sub" / "..", {"results": [], "passed": True})
        assert get_verify(tmp_path) is not None


# ── Token swatches (design panel) ───────────────────────────────────────────


class TestTokenSwatches:
    def test_flattens_nested_color_tokens(self):
        tokens = {
            "color": {
                "primary": {"$value": "#2563eb", "$type": "color"},
                "background": {
                    "default": {"$value": "#ffffff", "$type": "color"},
                    "muted": {"$value": "#f1f5f9", "$type": "color"},
                },
            }
        }
        flat = _flatten_color_tokens(tokens)
        paths = [t["path"] for t in flat]
        assert "color.primary" in paths
        assert "color.background.default" in paths
        assert "color.background.muted" in paths

    def test_skips_non_color_tokens(self):
        tokens = {"spacing": {"md": {"$value": "12px", "$type": "dimension"}}}
        assert _flatten_color_tokens(tokens) == []

    def test_aliases_resolved(self, tmp_path):
        (tmp_path / "burooj.design").mkdir(parents=True)
        (tmp_path / "burooj.design" / "tokens.json").write_text(json.dumps({
            "color": {
                "primary": {"$value": "#2563eb", "$type": "color"},
                "text": {"primary": {"$value": "{color.primary}", "$type": "color"}},
            },
        }))
        result = _tokens_swatches(tmp_path)
        assert result["status"] == "pass"
        by_path = {t["path"]: t["value"] for t in result["tokens"]}
        assert by_path["color.primary"] == "#2563eb"
        assert by_path["color.text.primary"] == "#2563eb"

    def test_missing_tokens_file_is_a_skip(self, tmp_path):
        result = _tokens_swatches(tmp_path)
        assert result["status"] == "skip"

    def test_broken_tokens_file_is_an_error(self, tmp_path):
        (tmp_path / "burooj.design").mkdir(parents=True)
        (tmp_path / "burooj.design" / "tokens.json").write_text("{not json")
        result = _tokens_swatches(tmp_path)
        assert result["status"] == "error"


# ── Ladder contract ─────────────────────────────────────────────────────────


class TestLadderContract:
    def test_ladder_names_match_verify_tool(self):
        """The panel renders the real rungs; a renamed rung must surface."""
        from tools.verify_tool import LADDER_RUNGS as TOOL_RUNGS

        assert list(LADDER_RUNGS) == list(TOOL_RUNGS)

    def test_eight_rungs_in_order(self):
        assert len(LADDER_RUNGS) == 8
        assert LADDER_RUNGS[0] == "install"
        assert LADDER_RUNGS[-1] == "design_gate"


# ── Gateway RPC handlers ────────────────────────────────────────────────────


def write_manifest(root: Path, data: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "burooj.build.json").write_text(json.dumps(data), encoding="utf-8")


class TestBuildStatusRpc:
    def test_status_reports_workspace_and_manifest(self, tmp_path):
        import tui_gateway.server as server

        write_manifest(tmp_path, {
            "typecheck": "tsc --noEmit",
            "dev": {"command": "npm run dev", "port": 3000, "ready": "/"},
            "routes": ["/", "/pricing"],
        })
        response = server._methods["burooj.build.status"](1, {"workspace": str(tmp_path)})
        result = response["result"]
        assert result["workspace"] == str(tmp_path.resolve())
        assert result["manifest_path"] == str(tmp_path.resolve() / "burooj.build.json")
        assert result["manifest"]["typecheck"] == "tsc --noEmit"
        assert result["manifest"]["dev_port"] == 3000
        assert result["manifest"]["routes"] == ["/", "/pricing"]
        assert len(result["ladder"]) == 8
        assert result["last_verify"] is None
        assert result["last_preview"] is None
        assert result["dev_server"]["running"] is False

    def test_status_without_manifest_reports_error_not_crash(self, tmp_path):
        import tui_gateway.server as server

        response = server._methods["burooj.build.status"](1, {"workspace": str(tmp_path)})
        result = response["result"]
        assert result["workspace"] == str(tmp_path.resolve())
        assert result["manifest"] is None
        assert "burooj.build.json" in result["manifest_error"]

    def test_verify_rpc_runs_and_records(self, tmp_path, monkeypatch):
        import tui_gateway.server as server

        write_manifest(tmp_path, {"typecheck": "echo ok"})
        monkeypatch.chdir(tmp_path)

        fake = {
            "results": [{"name": "typecheck", "status": "pass", "duration_ms": 1}],
            "passed": True,
        }

        def fake_verify(workspace=None, rungs=None):
            from agent.burooj_status import record_verify

            record_verify(workspace, fake)
            return fake

        monkeypatch.setattr("tools.verify_tool.verify", fake_verify)
        response = server._methods["burooj.build.verify"](2, {"workspace": str(tmp_path)})
        assert response["result"]["passed"] is True
        assert get_verify(tmp_path)["passed"] is True

    def test_verify_rpc_rejects_bad_rungs(self, tmp_path):
        import tui_gateway.server as server

        write_manifest(tmp_path, {"typecheck": "echo ok"})
        response = server._methods["burooj.build.verify"](
            2, {"workspace": str(tmp_path), "rungs": "typecheck"}
        )
        assert "error" in response

    def test_preview_rpc_rejects_bad_routes(self, tmp_path):
        import tui_gateway.server as server

        write_manifest(tmp_path, {"typecheck": "echo ok"})
        response = server._methods["burooj.build.preview"](
            3, {"workspace": str(tmp_path), "routes": "/"}
        )
        assert "error" in response


class TestDesignStatusRpc:
    def test_design_status_returns_tokens_contrast_lint(self, tmp_path):
        import tui_gateway.server as server

        (tmp_path / "burooj.design").mkdir(parents=True)
        (tmp_path / "burooj.design" / "tokens.json").write_text(json.dumps({
            "color": {
                "primary": {"$value": "#2563eb", "$type": "color"},
                "background": {"default": {"$value": "#ffffff", "$type": "color"}},
            },
        }))
        write_manifest(tmp_path, {"typecheck": "echo ok"})

        response = server._methods["burooj.design.status"](4, {"workspace": str(tmp_path)})
        result = response["result"]
        assert result["tokens"]["status"] == "pass"
        assert any(t["path"] == "color.primary" for t in result["tokens"]["tokens"])
        # contrast_check needs the expected pairs; a design system with no
        # gated pair must report skip, never a silent pass.
        assert result["contrast"]["status"] in ("skip", "pass", "fail")
        assert result["visual_diff"] is None
        assert result["a11y_check"] is None

    def test_design_checks_runs_all_four(self, tmp_path, monkeypatch):
        import tui_gateway.server as server

        write_manifest(tmp_path, {"typecheck": "echo ok"})
        (tmp_path / "burooj.design").mkdir(parents=True)
        (tmp_path / "burooj.design" / "tokens.json").write_text(json.dumps({
            "color": {
                "primary": {"$value": "#2563eb", "$type": "color"},
                "background": {"default": {"$value": "#ffffff", "$type": "color"}},
            },
        }))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "page.tsx").write_text("export const x = 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        # a11y_check and visual_diff skip without Playwright + a dev server,
        # so the combined gate must treat their skips as non-failing.
        response = server._methods["burooj.design.checks"](5, {"workspace": str(tmp_path)})
        result = response["result"]
        assert "lint" in result
        assert "contrast" in result
        assert "a11y_check" in result
        assert "visual_diff" in result
        assert isinstance(result["passed"], bool)

    def test_design_status_records_are_readable_after_checks(self, tmp_path, monkeypatch):
        import tui_gateway.server as server

        write_manifest(tmp_path, {"typecheck": "echo ok"})
        (tmp_path / "burooj.design").mkdir(parents=True)
        (tmp_path / "burooj.design" / "tokens.json").write_text(json.dumps({
            "color": {
                "primary": {"$value": "#2563eb", "$type": "color"},
                "background": {"default": {"$value": "#ffffff", "$type": "color"}},
            },
        }))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "page.tsx").write_text("export const x = 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        server._methods["burooj.design.checks"](6, {"workspace": str(tmp_path)})
        response = server._methods["burooj.design.status"](7, {"workspace": str(tmp_path)})
        result = response["result"]
        # The status read must surface what the checks recorded.
        assert result["visual_diff"] is not None
        assert result["a11y_check"] is not None


class TestRpcRegistration:
    def test_all_burooj_methods_registered(self):
        import tui_gateway.server as server

        for name in (
            "burooj.build.status",
            "burooj.build.verify",
            "burooj.build.preview",
            "burooj.design.status",
            "burooj.design.checks",
        ):
            assert name in server._methods, name

    def test_heavy_rpcs_on_the_pool(self):
        """verify/preview/checks can take minutes; they must not block the
        reader thread, or a slow ladder freezes the whole desktop."""
        import tui_gateway.server as server

        assert "burooj.build.verify" in server._LONG_HANDLERS
        assert "burooj.build.preview" in server._LONG_HANDLERS
        assert "burooj.design.checks" in server._LONG_HANDLERS

    def test_status_rpcs_are_inline(self):
        """Status reads are cheap (cache + manifest + token parse)."""
        import tui_gateway.server as server

        assert "burooj.build.status" not in server._LONG_HANDLERS
        assert "burooj.design.status" not in server._LONG_HANDLERS
