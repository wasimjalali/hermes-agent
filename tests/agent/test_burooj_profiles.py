"""Tests for Burooj mode profiles (B0 framework)."""

from pathlib import Path

import pytest

from agent import coding_context as cc
from agent import burooj_profiles as bp
from agent.coding_context import ContextProfile, register_profile


class TestBuroojProfilesResolve:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("agent", bp.AGENT_PROFILE),
            ("sanad", bp.SANAD_PROFILE),
            ("build", bp.BUILD_PROFILE),
            ("design", bp.DESIGN_PROFILE),
        ],
    )
    def test_resolves_by_name(self, tmp_path, name, expected):
        mode = cc.resolve_runtime_mode(
            platform="desktop",
            cwd=tmp_path,
            config={"agent": {"coding_context": "auto"}},
            profile=name,
        )
        assert mode.pinned is True
        assert mode.profile.name == name
        assert mode.profile is cc.get_profile(name)
        assert mode.kind == name
        assert mode.profile.toolset == expected.toolset
        assert mode.profile.model_hint == expected.model_hint
        assert mode.profile.memory_policy == expected.memory_policy

    def test_four_distinct_guidance_strings(self, tmp_path):
        guidances = []
        for name in ("agent", "sanad", "build", "design"):
            mode = cc.resolve_runtime_mode(platform="desktop", cwd=tmp_path, profile=name)
            assert mode.profile.guidance
            guidances.append(mode.profile.guidance)
            # Guidance lands in system prompt parts for every Burooj mode.
            prefix, _ws, _trail = mode.system_prompt_parts()
            assert prefix and mode.profile.guidance in prefix[0]
        assert len(set(guidances)) == 4

    def test_unregistered_profile_falls_back(self, tmp_path):
        # Bare dir + auto → general. Unregistered name must not raise.
        mode = cc.resolve_runtime_mode(
            platform="desktop",
            cwd=tmp_path,
            config={"agent": {"coding_context": "auto"}},
            profile="not_a_real_mode",
        )
        assert mode.pinned is False
        assert mode.profile.name == "general"

    def test_unregistered_does_not_block_coding_detect(self, tmp_path):
        (tmp_path / "main.py").write_text("print(1)\n")
        # Make a real code workspace without importing the heavy git helper.
        import subprocess
        import shutil

        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(tmp_path),
        }
        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            ["commit", "-q", "-m", "init"],
        ):
            subprocess.run(
                [shutil.which("git"), "-C", str(tmp_path), *args],
                check=True,
                env=env,
            )
        mode = cc.resolve_runtime_mode(
            platform="cli",
            cwd=tmp_path,
            config={"agent": {"coding_context": "auto"}},
            profile="nope",
        )
        assert mode.pinned is False
        assert mode.is_coding is True


class TestPinnedToolsetGate:
    def test_pinned_collapses_toolset_under_auto(self, tmp_path):
        # The B0 gate: pinned profile collapses toolset even when coding_context
        # is auto (which is prompt-only for unpinned coding).
        cfg = {"agent": {"coding_context": "auto"}}
        mode = cc.resolve_runtime_mode(
            platform="desktop", cwd=tmp_path, config=cfg, profile="sanad"
        )
        assert mode.pinned is True
        assert mode.config_mode == "auto"
        selection = mode.toolset_selection(cfg)
        assert selection is not None
        assert selection[0] == "burooj_sanad"

    def test_unpinned_auto_stays_prompt_only(self, tmp_path):
        (tmp_path / "package.json").write_text("{}\n")
        cfg = {"agent": {"coding_context": "auto"}}
        mode = cc.resolve_runtime_mode(platform="cli", cwd=tmp_path, config=cfg)
        assert mode.pinned is False
        assert mode.is_coding is True
        assert mode.toolset_selection(cfg) is None

    def test_coding_selection_wrapper_honors_pin(self, tmp_path):
        out = cc.coding_selection(
            platform="desktop",
            cwd=tmp_path,
            config={"agent": {"coding_context": "auto"}},
            profile="build",
        )
        assert out is not None
        assert out[0] == "burooj_build"


class TestRegisterProfile:
    def test_reregister_replaces(self):
        name = "_b0_test_replace"
        first = ContextProfile(name=name, guidance="first", toolset="burooj_agent")
        second = ContextProfile(name=name, guidance="second", toolset="burooj_sanad")
        register_profile(first)
        assert cc.get_profile(name).guidance == "first"
        register_profile(second)
        assert cc.get_profile(name).guidance == "second"
        assert cc.get_profile(name).toolset == "burooj_sanad"
        # Exactly one registry entry for the name.
        assert sum(1 for k in cc._PROFILES if k == name) == 1
        # Cleanup so later tests don't see the probe profile.
        del cc._PROFILES[name]


class TestBuroojToolsets:
    def test_toolsets_registered(self):
        from toolsets import resolve_toolset, validate_toolset

        for name in ("burooj_agent", "burooj_sanad", "burooj_build", "burooj_design"):
            assert validate_toolset(name)
            tools = resolve_toolset(name)
            assert tools

    def test_sanad_excludes_terminal_and_code(self):
        from toolsets import resolve_toolset

        tools = set(resolve_toolset("burooj_sanad"))
        assert "read_file" in tools
        assert "web_search" in tools
        assert "memory" in tools
        assert "todo" in tools
        for banned in ("terminal", "execute_code", "cronjob", "kanban_show"):
            assert banned not in tools

    def test_build_excludes_image_tts_ha(self):
        from toolsets import resolve_toolset

        tools = set(resolve_toolset("burooj_build"))
        assert "terminal" in tools
        assert "patch" in tools
        assert "execute_code" in tools
        for banned in ("image_generate", "text_to_speech", "ha_list_entities"):
            assert banned not in tools

    def test_design_excludes_kanban_cron_execute(self):
        from toolsets import resolve_toolset

        tools = set(resolve_toolset("burooj_design"))
        assert "vision_analyze" in tools
        assert "image_generate" in tools
        for banned in ("kanban_show", "cronjob", "execute_code", "ha_list_entities"):
            assert banned not in tools

    def test_agent_is_full_core(self):
        from toolsets import resolve_toolset, _HERMES_CORE_TOOLS

        tools = set(resolve_toolset("burooj_agent"))
        for t in _HERMES_CORE_TOOLS:
            assert t in tools
