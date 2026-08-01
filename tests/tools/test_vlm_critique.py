"""Tests for vlm_critique - the advisory vision-model design rung (spec §5.5).

The contract: the critique reports, it does not decide. A failing, erroring
or crashing vision call must leave the design gate passing, because a gate
that fails differently on identical input is worse than no gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.vlm_critique import _parse_vision_response, vlm_critique
from tools.verify_tool import _run_rung_vlm_advisory


def write_manifest(root: Path, data: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "burooj.build.json").write_text(json.dumps(data), encoding="utf-8")


class TestParseVisionResponse:
    def test_plain_json(self):
        raw = '{"issues": [{"severity": "major", "point": "low contrast", "suggestion": "darken"}]}'
        parsed = _parse_vision_response(raw)
        assert parsed["issues"][0]["point"] == "low contrast"
        assert parsed["issues"][0]["severity"] == "major"

    def test_json_wrapped_in_prose(self):
        raw = 'Here you go:\n```json\n{"issues": [{"severity": "minor", "point": "padding", "suggestion": "add space"}]}\n```'
        parsed = _parse_vision_response(raw)
        assert parsed["issues"][0]["suggestion"] == "add space"

    def test_empty_issues(self):
        assert _parse_vision_response('{"issues": []}')["issues"] == []

    def test_off_contract_severity_is_labelled_not_rewritten(self):
        """Was: any unknown severity became "minor".

        Filing "catastrophic" as "minor" buries the strongest signal the
        critique produced. Keep the model's word and mark it off-contract.
        """
        raw = '{"issues": [{"severity": "catastrophic", "point": "x", "suggestion": "y"}]}'
        parsed = _parse_vision_response(raw)
        severity = parsed["issues"][0]["severity"]
        assert "catastrophic" in severity
        assert "off-contract" in severity

    def test_unparseable_returns_empty(self):
        assert _parse_vision_response("the page looks fine") == {}
        assert _parse_vision_response("") == {}
        assert _parse_vision_response('{"issues": "nope"}') == {}

    def test_issue_without_point_dropped(self):
        raw = '{"issues": [{"severity": "major", "suggestion": "y"}, {"severity": "info", "point": "p", "suggestion": "s"}]}'
        parsed = _parse_vision_response(raw)
        assert len(parsed["issues"]) == 1
        assert parsed["issues"][0]["point"] == "p"

    def test_real_vision_tool_envelope_unwraps_analysis(self):
        """H2: vision_analyze_tool returns {success, analysis}, not bare issues JSON."""
        analysis = json.dumps({
            "issues": [
                {"severity": "major", "point": "low contrast", "suggestion": "darken"},
            ]
        })
        envelope = json.dumps({"success": True, "analysis": analysis})
        parsed = _parse_vision_response(envelope)
        assert parsed["issues"][0]["point"] == "low contrast"

    def test_real_vision_tool_envelope_with_fenced_analysis(self):
        analysis = (
            "Sure:\n```json\n"
            '{"issues": [{"severity": "minor", "point": "gap", "suggestion": "tighten"}]}\n'
            "```"
        )
        envelope = json.dumps({"success": True, "analysis": analysis})
        parsed = _parse_vision_response(envelope)
        assert parsed["issues"][0]["point"] == "gap"

    def test_vision_tool_failure_envelope_raises(self):
        envelope = json.dumps({"success": False, "error": "provider down"})
        with pytest.raises(ValueError, match="provider down|Vision"):
            _parse_vision_response(envelope)


class TestVlmCritiqueNeverBlocks:
    def test_skip_when_no_manifest(self, tmp_path):
        result = vlm_critique(workspace=tmp_path)
        assert result["status"] == "skip"
        assert result["advisory"] is True

    def test_skip_when_no_dev_server_declared(self, tmp_path):
        write_manifest(tmp_path, {"typecheck": "true"})
        result = vlm_critique(workspace=tmp_path)
        assert result["status"] == "skip"
        assert "dev" in result["reason"]
        assert result["advisory"] is True

    def test_gate_stays_green_when_critique_errors(self, tmp_path, monkeypatch):
        """The acceptance: a failing critique leaves the design gate passing."""
        from agent.build_session import BuildSession
        from agent.build_manifest import load_manifest

        def exploding_critique(workspace=None, routes=None):
            raise RuntimeError("vision provider down")

        monkeypatch.setattr("tools.vlm_critique.vlm_critique", exploding_critique)

        write_manifest(tmp_path, {"typecheck": "true"})
        session = BuildSession(tmp_path, load_manifest(tmp_path))
        lines = _run_rung_vlm_advisory(session)
        joined = "\n".join(lines)
        assert "ADVISORY" in joined
        assert "vision provider down" in joined

    def test_gate_stays_green_when_critique_reports_issues(self, tmp_path, monkeypatch):
        """Even a critique full of issues is advisory, never a failure."""
        from agent.build_session import BuildSession
        from agent.build_manifest import load_manifest

        def critical_critique(workspace=None, routes=None):
            return {
                "status": "advisory",
                "advisory": True,
                "routes": [
                    {
                        "path": "/",
                        "issues": [
                            {"severity": "major", "point": "bad spacing", "suggestion": "fix"},
                        ],
                    }
                ],
            }

        monkeypatch.setattr("tools.vlm_critique.vlm_critique", critical_critique)

        write_manifest(tmp_path, {"typecheck": "true"})
        session = BuildSession(tmp_path, load_manifest(tmp_path))
        lines = _run_rung_vlm_advisory(session)
        joined = "\n".join(lines)
        assert "ADVISORY" in joined
        assert "bad spacing" in joined
        # The advisory output must not carry a fail status: the caller maps
        # it into the design_gate rung's output, never its pass/fail.
        assert "FAIL" not in joined

    def test_advisory_label_is_explicit_in_output(self, tmp_path, monkeypatch):
        from agent.build_session import BuildSession
        from agent.build_manifest import load_manifest

        def skip_critique(workspace=None, routes=None):
            return {"status": "skip", "advisory": True, "reason": "no dev server", "routes": []}

        monkeypatch.setattr("tools.vlm_critique.vlm_critique", skip_critique)

        write_manifest(tmp_path, {"typecheck": "true"})
        session = BuildSession(tmp_path, load_manifest(tmp_path))
        lines = _run_rung_vlm_advisory(session)
        assert any("ADVISORY" in line for line in lines)


class TestVlmCritiqueLadder:
    def test_design_gate_passes_when_vlm_errors(self, tmp_path, monkeypatch):
        """End to end: the design_gate rung passes while the critique errors."""
        from tools.verify_tool import verify

        def exploding_critique(workspace=None, routes=None):
            raise RuntimeError("vision provider down")

        monkeypatch.setenv("BUROOJ_VLM_CRITIQUE", "1")
        monkeypatch.setattr("tools.vlm_critique.vlm_critique", exploding_critique)
        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=["design_gate"])
        assert result["passed"] is True
        output = result["results"][0]["output"]
        assert "vlm_critique: ADVISORY" in output

    def test_design_gate_passes_when_vlm_has_major_issues(self, tmp_path, monkeypatch):
        from tools.verify_tool import verify

        def critical_critique(workspace=None, routes=None):
            return {
                "status": "advisory",
                "advisory": True,
                "routes": [
                    {
                        "path": "/",
                        "issues": [
                            {"severity": "major", "point": "clashing colors", "suggestion": "use tokens"},
                        ],
                    }
                ],
            }

        monkeypatch.setenv("BUROOJ_VLM_CRITIQUE", "1")
        monkeypatch.setattr("tools.vlm_critique.vlm_critique", critical_critique)
        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=["design_gate"])
        assert result["passed"] is True
        output = result["results"][0]["output"]
        assert "clashing colors" in output

    def test_design_gate_skips_vlm_by_default(self, tmp_path, monkeypatch):
        """P1-1: full ladder must not call the vision API unless opted in."""
        from tools.verify_tool import verify

        called = {"n": 0}

        def tracking_critique(workspace=None, routes=None):
            called["n"] += 1
            return {"status": "advisory", "advisory": True, "routes": []}

        monkeypatch.delenv("BUROOJ_VLM_CRITIQUE", raising=False)
        monkeypatch.setattr("tools.vlm_critique.vlm_critique", tracking_critique)
        monkeypatch.setattr(
            "tools.verify_tool._vlm_critique_enabled",
            lambda: False,
        )
        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=["design_gate"])
        assert result["passed"] is True
        # The vision API is not called. The gate still says the rung is off,
        # because silence read the same as "ran and found nothing".
        assert called["n"] == 0
        output = result["results"][0]["output"]
        assert "vlm_critique: ADVISORY (off" in output


class TestSeverityHandling:
    def test_off_contract_severity_is_kept_not_downgraded(self):
        """A model reporting "critical" was filed as "minor".

        That is the wrong direction to guess in: it hid the strongest signal
        the critique had behind the weakest label.
        """
        raw = (
            '{"issues": [{"severity": "critical", "point": "text unreadable", '
            '"suggestion": "raise contrast"}]}'
        )
        issue = _parse_vision_response(raw)["issues"][0]
        assert "critical" in issue["severity"]
        assert issue["severity"] != "minor"

    @pytest.mark.parametrize("severity", ["info", "minor", "major", "MAJOR", " major "])
    def test_contract_severities_normalize(self, severity):
        raw = (
            '{"issues": [{"severity": "%s", "point": "p", "suggestion": "s"}]}'
            % severity
        )
        assert _parse_vision_response(raw)["issues"][0]["severity"] == severity.strip().lower()

    def test_prompt_enumerates_the_severities(self):
        from tools.vlm_critique import _CRITIQUE_PROMPT

        assert "info, minor, major" in _CRITIQUE_PROMPT


class TestDisabledRungIsVisible:
    def test_gate_says_when_the_critique_is_off(self, tmp_path, monkeypatch):
        """Emitting nothing looked the same as running and finding nothing."""
        from tools.verify_tool import verify

        monkeypatch.setenv("BUROOJ_VLM_CRITIQUE", "0")
        write_manifest(tmp_path, {"typecheck": "true"})
        result = verify(workspace=tmp_path, rungs=["design_gate"])
        output = result["results"][0]["output"]
        assert "vlm_critique: ADVISORY (off" in output
        assert "BUROOJ_VLM_CRITIQUE=1" in output


class TestCritiqueRoundTrip:
    """H3: parsed issues must survive RouteCritique.to_dict()."""

    def _route_with(self, raw: str):
        """Drive _critique_route's parse branch without a browser."""
        import asyncio

        from tools import vlm_critique as vc

        class FakePage:
            async def goto(self, *a, **k):
                return None

            async def screenshot(self, *a, **k):
                return None

        async def fake_vision(image_url, user_prompt, **kwargs):
            return raw

        import tools.vision_tools as vt

        original = getattr(vt, "vision_analyze_tool", None)
        vt.vision_analyze_tool = fake_vision
        try:
            return asyncio.run(
                vc._critique_route(FakePage(), "http://x", "/", Path("/tmp"))
            )
        finally:
            if original is not None:
                vt.vision_analyze_tool = original

    def test_issues_serialize_instead_of_raising(self):
        """A critique with one issue used to die with AttributeError.

        _parse_issues_object returns plain dicts; RouteCritique.to_dict calls
        .to_dict() on each. The crash landed outside _critique_route's try,
        so vlm_critique only ever succeeded on an empty critique.
        """
        inner = (
            '{"issues": [{"severity": "major", "point": "low contrast", '
            '"suggestion": "darken"}]}'
        )
        result = self._route_with(json.dumps({"success": True, "analysis": inner}))
        assert result.error == "", result.error
        data = result.to_dict()
        assert data["issues"] == [
            {"severity": "major", "point": "low contrast", "suggestion": "darken"}
        ]

    def test_unparseable_reply_is_an_error_not_a_clean_bill(self):
        """M1: prose instead of JSON is 'we do not know', not 'zero issues'."""
        result = self._route_with(
            json.dumps({"success": True, "analysis": "The page looks fine."})
        )
        assert result.issues == []
        assert "could not be parsed" in result.error

    def test_to_dict_survives_raw_dict_issues(self):
        """to_dict sits outside the try, so it must not assume the type."""
        from tools.vlm_critique import RouteCritique

        rc = RouteCritique(path="/")
        rc.issues = [{"severity": "minor", "point": "p", "suggestion": "s"}]
        assert rc.to_dict()["issues"] == [
            {"severity": "minor", "point": "p", "suggestion": "s"}
        ]

    def test_empty_issue_list_is_a_real_clean_result(self):
        result = self._route_with(
            json.dumps({"success": True, "analysis": '{"issues": []}'})
        )
        assert result.error == ""
        assert result.to_dict()["issues"] == []


