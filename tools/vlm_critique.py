"""vlm_critique tool - advisory vision-model critique of rendered routes.

Rung 5 of the design gate (architecture spec §5.5), and deliberately the
only rung that never blocks.

Every other design check is deterministic: the same input produces the same
result. A vision model is not. It can fail differently on identical input,
and a gate that does that is worse than no gate. So this tool reports, it
does not decide: its output is marked advisory, carries no pass/fail, and
``verify``'s design gate never fails on it.

Tool schema:
    vlm_critique(workspace: Path, routes: list[str] | None = None)
        -> { status: "advisory"|"skip"|"error", routes: [RouteCritique], advisory: true }

Each RouteCritique: { path, model, issues: [Issue], summary }
Each Issue: { severity: "info"|"minor"|"major", point, suggestion }
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

logger = logging.getLogger("hermes.vlm_critique")

# Viewport used for critique captures. Same as the other browser rungs so the
# critic sees what visual_diff compares.
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 800

# How long a single vision call may take. The critique is advisory: a slow or
# hanging model call must never hold the ladder hostage.
_CRITIQUE_TIMEOUT = 60

# Where the critique prompt tells the model to focus. Fixed, so the model's
# output stays comparable between runs.
_CRITIQUE_PROMPT = (
    "You are a design reviewer. Look at this screenshot of a web page and "
    "report ONLY concrete, actionable issues a designer could fix. For each "
    "issue give a severity (info, minor, major), the point, and a specific "
    "suggestion. Do not praise the page. Do not invent problems that are not "
    "visible. If the page looks fine, return an empty issues list. "
    "Reply with JSON: {\"issues\": [{\"severity\": \"minor\", \"point\": "
    "\"...\", \"suggestion\": \"...\"}]}"
)


@dataclass
class CritiqueIssue:
    severity: str
    point: str
    suggestion: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "point": self.point,
            "suggestion": self.suggestion,
        }


@dataclass
class RouteCritique:
    path: str
    model: str = ""
    issues: list[CritiqueIssue] = field(default_factory=list)
    summary: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "issues": [i.to_dict() for i in self.issues],
        }
        if self.model:
            data["model"] = self.model
        if self.summary:
            data["summary"] = self.summary
        if self.error:
            data["error"] = self.error
        return data


async def _critique_route(
    page: Any,
    base_url: str,
    route_path: str,
    screenshot_dir: Path,
) -> RouteCritique:
    """Navigate to *route_path*, screenshot it, and ask the vision model."""
    result = RouteCritique(path=route_path)
    url = f"{base_url}{route_path}"
    try:
        await page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS)
    except Exception as exc:
        result.error = f"Navigation failed: {exc}"
        return result

    route_name = route_path.strip("/").replace("/", "_") or "index"
    screenshot_path = screenshot_dir / f"{route_name}_critique.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=False)
    except Exception as exc:
        result.error = f"Screenshot failed: {exc}"
        return result

    try:
        from tools.vision_tools import vision_analyze_tool

        raw = await vision_analyze_tool(
            image_url=str(screenshot_path),
            user_prompt=_CRITIQUE_PROMPT,
        )
        critique = _parse_vision_response(raw)
        result.issues = critique.get("issues", [])
        result.summary = (
            f"{len(result.issues)} issue(s) reported by vision review"
        )
        if critique.get("model"):
            result.model = critique["model"]
    except Exception as exc:
        # Advisory only: a vision failure must never fail the gate. It is
        # reported, and the caller decides what that means.
        result.error = f"Vision call failed: {type(exc).__name__}: {exc}"
        logger.debug("vlm_critique vision failure for %s", route_path, exc_info=True)
    return result


def _parse_vision_response(raw: str) -> dict[str, Any]:
    """Best-effort parse of the vision model's reply into issues.

    The model is asked for JSON but models wrap JSON in prose or fence blocks.
    Extract the first JSON object defensively; anything unparseable yields an
    empty issue list, which for an advisory rung is a safe default.
    """
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    issues = parsed.get("issues")
    if not isinstance(issues, list):
        return {}
    clean: list[dict[str, str]] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "minor")
        if severity not in {"info", "minor", "major"}:
            severity = "minor"
        clean.append({
            "severity": severity,
            "point": str(item.get("point") or "").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
        })
    clean = [i for i in clean if i["point"]]
    out: dict[str, Any] = {"issues": clean}
    if parsed.get("model"):
        out["model"] = str(parsed["model"])
    return out


async def _run_critique(session: BuildSession, routes: list[str]) -> dict[str, Any]:
    """Async implementation against the shared dev server."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"status": "skip", "reason": PLAYWRIGHT_MISSING, "routes": []}

    screenshot_dir = session.workspace / ".burooj" / "critique"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    status = session.ensure_server(timeout=30)
    if not status.ready:
        return {
            "status": "skip",
            "reason": f"no dev server to review: {status.reason}",
            "routes": [],
        }

    route_results: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for route_path in routes:
                page = await new_page(browser, _VIEWPORT_WIDTH, _VIEWPORT_HEIGHT)
                result = await _critique_route(
                    page, status.base_url, route_path, screenshot_dir
                )
                route_results.append(result.to_dict())
                await page.close()
        finally:
            await browser.close()

    return {"status": "advisory", "routes": route_results}


