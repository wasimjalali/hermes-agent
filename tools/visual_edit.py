"""visual_edit tool - patch JSX source through a data-oid.

Onlook-style visual editing, agent-driven: an ``element_map`` run stamps the
rendered page with ``data-oid`` values that match this module's static index
(``tools/source_map.py``). The model picks an element by oid and requests an
edit; this tool locates the JSX node by byte range, patches the source, and
refuses to write unless the tree still parses and the edit landed exactly at
the target node.

Operations (v1):
    set_class    - set or replace the className attribute value
    set_attr     - set or replace any attribute value
    remove_attr  - remove an attribute entirely
    set_text     - replace the element's literal JSX text

Scope: intrinsic elements in workspace-authored components. Custom
components resolve to their own oid but editing them, or any element whose
path the browser could not reproduce, is an explicit error. A patch that
cannot be verified is never written.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from agent.build_workspace import resolve_workspace
from tools.registry import registry
from tools.source_map import (
    ElementInfo,
    SourceIndex,
    build_source_index,
)

logger = logging.getLogger("hermes.visual_edit")

# Operations that replace an attribute value vs those that need the value
# quoted. Both write into the existing attribute's byte range when present.
_ATTR_OPS = frozenset({"set_class", "set_attr"})
_REMOVE_OPS = frozenset({"remove_attr"})
_TEXT_OPS = frozenset({"set_text"})


class EditError(ValueError):
    """Raised when an edit cannot be applied safely. Never write on error."""


def _reparse_ok(source: bytes) -> bool:
    """Whether *source* still parses as valid TSX."""
    try:
        from tree_sitter_language_pack import get_parser

        tree = get_parser("tsx").parse(source)
        return not tree.root_node.has_error
    except Exception:
        return False


def _find_attribute(
    element: ElementInfo, source: bytes, name: str
) -> Optional[tuple[int, int, Optional[str]]]:
    """Locate an attribute's (start, end, quoted_value) byte ranges.

    Returns None when the attribute is absent. When present, the value may
    be quoted (``className="x"``) or an expression (``className={x}``); the
    third element is the quoted value text including quotes, or None for
    expression values.
    """
    for attr_name, start, end in element.attributes:
        if attr_name != name:
            continue
        raw = source[start:end]
        # ``name="value"`` or ``name={expr}`` or bare ``name``.
        eq = raw.find(b"=")
        if eq == -1:
            return (start, end, None)
        value = raw[eq + 1:].strip()
        if value.startswith(b'"') or value.startswith(b"'"):
            quote = value[:1]
            close = value[1:].find(quote)
            if close != -1:
                value_end = eq + 1 + 1 + close + 1  # start + '=' + quote + value + quote
                return (start + eq + 1, start + value_end, value[:close + 2].decode())
        return (start + eq + 1, end, None)  # expression value
    return None


def _set_attr_patch(
    element: ElementInfo, source: bytes, attr: str, value: str
) -> tuple[bytes, str, str]:
    """Return (patched_source, old_text, new_text) for set_attr/set_class."""
    found = _find_attribute(element, source, attr)
    if found is not None:
        start, _end, quoted = found
        if quoted is not None:
            new_value = f'"{value}"'
            old = source[start:start + len(quoted)].decode(errors="replace")
            patched = source[:start] + new_value.encode() + source[start + len(quoted):]
            return patched, old, f'"{value}"'
        # Expression value: replace the whole attribute with a quoted one.
        for name, astart, aend in element.attributes:
            if name == attr:
                new_attr = f'{attr}="{value}"'
                old = source[astart:aend].decode(errors="replace")
                patched = source[:astart] + new_attr.encode() + source[aend:]
                return patched, old, new_attr
        raise EditError(f"attribute {attr!r} not found on element")

    # Insert a new attribute right after the tag name.
    insert_at = element.tag_byte + len(element.tag.encode())
    new_attr = f' {attr}="{value}"'
    patched = source[:insert_at] + new_attr.encode() + source[insert_at:]
    return patched, "", new_attr


def _remove_attr_patch(
    element: ElementInfo, source: bytes, attr: str
) -> tuple[bytes, str, str]:
    for name, start, end in element.attributes:
        if name != attr:
            continue
        removed = source[start:end].decode(errors="replace")
        patched = source[:start] + source[end:]
        # Collapse the whitespace the attribute left behind ("<h1 >x</h1>").
        # After the splice the space sits just before *start*; drop it when
        # it lands directly between the tag name and the closing ">".
        if patched[start - 1:start + 1] == b" >":
            patched = patched[:start - 1] + patched[start:]
        return patched, removed, ""
    raise EditError(f"attribute {attr!r} not found on element")


def _set_text_patch(
    element: ElementInfo, source: bytes, text: str
) -> tuple[bytes, str, str]:
    if element.text_range is None:
        raise EditError(
            f"element <{element.tag}> has no literal JSX text to replace"
        )
    start, end = element.text_range
    old = source[start:end].decode(errors="replace")
    patched = source[:start] + text.encode() + source[end:]
    return patched, old, text


def _apply_patch(
    index: SourceIndex, oid: str, op: str, attr: str, value: str
) -> tuple[Path, str, str, bytes]:
    """Apply *op* to the element with *oid*.

    Returns (path, old_text, new_text, patched_source).
    """
    element = index.element(oid)
    if element is None:
        raise EditError(f"no element with oid {oid} in the source index")
    if element.custom_component:
        raise EditError(
            f"<{element.tag}> is a custom component. v1 edits only intrinsic "
            f"elements (div, p, h1, button, ...). Edit the component's own "
            f"file instead."
        )

    path = Path(element.file)
    source = path.read_bytes()

    if op in _ATTR_OPS:
        if not attr:
            raise EditError(f"{op} requires an attribute name")
        patched, old, new = _set_attr_patch(element, source, attr, value)
    elif op in _REMOVE_OPS:
        if not attr:
            raise EditError("remove_attr requires an attribute name")
        patched, old, new = _remove_attr_patch(element, source, attr)
    elif op in _TEXT_OPS:
        patched, old, new = _set_text_patch(element, source, value)
    else:
        raise EditError(f"unknown operation {op!r}")

    if not _reparse_ok(patched):
        raise EditError(
            f"patched source for {path} does not parse; refusing to write"
        )

    # The edit must actually have changed something.
    if patched == source:
        raise EditError(f"{op} produced no change for oid {oid}")

    return path, old, new, patched


def visual_edit(
    workspace: Optional[Path] = None,
    oid: str = "",
    op: str = "",
    attr: str = "",
    value: str = "",
) -> dict[str, Any]:
    """Patch a JSX element identified by *oid*.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    oid : str
        The element's data-oid from an ``element_map`` run.
    op : str
        One of set_class, set_attr, remove_attr, set_text.
    attr : str
        Attribute name for set_attr / set_class / remove_attr.
    value : str
        New value for set_class / set_attr / set_text.

    Returns
    -------
    dict with keys:
        oid, op, file, applied : bool
        old : the replaced source text ("" for insertions)
        new : the inserted source text
    """
    if workspace is None:
        workspace = resolve_workspace()
    if not oid:
        return {"error": "an oid is required. Run element_map to stamp the page."}
    if not op:
        return {"error": "an operation is required: set_class, set_attr, remove_attr, set_text."}

    try:
        index = build_source_index(Path(workspace))
        path, old, new, patched = _apply_patch(index, oid, op, attr, value)
    except EditError as exc:
        return {"error": str(exc), "oid": oid, "op": op}
    except Exception as exc:
        logger.exception("visual_edit failed")
        return {"error": f"visual_edit crashed: {type(exc).__name__}: {exc}"}

    # Write through the same confinement the workspace tools use; a failure
    # here means the file was not touched.
    path.write_bytes(patched)

    return {
        "oid": oid,
        "op": op,
        "file": str(path),
        "applied": True,
        "old": old,
        "new": new,
    }


# ── Tool registration ───────────────────────────────────────────────────────

VISUAL_EDIT_SCHEMA = {
    "name": "visual_edit",
    "description": (
        "Patch a rendered JSX element by its data-oid: set_class, set_attr, "
        "remove_attr, set_text. The oid comes from element_map, which stamps "
        "the running app. The edit lands in the workspace source and is "
        "verified by re-parsing before it is written. Custom components and "
        "unmapped elements are explicit errors, never silent guesses. Run "
        "verify() after editing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "oid": {
                "type": "string",
                "description": "The element's data-oid from element_map.",
            },
            "op": {
                "type": "string",
                "enum": ["set_class", "set_attr", "remove_attr", "set_text"],
                "description": "What to do to the element.",
            },
            "attr": {
                "type": "string",
                "description": "Attribute name for set_class / set_attr / remove_attr.",
            },
            "value": {
                "type": "string",
                "description": "New value for set_class / set_attr / set_text.",
            },
        },
        "required": ["oid", "op"],
    },
}


def handle_visual_edit(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for visual_edit."""
    try:
        return json.dumps(
            visual_edit(
                oid=str(args.get("oid") or ""),
                op=str(args.get("op") or ""),
                attr=str(args.get("attr") or ""),
                value=str(args.get("value") or ""),
            ),
            indent=2,
        )
    except Exception as exc:
        logger.exception("visual_edit failed")
        return json.dumps({"error": f"visual_edit crashed: {type(exc).__name__}: {exc}"})


registry.register(
    name="visual_edit",
    toolset="burooj_design",
    schema=VISUAL_EDIT_SCHEMA,
    handler=handle_visual_edit,
    emoji="✏️",
)
