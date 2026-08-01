"""element_map tool - stamp the running app with data-oid and list elements.

The browser side of the visual-editing pair. ``tools/source_map.py`` computes
a static oid for every JSX element in the workspace, anchored at the
component root. A live DOM is anchored at ``document.body`` and carries
framework wrappers the source never saw, so the browser cannot derive the
same paths by itself.

This tool therefore works in passes:

1. Dump the rendered DOM: for every element, its tag, text, classes and its
   body-relative structural path (sibling indices among *element* siblings;
   text nodes never count, which keeps JSX and DOM sibling counts aligned).
2. In Python, align the DOM to the page component root (the indexed
   component whose root tag matches a DOM element whose subtree contains the
   most indexed elements). The offset between body and component root is the
   alignment.
3. Compute each element's oid with the *same* function the source index
   uses, and cross-check every oid against the index.
4. Write ``data-oid`` back into the DOM so the model can point at an element
   by the id the index knows.

Anything that does not resolve to an indexed element is reported as unmapped
with the reason, never silently dropped, and never patched: ``visual_edit``
refuses oids that are not in the source index.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.build_manifest import ManifestError
from agent.build_session import BuildSession, get_session
from agent.build_workspace import resolve_workspace
from tools.burooj_browser import (
    _CAPTURE_TIMEOUT,
    _NAV_TIMEOUT_MS,
    _WAIT_UNTIL,
    PLAYWRIGHT_MISSING,
    new_page,
    run_async,
)
from tools.registry import registry
from tools.source_map import (
    SourceIndex,
    _oid_for_path,
    build_source_index,
    tree_sitter_available,
)

logger = logging.getLogger("hermes.element_map")

# Dump pass: body-relative structural paths, no oid computation in the
# browser. Paths are arrays of sibling indices among element children.
_DUMP_SCRIPT = """
() => {
  const out = [];
  const walk = (node, path) => {
    const parent = node.parentElement;
    const siblings = parent ? Array.from(parent.children) : [node];
    const index = siblings.indexOf(node);
    const p = path.concat(index);
    out.push({
      path: p.join('.'),
      tag: node.tagName.toLowerCase(),
      text: (node.childNodes.length === 1 && node.childNodes[0].nodeType === 3)
        ? node.textContent.trim() : '',
      classes: Array.from(node.classList).slice(0, 8),
    });
    for (const child of node.children) walk(child, p);
  };
  walk(document.body, []);
  return out;
}
"""

# Stamp pass: set data-oid + data-oid-path on elements by body-relative path.
_STAMP_SCRIPT = """
(map) => {
  let stamped = 0;
  for (const el of document.querySelectorAll('body *')) {
    const parent = el.parentElement;
    const siblings = parent ? Array.from(parent.children) : [el];
    const index = siblings.indexOf(el);
    const path = [];
    let cur = el;
    while (cur && cur !== document.body) {
      const par = cur.parentElement;
      if (!par) break;
      path.unshift(Array.from(par.children).indexOf(cur));
      cur = par;
    }
    const key = path.join('.');
    if (map[key]) {
      el.setAttribute('data-oid', map[key]);
      el.setAttribute('data-oid-path', key);
      stamped++;
    }
  }
  return stamped;
}
"""


@dataclass
class ElementObservation:
    oid: str
    path: str
    tag: str
    text: str = ""
    classes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "oid": self.oid,
            "path": self.path,
            "tag": self.tag,
            "text": self.text[:80],
            "classes": self.classes[:8],
        }


def _dom_paths_to_oids(
    dom: list[dict[str, Any]],
    index: SourceIndex,
) -> tuple[list[ElementObservation], list[dict[str, Any]]]:
    """Align the DOM dump to the source index and compute oids.

    Returns (elements, unmapped). The alignment: the indexed component whose
    root element tag appears in the DOM, whose subtree contains the most
    indexed elements, wins. Only oids present in the index are emitted; every
    other DOM element is reported unmapped.
    """
    by_path: dict[str, dict[str, Any]] = {d["path"]: d for d in dom}

    # Every indexed element's path, keyed by oid, so we can find which DOM
    # subtree position yields the most index hits for a given offset.
    index_paths = [(oid, e.path) for oid, e in index.by_oid.items() if not e.custom_component]

    # Candidate anchors: DOM elements whose tag matches an indexed
    # component's root element tag.
    root_tags = {
        component.elements[0].tag
        for component in index.components
        if component.elements
    }
    anchors: list[tuple[int, str]] = []  # (depth, dom_path)
    for dom_path, el in by_path.items():
        if el["tag"] in root_tags:
            anchors.append((dom_path.count(".") + 1, dom_path))

    best: Optional[str] = None
    best_hits = -1
    for _depth, anchor_path in anchors:
        anchor_parts = [int(p) for p in anchor_path.split(".")] if anchor_path else []
        hits = 0
        for _oid, src_path in index_paths:
            dom_path = list(anchor_parts) + list(src_path)
            if ".".join(str(i) for i in dom_path) in by_path:
                hits += 1
        if hits > best_hits:
            best_hits = hits
            best = anchor_path

    elements: list[ElementObservation] = []
    unmapped: list[dict[str, Any]] = []

    if best is None or best_hits <= 0:
        for d in dom:
            unmapped.append({
                "path": d["path"],
                "tag": d["tag"],
                "reason": "no component root alignment found in the DOM",
            })
        return elements, unmapped

    anchor_parts = [int(p) for p in best.split(".")] if best else []
    known = {tuple(e.path): oid for oid, e in index.by_oid.items() if not e.custom_component}

    for d in dom:
        parts = [int(p) for p in d["path"].split(".")] if d["path"] else []
        if len(parts) < len(anchor_parts):
            unmapped.append({
                "path": d["path"],
                "tag": d["tag"],
                "reason": "above the aligned component root",
            })
            continue
        rel = tuple(parts[len(anchor_parts):])
        oid = known.get(rel)
        if oid is None:
            # The element is inside a custom component's subtree or is
            # framework-generated; it is not editable in v1.
            unmapped.append({
                "path": d["path"],
                "tag": d["tag"],
                "reason": (
                    "not an indexed intrinsic element (inside a custom "
                    "component, a mapped/conditional sibling, or framework "
                    "wrapping)"
                ),
            })
            continue
        elements.append(ElementObservation(
            oid=oid,
            path=d["path"],
            tag=d["tag"],
            text=d.get("text", ""),
            classes=d.get("classes", []),
        ))

    elements.sort(key=lambda e: e.path)
    return elements, unmapped


async def _map_route(
    page: Any,
    base_url: str,
    route_path: str,
    index: SourceIndex,
) -> dict[str, Any]:
    """Navigate to *route_path*, dump, align, and stamp data-oid."""
    url = f"{base_url}{route_path}"
    await page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS)

    dom = await page.evaluate(_DUMP_SCRIPT)
    elements, unmapped = _dom_paths_to_oids(dom, index)

    stamp_map = {e.path: e.oid for e in elements}
    await page.evaluate(_STAMP_SCRIPT, stamp_map)

    return {
        "path": route_path,
        "elements": [e.to_dict() for e in elements],
        "unmapped": unmapped[:20],
        "stamped": len(elements),
    }


async def _run_element_map(
    session: BuildSession, routes: list[str], index: SourceIndex
) -> dict[str, Any]:
    """Async implementation against the shared dev server."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"status": "skip", "reason": PLAYWRIGHT_MISSING, "routes": []}

    status = session.ensure_server(timeout=30)
    if not status.ready:
        return {
            "status": "error",
            "error": f"dev server not ready: {status.reason}",
            "routes": [],
        }

    routes_out: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for route_path in routes:
                page = await new_page(browser)
                try:
                    result = await _map_route(page, status.base_url, route_path, index)
                except Exception as exc:
                    result = {
                        "path": route_path,
                        "elements": [],
                        "unmapped": [],
                        "error": f"stamp failed: {type(exc).__name__}: {exc}",
                    }
                    logger.debug("element_map stamp failed for %s", route_path, exc_info=True)
                routes_out.append(result)
                await page.close()
        finally:
            await browser.close()

    return {"status": "pass", "routes": routes_out}


