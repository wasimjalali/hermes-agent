"""source_map - static data-oid index over workspace JSX/TSX.

Onlook-style source mapping, rewritten on our own tree-sitter stack (the
same stack ``repo_map`` uses; no Onlook code is vendored, the mechanism is
credited in NOTICE.md).

The core idea, learned from Onlook (Apache-2.0): give every JSX element a
stable id derived from its *structural path* in the component tree. A path
is the sequence of sibling indices from the component root down to the
element, where each index counts the element's position among its *element*
siblings (text, whitespace and comments do not count). Because the
derivation is static, the same path can be recomputed on the rendered DOM,
and an ``element_map`` run over the browser yields the same ids.

This module builds the source side: it parses every authored ``.tsx`` /
``.jsx`` in the workspace, computes the path and oid for each JSX element,
and records the byte ranges a patch needs. It never edits files; patching
lives in ``tools/visual_edit.py``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools.repo_map import TreeSitterUnavailable, tree_sitter_available

logger = logging.getLogger("hermes.source_map")

# Extensions we index. scss/css are linted by design_lint, not edited here.
_TS_EXTENSIONS = frozenset({".tsx", ".jsx"})

# Directories never treated as authored source.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", ".next", ".turbo", ".burooj", "coverage", "burooj.design",
})

# Component names are capitalized identifiers; intrinsic elements are
# lowercase. Used only to report unmapped custom components loudly.
_INTRINSIC_NAMES = frozenset({
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base",
    "bdi", "bdo", "blockquote", "body", "br", "button", "canvas", "caption",
    "cite", "code", "col", "colgroup", "data", "datalist", "dd", "del",
    "details", "dfn", "dialog", "div", "dl", "dt", "em", "embed", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "head", "header", "hgroup", "hr", "html", "i", "iframe", "img",
    "input", "ins", "kbd", "label", "legend", "li", "link", "main", "map",
    "mark", "menu", "meta", "meter", "nav", "noscript", "object", "ol",
    "optgroup", "option", "output", "p", "picture", "pre", "progress", "q",
    "rp", "rt", "ruby", "s", "samp", "script", "section", "select", "slot",
    "small", "source", "span", "strong", "style", "sub", "summary", "sup",
    "table", "tbody", "td", "template", "textarea", "tfoot", "th", "thead",
    "time", "title", "tr", "track", "u", "ul", "var", "video", "wbr",
})


class OidCollisionError(ValueError):
    """Two elements produced the same oid. Indexing must not drop either."""


def _oid_for_path(
    file_rel: str,
    path: tuple[int, ...],
    component: str = "",
    root: int = 0,
) -> str:
    """Stable oid for one element in one workspace file.

    Digests the workspace-relative file path, optional component name, the
    JSX root ordinal within that component, and the structural sibling path.

    The file path is required so two components with the same tree shape in
    different files never share an oid. The root ordinal is required because
    one component routinely has several JSX roots, each at structural path
    ``()``: an early return, a loading guard, a ``.map`` callback. Without it
    every one of those collides, which is the common case, not an edge case.
    """
    structural = ".".join(str(i) for i in path)
    payload = f"{file_rel}\0{component}\0{root}\0{structural}".encode()
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


@dataclass
class ElementInfo:
    """One indexed JSX element."""

    oid: str
    path: tuple[int, ...]
    tag: str
    file: str
    # Byte offsets of the opening element (for attribute insertion).
    open_start: int
    open_end: int
    # Byte offset of the element tag name inside the opening element.
    tag_byte: int
    # (attr_name, attr_byte_start, attr_byte_end) per attribute.
    attributes: list[tuple[str, int, int]] = field(default_factory=list)
    # Text node byte range when the element has literal JSX text.
    text_range: Optional[tuple[int, int]] = None
    # True when the tag name is a capitalized (custom) component.
    custom_component: bool = False
    # Which JSX root of its component this element descends from. A component
    # with an early return or a .map callback has more than one.
    root: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "oid": self.oid,
            "path": ".".join(str(i) for i in self.path),
            "root": self.root,
            "tag": self.tag,
            "file": self.file,
            "attributes": [name for name, _, _ in self.attributes],
            "custom_component": self.custom_component,
        }


@dataclass
class ComponentInfo:
    """A component function and its JSX tree."""

    name: str
    file: str
    start: int
    end: int
    elements: list[ElementInfo] = field(default_factory=list)


@dataclass
class SourceIndex:
    """The oid → element map for one workspace."""

    workspace: str
    components: list[ComponentInfo] = field(default_factory=list)
    by_oid: dict[str, ElementInfo] = field(default_factory=dict)
    unmapped_custom: list[str] = field(default_factory=list)

    def element(self, oid: str) -> Optional[ElementInfo]:
        return self.by_oid.get(oid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "component_count": len(self.components),
            "element_count": len(self.by_oid),
            "elements": [e.to_dict() for e in self.by_oid.values()],
            "unmapped_custom": self.unmapped_custom,
        }


def _walk_source_files(workspace: Path) -> list[Path]:
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in _TS_EXTENSIONS:
                results.append(Path(dirpath) / fname)
    return sorted(results)


def _element_siblings(parent: Any) -> list[Any]:
    """JSX element children of *parent*, in source order.

    Only ``jsx_element`` and ``jsx_self_closing_element`` count; text,
    expressions and comments do not, matching the DOM-side derivation.
    """
    return [
        c for c in parent.children
        if c.type in ("jsx_element", "jsx_self_closing_element")
    ]


def _walk_jsx(
    node: Any,
    path: tuple[int, ...],
    out: list[ElementInfo],
    file: str,
    source: bytes,
    file_rel: str = "",
    component: str = "",
    root_counter: Optional[list[int]] = None,
    root: int = 0,
) -> None:
    """Depth-first walk over JSX elements, recording paths and byte ranges.

    ``root_counter`` is a one-slot box shared across the walk of a single
    component. Each JSX element found at structural path ``()`` claims the
    next ordinal from it and passes that ordinal down to its children, so
    a component with several roots does not give them all the same oid.
    """
    if root_counter is None:
        root_counter = [0]
    if node.type not in ("jsx_element", "jsx_self_closing_element"):
        for child in node.children:
            _walk_jsx(
                child, path, out, file, source, file_rel, component,
                root_counter, root,
            )
        return

    if not path:
        root = root_counter[0]
        root_counter[0] += 1

    opening = None
    for child in node.children:
        if child.type in ("jsx_opening_element", "jsx_self_closing_element"):
            opening = child
            break
    if opening is None:
        return

    tag_name = ""
    tag_byte = opening.start_byte
    for child in opening.children:
        if child.type == "identifier":
            tag_name = source[child.start_byte:child.end_byte].decode()
            tag_byte = child.start_byte
            break
    if not tag_name:
        return

    attributes: list[tuple[str, int, int]] = []
    for child in opening.children:
        if child.type == "jsx_attribute":
            attr_name = ""
            for part in child.children:
                if part.type in ("identifier", "property_identifier"):
                    attr_name = source[part.start_byte:part.end_byte].decode()
                    break
            attributes.append((attr_name, child.start_byte, child.end_byte))

    text_range = None
    for child in node.children:
        if child.type == "jsx_text" and child.end_byte > child.start_byte:
            text = source[child.start_byte:child.end_byte]
            if text.strip():
                text_range = (child.start_byte, child.end_byte)
                break

    out.append(ElementInfo(
        oid=_oid_for_path(file_rel, path, component, root),
        path=path,
        tag=tag_name,
        file=file,
        open_start=opening.start_byte,
        open_end=opening.end_byte,
        tag_byte=tag_byte,
        attributes=attributes,
        text_range=text_range,
        custom_component=tag_name[:1].isupper(),
        root=root,
    ))

    # Children get the path extended with their sibling index, and inherit
    # the root ordinal their subtree hangs from.
    siblings = _element_siblings(node)
    for index, child in enumerate(siblings):
        _walk_jsx(
            child, path + (index,), out, file, source, file_rel, component,
            root_counter, root,
        )


def _component_functions(node: Any) -> list[Any]:
    """Function declarations and arrow functions that return JSX."""
    found: list[Any] = []

    def scan(n: Any) -> None:
        if n.type in ("function_declaration", "arrow_function"):
            found.append(n)
        for child in n.children:
            scan(child)

    scan(node)
    return found


def _function_has_jsx(fn: Any) -> bool:
    for child in fn.children:
        if child.type in ("jsx_element", "jsx_self_closing_element"):
            return True
        # Walk expressions too (return ( <div/> )).
        if child.type in ("statement_block", "parenthesized_expression", "return_statement"):
            if _function_has_jsx(child):
                return True
    return False


def _index_file(path: Path, source: bytes, file_rel: str) -> ComponentInfo:
    """Index one TSX file: its components and their element trees."""
    from tree_sitter_language_pack import get_parser

    parser = get_parser("tsx")
    tree = parser.parse(source)
    component = ComponentInfo(
        name=path.stem, file=str(path), start=0, end=len(source)
    )

    # One counter for the whole file. Component functions are discovered
    # nested (a .map callback is its own arrow function and inherits the
    # enclosing component's name), so a per-function counter would restart at
    # zero and collide with the parent's root. File scope is what makes the
    # ordinal unique.
    root_counter = [0]

    for fn in _component_functions(tree.root_node):
        if not _function_has_jsx(fn):
            continue
        name = ""
        for child in fn.children:
            if child.type == "identifier":
                name = source[child.start_byte:child.end_byte].decode()
                break
        # Arrow functions: name from the enclosing assignment when possible.
        if not name:
            parent = fn.parent
            if parent is not None and parent.type == "variable_declarator":
                for child in parent.children:
                    if child.type == "identifier":
                        name = source[child.start_byte:child.end_byte].decode()
                        break
        component.name = name or component.name
        _walk_jsx(
            fn,
            (),
            component.elements,
            str(path),
            source,
            file_rel=file_rel,
            component=component.name,
            root_counter=root_counter,
        )
    return component


def _workspace_fingerprint(files: list[Path]) -> str:
    hasher = hashlib.blake2b(digest_size=16)
    for path in files:
        try:
            st = path.stat()
            hasher.update(f"{path}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            hasher.update(f"{path}:missing".encode())
    return hasher.hexdigest()


# workspace -> (fingerprint, SourceIndex)
_INDEX_CACHE: dict[str, tuple[str, SourceIndex]] = {}


def build_source_index(workspace: Path, use_cache: bool = True) -> SourceIndex:
    """Build (or return the cached) oid index for *workspace*.

    Raises :class:`~tools.repo_map.TreeSitterUnavailable` when the language
    pack is missing, mirroring ``repo_map``'s contract: an index that was
    never built must not be mistaken for an empty index.
    """
    if not tree_sitter_available():
        raise TreeSitterUnavailable(
            "source_map needs tree-sitter. Install with: "
            "pip install -e '.[burooj-build]'"
        )

    files = _walk_source_files(workspace)
    fingerprint = _workspace_fingerprint(files)
    cached = _INDEX_CACHE.get(str(workspace))
    if use_cache and cached is not None and cached[0] == fingerprint:
        return cached[1]

    index = SourceIndex(workspace=str(workspace))
    root = Path(workspace).resolve()
    for path in files:
        try:
            source = path.read_bytes()
        except OSError:
            continue
        try:
            file_rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            file_rel = path.name
        component = _index_file(path, source, file_rel=file_rel)
        if not component.elements:
            continue
        for element in component.elements:
            if element.custom_component:
                # Custom components are indexed but their internals are not
                # resolvable in v1; surface them so a caller can say why.
                index.unmapped_custom.append(element.tag)
            prior = index.by_oid.get(element.oid)
            if prior is not None:
                raise OidCollisionError(
                    f"oid collision for {element.oid}: "
                    f"{prior.file}:{prior.tag} and {element.file}:{element.tag}"
                )
            index.by_oid[element.oid] = element
        index.components.append(component)

    if use_cache:
        _INDEX_CACHE[str(workspace)] = (fingerprint, index)
    return index