def vlm_critique(
    workspace: Optional[Path] = None,
    routes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Capture route screenshots and ask a vision model for design critique.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    routes : list[str], optional
        Routes to review. Defaults to manifest's declared routes.

    Returns
    -------
    dict with keys:
        status : "advisory" | "skip" | "error"
        advisory : True - this result never fails the design gate
        routes : list[dict] - per-route critique
    """
    if workspace is None:
        workspace = resolve_workspace()

    try:
        session = get_session(Path(workspace))
    except ManifestError as exc:
        return {"status": "skip", "reason": str(exc), "routes": [], "advisory": True}

    if session.manifest.dev is None:
        return {
            "status": "skip",
            "reason": "no 'dev' section in burooj.build.json, nothing to review",
            "routes": [],
            "advisory": True,
        }

    if routes is None:
        routes = session.manifest.routes

    try:
        result = run_async(_run_critique(session, routes), timeout=_CAPTURE_TIMEOUT)
    except TimeoutError as exc:
        result = {"status": "error", "error": str(exc), "routes": [], "advisory": True}
    except Exception as exc:
        result = {
            "status": "error",
            "error": f"vlm_critique crashed: {type(exc).__name__}: {exc}",
            "routes": [],
            "advisory": True,
        }
    result["advisory"] = True

    # The Design panel shows the last run. Record it here so a model-driven
    # run inside a session is visible to the panel.
    from agent.burooj_status import record_design_check

    record_design_check(workspace, "vlm_critique", result)

    return result


# ── Tool registration ───────────────────────────────────────────────────────

VLM_CRITIQUE_SCHEMA = {
    "name": "vlm_critique",
    "description": (
        "Ask a vision model to review the rendered routes for design issues. "
        "ADVISORY ONLY: the result never fails the design gate. Vision models "
        "are not reproducible, and a gate that fails differently on identical "
        "input is worse than no gate. Use it for suggestions, never as a "
        "pass/fail signal."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "routes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Routes to review. Omit to use the manifest's routes.",
            },
        },
        "required": [],
    },
}


def handle_vlm_critique(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for vlm_critique."""
    routes = args.get("routes")
    if routes is not None and (
        not isinstance(routes, list) or not all(isinstance(r, str) for r in routes)
    ):
        return json.dumps({"error": "'routes' must be an array of strings."})
    try:
        return json.dumps(vlm_critique(routes=routes), indent=2)
    except Exception as exc:
        logger.exception("vlm_critique failed")
        return json.dumps({"error": f"vlm_critique crashed: {type(exc).__name__}: {exc}"})


registry.register(
    name="vlm_critique",
    toolset="burooj_design",
    schema=VLM_CRITIQUE_SCHEMA,
    handler=handle_vlm_critique,
    emoji="🎨",
)
