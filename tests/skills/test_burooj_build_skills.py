"""Tests for the Burooj Build skills: ship-landing-page and verify-loop.

The skills system discovers any ``SKILL.md`` under ``skills/``; Build mode
reaches them through the ``skills_list`` / ``skill_view`` tools in the
``burooj_build`` toolset. These tests assert the skills load, follow the
authoring contract (frontmatter, native tool names, no fabricated tools)
and are reachable from the Build toolset.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

HERMES_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = HERMES_ROOT / "skills" / "software-development"

BUILD_SKILLS = {
    "ship-landing-page": SKILLS_DIR / "ship-landing-page" / "SKILL.md",
    "verify-loop": SKILLS_DIR / "verify-loop" / "SKILL.md",
}


def _frontmatter(skill_md: Path) -> dict:
    content = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    assert match, f"{skill_md} has no YAML frontmatter"
    return yaml.safe_load(match.group(1))


def _native_builtin_tools() -> set[str]:
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    return set(registry.get_all_tool_names())


class TestSkillFiles:
    @pytest.mark.parametrize("name", sorted(BUILD_SKILLS))
    def test_skill_exists_with_frontmatter(self, name):
        skill_md = BUILD_SKILLS[name]
        assert skill_md.is_file(), f"missing {skill_md}"
        fm = _frontmatter(skill_md)
        assert fm.get("name") == name
        assert fm.get("description"), f"{name}: description is required"

    @pytest.mark.parametrize("name", sorted(BUILD_SKILLS))
    def test_description_within_limit(self, name):
        """description must be one sentence, ≤ 60 chars (skills HARDLINE #1)."""
        fm = _frontmatter(BUILD_SKILLS[name])
        assert len(fm.get("description", "")) <= 60, f"{name}: description too long"

    @pytest.mark.parametrize("name", sorted(BUILD_SKILLS))
    def test_platforms_declared(self, name):
        fm = _frontmatter(BUILD_SKILLS[name])
        platforms = fm.get("platforms", "")
        assert platforms, f"{name}: platforms: is required"
        assert "windows" in platforms

    @pytest.mark.parametrize("name", sorted(BUILD_SKILLS))
    def test_modern_section_order(self, name):
        """Body follows the modern order: When to Use, Prerequisites, ..."""
        content = BUILD_SKILLS[name].read_text(encoding="utf-8")
        positions = [content.find(sec) for sec in ("## When to Use", "## Pitfalls")]
        assert all(p != -1 for p in positions), f"{name}: missing required section"
        assert positions[0] < positions[1], f"{name}: sections out of order"

    @pytest.mark.parametrize("name", sorted(BUILD_SKILLS))
    def test_tools_referenced_are_real(self, name):
        """Every backticked snake_case token in the body must be a registered
        tool. Prose words like `skip` and `test` are not tool references."""
        content = BUILD_SKILLS[name].read_text(encoding="utf-8")
        natives = _native_builtin_tools()
        mentioned = set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)`", content))
        non_tools = {"scaffold-app", "scaffold-design-system", "verify-loop"}
        unknown = sorted(
            m for m in mentioned
            if m not in natives and m not in non_tools
        )
        assert not unknown, f"{name}: mentions non-existent tools: {unknown}"

    @pytest.mark.parametrize("name", sorted(BUILD_SKILLS))
    def test_no_em_dashes(self, name):
        """Writing style: no em dashes anywhere in the skill."""
        content = BUILD_SKILLS[name].read_text(encoding="utf-8")
        assert "\u2014" not in content, f"{name}: contains an em dash"


class TestBuildToolsetReachesSkills:
    def test_build_toolset_includes_skills_tools(self):
        """Skills are reachable from Build mode through the toolset."""
        from toolsets import resolve_toolset

        tools = set(resolve_toolset("burooj_build"))
        for name in ("skills_list", "skill_view", "skill_manage"):
            assert name in tools, f"burooj_build missing {name}"

    def test_skills_are_in_software_development_category(self):
        """Both skills live under the software-development category, which is
        the category the Build profile does NOT compact to names-only."""
        from agent.burooj_profiles import BUROOJ_PROFILES
        from agent.coding_context import _NON_CODING_SKILL_CATEGORIES

        build = next(p for p in BUROOJ_PROFILES if p.name == "build")
        compacted = frozenset(build.compact_skill_categories)
        assert "software-development" not in compacted
        assert "software-development" not in _NON_CODING_SKILL_CATEGORIES

    def test_skills_list_loads_both(self, monkeypatch, tmp_path):
        """The skills scanner discovers both skills with correct metadata."""
        from tools.skills_tool import _find_all_skills

        # Point the scanner at the repo's bundled skills directory.
        monkeypatch.setattr(
            "tools.skills_tool._skills_dir",
            lambda: HERMES_ROOT / "skills",
        )
        all_skills = _find_all_skills(skip_disabled=True)
        names = {s["name"] for s in all_skills}
        for name in BUILD_SKILLS:
            assert name in names, f"{name} not discovered by the skills scanner"
        by_name = {s["name"]: s for s in all_skills}
        assert by_name["ship-landing-page"]["category"] == "software-development"
        assert by_name["verify-loop"]["category"] == "software-development"