class TestVlmCritiqueOptInCache:
    """The config read is cached on config.yaml's mtime, not loaded once."""

    def _reset_cache(self):
        import tools.verify_tool as vt

        vt._vlm_critique_cfg_key = None
        vt._vlm_critique_cfg_value = False

    def test_config_edit_takes_effect_without_restart(self, tmp_path, monkeypatch):
        """A 'load once' cache made `burooj.vlm_critique: true` look ignored."""
        import time

        from tools.verify_tool import _vlm_critique_enabled

        monkeypatch.delenv("BUROOJ_VLM_CRITIQUE", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = tmp_path / "config.yaml"
        config.write_text("_config_version: 34\n", encoding="utf-8")
        self._reset_cache()

        assert _vlm_critique_enabled() is False

        time.sleep(0.01)
        config.write_text(
            "_config_version: 34\nburooj:\n  vlm_critique: true\n", encoding="utf-8"
        )
        assert _vlm_critique_enabled() is True

        time.sleep(0.01)
        config.write_text(
            "_config_version: 34\nburooj:\n  vlm_critique: false\n", encoding="utf-8"
        )
        assert _vlm_critique_enabled() is False

    def test_env_wins_over_cached_config(self, tmp_path, monkeypatch):
        from tools.verify_tool import _vlm_critique_enabled

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "_config_version: 34\nburooj:\n  vlm_critique: false\n", encoding="utf-8"
        )
        self._reset_cache()
        assert _vlm_critique_enabled() is False

        monkeypatch.setenv("BUROOJ_VLM_CRITIQUE", "1")
        assert _vlm_critique_enabled() is True


class TestBuroojConfigComment:
    """The commented burooj block lives in a Burooj-owned module."""

    def test_config_py_imports_the_shared_constant(self):
        import hermes_cli.config as config
        from hermes_cli.burooj_config import BUROOJ_COMMENT

        assert config._BUROOJ_COMMENT is BUROOJ_COMMENT
        assert "burooj:" in BUROOJ_COMMENT
        assert "model_hints" in BUROOJ_COMMENT
        assert "vlm_critique" in BUROOJ_COMMENT


class TestVlmCritiqueRegistration:
    def test_registered_in_design_toolset(self):
        from tools.registry import discover_builtin_tools, registry
        from toolsets import resolve_toolset

        discover_builtin_tools()
        assert "vlm_critique" in set(registry.get_all_tool_names())
        assert "vlm_critique" in set(resolve_toolset("burooj_design"))

    def test_schema_marks_advisory(self):
        from tools.vlm_critique import VLM_CRITIQUE_SCHEMA

        assert "ADVISORY" in VLM_CRITIQUE_SCHEMA["description"].upper()
