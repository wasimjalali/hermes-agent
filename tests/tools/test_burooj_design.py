"""Tests for the Burooj design gate (B3) and Sanad tools (B4).

The headline test is ``test_shipped_default_tokens_pass`` /
``test_stock_next_scaffold_passes_with_shipped_ignores``: B3's own acceptance
criterion says a clean scaffold passes the design gate, and it did not. A gate
that no correct input can satisfy gets switched off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.contrast_check import ColorParseError, _contrast_ratio, contrast_check
from tools.design_lint import design_lint

HERMES_ROOT = Path(__file__).resolve().parents[2]
TOKENS_TEMPLATE = (
    HERMES_ROOT
    / "skills/software-development/scaffold-design-system/templates/tokens.json"
)
SCAFFOLD_TEMPLATES = HERMES_ROOT / "skills/software-development/scaffold-app/templates"


def make_workspace(tmp_path: Path, tokens: dict | None = None) -> Path:
    (tmp_path / "burooj.design").mkdir(parents=True, exist_ok=True)
    if tokens is not None:
        (tmp_path / "burooj.design" / "tokens.json").write_text(
            json.dumps(tokens), encoding="utf-8"
        )
    return tmp_path


# ── WCAG contrast math ──────────────────────────────────────────────────────


class TestContrastMath:
    @pytest.mark.parametrize("fg,bg,expected", [
        ("#000000", "#ffffff", 21.0),   # maximum possible
        ("#ffffff", "#ffffff", 1.0),    # identical
        ("#2563eb", "#ffffff", 5.17),   # tailwind blue-600 on white
        ("#0f172a", "#ffffff", 17.85),  # slate-900 on white
    ])
    def test_known_ratios(self, fg, bg, expected):
        assert _contrast_ratio(fg, bg) == pytest.approx(expected, abs=0.02)

    def test_ratio_is_symmetric(self):
        assert _contrast_ratio("#123456", "#abcdef") == pytest.approx(
            _contrast_ratio("#abcdef", "#123456")
        )

    @pytest.mark.parametrize("value", ["#fff", "#ffff", "#ffffff", "#ffffffff"])
    def test_hex_shorthands(self, value):
        assert _contrast_ratio(value, "#000000") == pytest.approx(21.0, abs=0.01)

    @pytest.mark.parametrize("value", ["#zzzzzz", "#12345", "oklch(bad)", "", "rebeccapurple"])
    def test_malformed_color_raises_instead_of_crashing(self, value):
        """A bad token value used to raise a bare ValueError out of the gate.

        It escaped contrast_check, escaped the design gate, and took down the
        whole verify ladder with a traceback.
        """
        with pytest.raises(ColorParseError):
            _contrast_ratio(value, "#ffffff")

    def test_oklch_is_understood(self):
        """Tailwind v4 and current shadcn emit oklch by default."""
        assert _contrast_ratio("oklch(1 0 0)", "oklch(0 0 0)") == pytest.approx(21.0, abs=0.1)

    def test_rgb_is_understood(self):
        assert _contrast_ratio("rgb(255 255 255)", "rgb(0, 0, 0)") == pytest.approx(21.0, abs=0.01)


# ── contrast_check ──────────────────────────────────────────────────────────


class TestContrastCheck:
    def test_shipped_default_tokens_pass(self, tmp_path):
        """B3 acceptance criterion 6: a clean scaffold passes the design gate.

        The shipped tokens.json failed its own gate on three pairs.
        """
        tokens = json.loads(TOKENS_TEMPLATE.read_text(encoding="utf-8"))
        result = contrast_check(workspace=make_workspace(tmp_path, tokens))
        failing = [p for p in result["pairs"] if not p["passed"]]
        assert result["status"] == "pass", f"default tokens fail contrast: {failing}"
        assert result["pairs"], "expected the default token names to be recognised"

    def test_missing_tokens_is_skip_not_pass(self, tmp_path):
        """Reporting PASS for a check that never ran is a false green."""
        result = contrast_check(workspace=tmp_path)
        assert result["status"] == "skip"

    def test_malformed_tokens_file_is_error(self, tmp_path):
        make_workspace(tmp_path)
        (tmp_path / "burooj.design" / "tokens.json").write_text("{bad", encoding="utf-8")
        result = contrast_check(workspace=tmp_path)
        assert result["status"] == "error"
        assert result["passed"] is False

    def test_bad_color_value_is_error_not_crash(self, tmp_path):
        ws = make_workspace(tmp_path, {
            "color": {
                "text": {"primary": {"$value": "#zzzzzz"}},
                "background": {"default": {"$value": "#ffffff"}},
            }
        })
        result = contrast_check(workspace=ws)
        assert result["status"] == "error"
        assert result["passed"] is False

    def test_dtcg_aliases_resolve(self, tmp_path):
        """An alias tree used to resolve to nothing and vacuously pass."""
        ws = make_workspace(tmp_path, {
            "color": {
                "brand": {"ink": {"$value": "#0f172a", "$type": "color"}},
                "paper": {"$value": "#ffffff", "$type": "color"},
                "text": {"primary": {"$value": "{color.brand.ink}", "$type": "color"}},
                "background": {"default": {"$value": "{color.paper}", "$type": "color"}},
            }
        })
        result = contrast_check(workspace=ws)
        pair = next(p for p in result["pairs"] if p["foreground"] == "color.text.primary")
        assert pair["ratio"] == pytest.approx(17.85, abs=0.05)

    def test_alias_cycle_does_not_hang(self, tmp_path):
        ws = make_workspace(tmp_path, {
            "color": {
                "a": {"$value": "{color.b}"},
                "b": {"$value": "{color.a}"},
                "text": {"primary": {"$value": "{color.a}"}},
                "background": {"default": {"$value": "#ffffff"}},
            }
        })
        result = contrast_check(workspace=ws)
        assert "color.text.primary" in result.get("unresolved_tokens", [])

    def test_renamed_tokens_are_reported_not_silently_skipped(self, tmp_path):
        """A renamed token means the gate quietly stopped checking something."""
        ws = make_workspace(tmp_path, {
            "color": {"totally": {"different": {"$value": "#000000"}}}
        })
        result = contrast_check(workspace=ws)
        assert result.get("unresolved_tokens")

    def test_failing_pair_fails_the_check(self, tmp_path):
        ws = make_workspace(tmp_path, {
            "color": {
                "text": {"primary": {"$value": "#cccccc"}},
                "background": {"default": {"$value": "#ffffff"}},
            }
        })
        result = contrast_check(workspace=ws)
        assert result["passed"] is False
        assert result["status"] == "fail"


# ── design_lint ─────────────────────────────────────────────────────────────


class TestDesignLint:
    def test_catches_each_rule(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.tsx").write_text(
            'export const A = () => <div style={{ padding: "12px", color: "#3366ff" }} '
            'className="bg-[#ff0000]">x</div>\n',
            encoding="utf-8",
        )
        result = design_lint(workspace=tmp_path)
        rules = {v["rule"] for v in result["violations"]}
        assert {"raw-hex", "arbitrary-tailwind", "raw-px"} <= rules
        assert result["passed"] is False
        # The hex inside bg-[#ff0000] is reported once, as arbitrary-tailwind.
        hexes = {v["value"] for v in result["violations"] if v["rule"] == "raw-hex"}
        assert hexes == {"#3366ff"}

    def test_grid_rows_arbitrary_value_is_caught(self, tmp_path):
        """The prefix list missed grid-rows, grid-cols, leading, tracking..."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.tsx").write_text(
            '<div className="grid-rows-[20px_1fr] leading-[1.7]">x</div>\n', encoding="utf-8"
        )
        values = {v["value"] for v in design_lint(workspace=tmp_path)["violations"]}
        assert "grid-rows-[20px_1fr]" in values
        assert "leading-[1.7]" in values

    def test_px_inside_tailwind_value_not_double_counted(self, tmp_path):
        """p-[13px] is one mistake, not two."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.tsx").write_text('<div className="p-[13px]">x</div>\n', encoding="utf-8")
        violations = design_lint(workspace=tmp_path)["violations"]
        assert len(violations) == 1
        assert violations[0]["rule"] == "arbitrary-tailwind"

    def test_design_system_files_are_exempt(self, tmp_path):
        make_workspace(tmp_path)
        (tmp_path / "burooj.design" / "tokens.css").write_text(
            ":root { --color-primary: #2563eb; }\n", encoding="utf-8"
        )
        assert design_lint(workspace=tmp_path)["passed"] is True

    def test_css_custom_property_definitions_are_exempt(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "globals.css").write_text(
            ":root {\n  --background: #ffffff;\n  --foreground: #171717;\n}\n",
            encoding="utf-8",
        )
        assert design_lint(workspace=tmp_path)["passed"] is True

    def test_disable_next_line_comment(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.tsx").write_text(
            "// burooj-design-lint-disable-next-line\n"
            '<div className="bg-[#ff0000]">x</div>\n',
            encoding="utf-8",
        )
        result = design_lint(workspace=tmp_path)
        assert result["passed"] is True
        assert result["suppressed"] == 1

    def test_disable_file_comment(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.tsx").write_text(
            "// burooj-design-lint-disable-file\n"
            '<div className="bg-[#ff0000]" style={{ padding: "9px" }}>x</div>\n',
            encoding="utf-8",
        )
        assert design_lint(workspace=tmp_path)["passed"] is True

    def test_lint_ignore_config(self, tmp_path):
        make_workspace(tmp_path)
        src = tmp_path / "src" / "generated"
        src.mkdir(parents=True)
        (src / "a.tsx").write_text('<div className="bg-[#ff0000]" />\n', encoding="utf-8")
        (tmp_path / "burooj.design" / "lint-ignore.json").write_text(
            json.dumps({"paths": ["src/generated/**"]}), encoding="utf-8"
        )
        assert design_lint(workspace=tmp_path)["passed"] is True

    def test_malformed_ignore_config_does_not_disable_the_gate(self, tmp_path):
        """A broken exemption file must not silently exempt everything."""
        make_workspace(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.tsx").write_text('<div className="bg-[#ff0000]" />\n', encoding="utf-8")
        (tmp_path / "burooj.design" / "lint-ignore.json").write_text("{bad", encoding="utf-8")
        assert design_lint(workspace=tmp_path)["passed"] is False

    def test_stock_next_scaffold_passes_with_shipped_ignores(self, tmp_path):
        """B3 acceptance criterion 6, the design_lint half.

        A stock create-next-app page.tsx ships bg-[#383838], gap-[32px] and raw
        hex. The scaffold skill therefore ships a lint-ignore.json covering it;
        without that pairing the gate fails on an untouched scaffold.
        """
        make_workspace(tmp_path)
        app = tmp_path / "src" / "app"
        app.mkdir(parents=True)
        (app / "page.tsx").write_text(
            '<a className="bg-[#383838] gap-[32px] dark:hover:bg-[#ccc]">Deploy</a>\n',
            encoding="utf-8",
        )
        (tmp_path / "burooj.design" / "lint-ignore.json").write_text(
            (SCAFFOLD_TEMPLATES / "lint-ignore.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        result = design_lint(workspace=tmp_path)
        assert result["passed"] is True, result["violations"]


# ── Sanad tools (B4) ────────────────────────────────────────────────────────


class TestSanadTools:
    def test_chunk_id_is_url_quoted(self, monkeypatch):
        """A model-supplied id must not be able to walk the API path."""
        import tools.sanad_tools as st

        seen = {}

        class FakeResponse:
            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return FakeResponse()

        monkeypatch.setattr(st.urllib.request, "urlopen", fake_urlopen)
        st.handle_sanad_get_chunk({"chunk_id": "../../admin"})
        assert "../.." not in seen["url"]
        assert "%2F" in seen["url"] or "%2E" in seen["url"]

    @pytest.mark.parametrize("value", [None, "eight", -5, 999, True, 3.7])
    def test_top_k_never_raises(self, value):
        """min(args.get('top_k'), 24) raised TypeError on a string or null."""
        from tools.sanad_tools import _coerce_top_k

        result = _coerce_top_k(value)
        assert isinstance(result, int)
        assert 1 <= result <= 24

    @pytest.mark.parametrize("args", [{}, {"query": ""}, {"query": "   "}, {"query": 42}])
    def test_empty_or_wrong_type_query_is_rejected(self, args):
        from tools.sanad_tools import handle_sanad_search

        assert "error" in json.loads(handle_sanad_search(args))

    def test_principal_comes_from_the_environment(self, monkeypatch):
        """Hardcoding one principal made Sanad's ACL layer meaningless."""
        from tools.sanad_tools import _current_principal

        monkeypatch.setenv("SANAD_PRINCIPAL_USER_ID", "wasim")
        monkeypatch.setenv("SANAD_PRINCIPAL_ROLES", "admin, engineering")
        principal = _current_principal()
        assert principal["user_id"] == "wasim"
        assert principal["roles"] == ["admin", "engineering"]

    def test_principal_defaults_to_least_privilege(self, monkeypatch):
        """Under-fetching is a visible gap; over-fetching is a silent leak."""
        from tools.sanad_tools import _current_principal

        monkeypatch.delenv("SANAD_PRINCIPAL_USER_ID", raising=False)
        principal = _current_principal()
        assert principal["roles"] == []
        assert principal["user_id"] == "anonymous"

    def test_oversized_response_is_truncated(self):
        from tools.sanad_tools import _MAX_RESULT_CHARS, _truncate

        out = _truncate("x" * (_MAX_RESULT_CHARS * 2))
        assert len(out) < _MAX_RESULT_CHARS * 2
        assert "truncated" in out


# ── Registration smoke test ─────────────────────────────────────────────────


class TestToolRegistration:
    """The defect that made B2 and B3 undeliverable: nothing was registered."""

    def test_all_burooj_tools_are_registered(self):
        from tools.registry import discover_builtin_tools, registry

        discover_builtin_tools()
        names = set(registry.get_all_tool_names())
        expected = {
            "repo_map", "verify", "preview",
            "design_lint", "contrast_check", "a11y_check", "visual_diff",
            "sanad_search", "sanad_get_chunk",
        }
        assert expected <= names, f"unregistered: {sorted(expected - names)}"

    def test_build_toolset_exposes_the_build_harness(self):
        from toolsets import resolve_toolset

        tools = set(resolve_toolset("burooj_build"))
        assert {"repo_map", "verify", "preview"} <= tools

    def test_design_toolset_exposes_the_design_gate(self):
        from toolsets import resolve_toolset

        tools = set(resolve_toolset("burooj_design"))
        assert {"design_lint", "contrast_check", "a11y_check", "visual_diff"} <= tools

    def test_build_guidance_only_names_callable_tools(self):
        """BUILD_GUIDANCE told the model to call tools that did not exist."""
        import re

        from agent.burooj_profiles import BUILD_GUIDANCE
        from tools.registry import discover_builtin_tools, registry

        discover_builtin_tools()
        names = set(registry.get_all_tool_names())
        referenced = set(re.findall(r"`(\w+)\(\)`", BUILD_GUIDANCE))
        assert referenced, "expected BUILD_GUIDANCE to name some tools"
        assert referenced <= names, f"guidance names missing tools: {referenced - names}"