def element_map(
    workspace: Optional[Path] = None,
    routes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Stamp the running app with data-oid and list the editable elements.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    routes : list[str], optional
        Routes to stamp. Defaults to manifest's declared routes.

    Returns
    -------
    dict with keys:
        status : "pass" | "skip" | "error"
        index : summary of the source index
        routes : list[dict] - per-route { path, elements, unmapped }
    """
    if workspace is None:
        workspace = resolve_workspace()

    if not tree_sitter_available():
        return {
            "status": "error",
            "error": (
                "source_map needs tree-sitter. Install with: "
                "pip install -e '.[burooj-build]'"
            ),
            "routes": [],
        }

    try:
        index = build_source_index(Path(workspace))
    except Exception as exc:
        return {"status": "error", "error": str(exc), "routes": []}

    try:
        session = get_session(Path(workspace))
    except ManifestError as exc:
        return {"status": "skip", "reason": str(exc), "routes": [], "index": index.to_dict()}

    if session.manifest.dev is None:
        return {
            "status": "skip",
            "reason": "no 'dev' section in burooj.build.json, nothing to stamp",
            "routes": [],
            "index": index.to_dict(),
        }

    if routes is None:
        routes = session.manifest.routes

    try:
        result = run_async(
            _run_element_map(session, routes, index), timeout=_CAPTURE_TIMEOUT
        )
    except TimeoutError as exc:
        result = {"status": "error", "error": str(exc), "routes": []}
    except Exception as exc:
        result = {
            "status": "error",
            "error": f"element_map crashed: {type(exc).__name__}: {exc}",
            "routes": [],
        }

    result["index"] = index.to_dict()
    return result


# ── Tool registration ───────────────────────────────────────────────────────

ELEMENT_MAP_SCHEMA = {
    "name": "element_map",
    "description": (
        "Stamp the running app's DOM with data-oid attributes and list the "
        "editable elements per route. Pairs with visual_edit: pick an "
        "element by oid here, edit it there. Intrinsic elements in authored "
        "components only; everything else is reported as unmapped with a "
        "reason, never guessed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "routes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Routes to stamp. Omit to use the manifest's routes.",
            },
        },
        "required": [],
    },
}


def handle_element_map(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for element_map."""
    routes = args.get("routes")
    if routes is not None and (
        not isinstance(routes, list) or not all(isinstance(r, str) for r in routes)
    ):
        return json.dumps({"error": "'routes' must be an array of strings."})
    try:
        return json.dumps(element_map(routes=routes), indent=2)
    except Exception as exc:
        logger.exception("element_map failed")
        return json.dumps({"error": f"element_map crashed: {type(exc).__name__}: {exc}"})


registry.register(
    name="element_map",
    toolset="burooj_design",
    schema=ELEMENT_MAP_SCHEMA,
    handler=handle_element_map,
    emoji="🗺️",
)
