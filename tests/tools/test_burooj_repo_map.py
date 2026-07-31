"""Tests for repo_map (B2).

Skipped wholesale when tree-sitter is absent, which is itself the point of
``test_missing_tree_sitter_raises``: the tool used to degrade silently into an
alphabetical filename listing and still report success, so the model treated an
empty map as ground truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.repo_map import (
    TreeSitterUnavailable,
    _extract_signature,
    repo_map,
    tree_sitter_available,
)

requires_tree_sitter = pytest.mark.skipif(
    not tree_sitter_available(), reason="tree-sitter not installed"
)


def make_next_workspace(root: Path) -> Path:
    """A miniature Next.js-shaped project using @/ path aliases."""
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "components").mkdir(parents=True)
    (root / "src" / "lib").mkdir(parents=True)

    (root / "tsconfig.json").write_text(
        # JSONC with a comment, as Next.js ships it.
        '{\n  // Next.js default\n  "compilerOptions": {\n'
        '    "baseUrl": ".",\n    "paths": { "@/*": ["./src/*"] }\n  }\n}\n',
        encoding="utf-8",
    )
    (root / "src" / "lib" / "utils.ts").write_text(
        "export function cn(...classes: string[]) {\n  return classes.join(' ')\n}\n",
        encoding="utf-8",
    )
    (root / "src" / "components" / "button.tsx").write_text(
        "import { cn } from '@/lib/utils'\n"
        "export function Button({ label }: { label: string }) {\n"
        "  return <button className={cn('a')}>{label}</button>\n}\n",
        encoding="utf-8",
    )
    (root / "src" / "app" / "page.tsx").write_text(
        "import { Button } from '@/components/button'\n"
        "import { cn } from '@/lib/utils'\n"
        "export default function Home() {\n  return <Button label='x' />\n}\n",
        encoding="utf-8",
    )
    return root


class TestFailLoud:
    def test_missing_tree_sitter_raises(self, tmp_path, monkeypatch):
        """Silently returning a filename list let the model trust an empty map."""
        import tools.repo_map as rm

        monkeypatch.setattr(rm, "tree_sitter_available", lambda: False)
        (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
        with pytest.raises(TreeSitterUnavailable, match="pip install"):
            rm.repo_map(tmp_path)

    def test_handler_reports_the_error_instead_of_a_fake_map(self, tmp_path, monkeypatch):
        import tools.repo_map as rm

        monkeypatch.setattr(rm, "tree_sitter_available", lambda: False)
        monkeypatch.setattr(rm, "resolve_workspace", lambda: tmp_path)
        payload = json.loads(rm.handle_repo_map({}))
        assert "error" in payload
        assert "map" not in payload


@requires_tree_sitter
class TestRepoMap:
    def test_empty_workspace(self, tmp_path):
        result = repo_map(tmp_path)
        assert result["files"] == 0

    def test_extracts_python_definitions(self, tmp_path):
        (tmp_path / "m.py").write_text(
            "class Widget:\n    pass\n\ndef build(name: str) -> Widget:\n    return Widget()\n",
            encoding="utf-8",
        )
        result = repo_map(tmp_path)
        assert "class Widget" in result["map"]
        assert "def build(name: str) -> Widget:" in result["map"]

    def test_resolves_relative_imports(self, tmp_path):
        (tmp_path / "a.ts").write_text("import { b } from './b'\nexport const a = 1\n", encoding="utf-8")
        (tmp_path / "b.ts").write_text("export const b = 2\n", encoding="utf-8")
        assert repo_map(tmp_path)["edges"] >= 1

    def test_resolves_tsconfig_path_aliases(self, tmp_path):
        """Without this the graph had no edges at all on the pinned stack."""
        result = repo_map(make_next_workspace(tmp_path))
        assert result["edges"] == 3, result["map"]

    def test_ranking_puts_the_most_imported_file_first(self, tmp_path):
        result = repo_map(make_next_workspace(tmp_path))
        lines = [ln for ln in result["map"].splitlines() if ln.startswith("## ")]
        assert lines[0] == "## src/lib/utils.ts", lines

    def test_dotted_filename_import_resolves(self, tmp_path):
        """with_suffix() turned './auth.service' into 'auth.ts'."""
        (tmp_path / "auth.service.ts").write_text("export const s = 1\n", encoding="utf-8")
        (tmp_path / "main.ts").write_text(
            "import { s } from './auth.service'\nexport const m = s\n", encoding="utf-8"
        )
        assert repo_map(tmp_path)["edges"] >= 1

    def test_relative_workspace_path_still_resolves_edges(self, tmp_path, monkeypatch):
        """A relative workspace made every relative_to() raise, dropping edges."""
        make_next_workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert repo_map(Path("."))["edges"] == 3

    def test_budget_is_respected(self, tmp_path):
        for i in range(40):
            (tmp_path / f"f{i}.py").write_text(
                f"def function_number_{i}(argument_one, argument_two):\n    return {i}\n",
                encoding="utf-8",
            )
        small = repo_map(tmp_path, budget=300)
        large = repo_map(tmp_path, budget=8000)
        assert len(small["map"]) < len(large["map"])
        assert len(small["map"]) <= int(300 * 3.5) + 200

    def test_cache_returns_identical_output(self, tmp_path):
        make_next_workspace(tmp_path)
        assert repo_map(tmp_path, budget=2000)["map"] == repo_map(tmp_path, budget=2000)["map"]

    def test_cache_invalidates_on_edit(self, tmp_path):
        make_next_workspace(tmp_path)
        before = repo_map(tmp_path, budget=4000)["map"]
        (tmp_path / "src" / "lib" / "extra.ts").write_text(
            "export function brandNewSymbol() { return 1 }\n", encoding="utf-8"
        )
        after = repo_map(tmp_path, budget=4000)["map"]
        assert "brandNewSymbol" in after
        assert before != after

    def test_deeply_nested_source_does_not_blow_the_stack(self, tmp_path):
        """The walk was recursive despite a comment claiming otherwise."""
        depth = 2000
        (tmp_path / "deep.py").write_text(
            "x = " + "(" * depth + "1" + ")" * depth + "\n", encoding="utf-8"
        )
        result = repo_map(tmp_path)  # must not raise RecursionError
        assert result["files"] == 1

    def test_unreadable_file_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / "ok.py").write_text("def f(): pass\n", encoding="utf-8")
        (tmp_path / "binary.py").write_bytes(b"\x00\xff\xfe not valid utf8 \x00")
        assert repo_map(tmp_path)["files"] == 2


class TestSignatureExtraction:
    """The map should carry declarations, not the first lines of each body."""

    class FakeNode:
        def __init__(self, row: int) -> None:
            self.start_point = (row, 0)

    def test_destructured_params_are_not_mistaken_for_the_body(self):
        lines = ["export function Button({ label }: { label: string }) {", "  return null", "}"]
        assert (
            _extract_signature(self.FakeNode(0), lines)
            == "export function Button({ label }: { label: string })"
        )

    def test_body_is_dropped(self):
        lines = ["def build(name: str) -> Widget:", "    return Widget()"]
        assert _extract_signature(self.FakeNode(0), lines) == "def build(name: str) -> Widget:"

    def test_multiline_parameter_list_is_joined(self):
        lines = ["def f(", "    a: int,", "    b: int,", ") -> int:", "    return a"]
        signature = _extract_signature(self.FakeNode(0), lines)
        assert "a: int" in signature and "b: int" in signature

    def test_long_signature_is_truncated(self):
        lines = ["def f(" + ", ".join(f"arg{i}: int" for i in range(200)) + "):"]
        assert len(_extract_signature(self.FakeNode(0), lines)) <= 200

    def test_row_past_end_of_file(self):
        assert _extract_signature(self.FakeNode(99), ["only one line"]) == ""
