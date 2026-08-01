"""Burooj Build/Design status JSON-RPC handlers for the desktop mode panels.

The Build and Design panels live in the renderer; the workspace state they
render lives in this process. These handlers expose it. They read the
per-workspace status store (``agent/burooj_status``) that the tools write
as a side effect of real runs, plus cheap live reads (manifest, dev server
handle, tokens on disk). They never fabricate a result: a check that has
not run in this process reports ``null``, and the panels say so.

``burooj.build.verify``, ``burooj.build.preview`` and ``burooj.design.checks``
run real tool calls and can take minutes (builds, browser boots), so they
are registered in server.py's ``_LONG_HANDLERS`` pool set.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .method_ctx import HandlerRegistry

logger = logging.getLogger("hermes.burooj_rpc")

_registry = HandlerRegistry()
method = _registry.method
register = _registry.install

# All ladder rungs in canonical order, mirrored from tools.verify_tool.
LADDER_RUNGS = (
    "install", "typecheck", "lint", "fix", "guard", "build", "render", "design_gate",
)


def _resolve_workspace_for_rpc(params: dict, cwd: str) -> Path:
    """Resolve the workspace the panel is asking about.

    An explicit ``workspace`` param wins (the desktop pins a folder in the
    panel). Otherwise the session cwd chain from server.py (passed in as
    *cwd*) resolves it the same way a session would.
    """
    from agent.build_workspace import resolve_workspace

    raw = str(params.get("workspace") or "").strip()
    if raw:
        return resolve_workspace(Path(raw))
    return resolve_workspace(cwd)


def _workspace_status(workspace: Path) -> dict[str, Any]:
    """Workspace + manifest + dev server state. Never boots a server."""
    from agent.build_session import get_session
    from agent.build_manifest import ManifestError

    status: dict[str, Any] = {
        "workspace": str(workspace),
        "ladder": list(LADDER_RUNGS),
        "manifest_path": None,
        "manifest": None,
        "manifest_error": None,
        "dev_server": None,
    }

    try:
        session = get_session(workspace)
    except ManifestError as exc:
        status["manifest_error"] = str(exc)
        return status

    status["manifest_path"] = str(workspace / "burooj.build.json")
    m = session.manifest
    status["manifest"] = {
        "install": m.install,
        "typecheck": m.typecheck,
        "lint": m.lint,
        "test_command": m.test.command if m.test else None,
        "test_fix": m.test.fix if m.test else [],
        "test_guard": m.test.guard if m.test else [],
        "build": m.build,
        "dev_command": m.dev.command if m.dev else None,
        "dev_port": m.dev.port if m.dev else None,
        "routes": list(m.routes),
    }

    if m.dev is None:
        return status

    status["dev_server"] = {
        "running": session.server_running,
        "port": m.dev.port,
        "base_url": session.base_url,
        "stdout_tail": [],
        "stderr_tail": [],
    }
    if session.server_running:
        stdout, stderr = session.server_output()
        status["dev_server"]["stdout_tail"] = stdout[-60:]
        status["dev_server"]["stderr_tail"] = stderr[-60:]
    return status


def _preview_screenshots(workspace: Path) -> list[dict[str, str]]:
    """Screenshots on disk from the most recent preview run, if any."""
    preview_dir = workspace / ".burooj" / "preview"
    if not preview_dir.is_dir():
        return []
    try:
        files = sorted(preview_dir.glob("*.png"))
    except OSError:
        return []
    return [{"path": str(p), "name": p.stem} for p in files]


@method("burooj.build.status")
def _(rid, params: dict) -> dict:
    """Workspace, manifest, dev server health and last verify/preview results."""
    try:
        from tui_gateway.methods_burooj import _preview_screenshots, _resolve_workspace_for_rpc, _workspace_status

        workspace = _resolve_workspace_for_rpc(params, _completion_cwd(params))
        status = _workspace_status(workspace)

        from agent.burooj_status import get_preview, get_verify

        last_verify = get_verify(workspace)
        status["last_verify"] = last_verify
        status["last_preview"] = get_preview(workspace)
        status["screenshots"] = _preview_screenshots(workspace)
        return _ok(rid, status)
    except Exception as exc:
        logger.exception("burooj.build.status failed")
        return _err(rid, 5091, f"burooj.build.status: {exc}")


@method("burooj.build.verify")
def _(rid, params: dict) -> dict:
    """Run the ladder on demand. Records the result for the panel."""
    try:
        from tui_gateway.methods_burooj import _resolve_workspace_for_rpc
        from tools.verify_tool import verify

        raw_rungs = params.get("rungs")
        if raw_rungs is not None and not (
            isinstance(raw_rungs, list) and all(isinstance(r, str) for r in raw_rungs)
        ):
            return _err(rid, 5092, "'rungs' must be an array of rung names.")

        workspace = _resolve_workspace_for_rpc(params, _completion_cwd(params))

        # The shared session caches the manifest it was built with. A panel
        # run is an explicit user request against the workspace as it is now,
        # so pick up any edits since the last tool call.
        from agent.build_session import get_session

        try:
            get_session(workspace, reload_manifest=True)
        except Exception:
            # verify() reports manifest problems itself; just let it run.
            pass

        result = verify(workspace=workspace, rungs=raw_rungs)
        return _ok(rid, result)
    except Exception as exc:
        logger.exception("burooj.build.verify failed")
        return _err(rid, 5093, f"burooj.build.verify: {exc}")


@method("burooj.build.preview")
def _(rid, params: dict) -> dict:
    """Capture route screenshots on demand. Records the result for the panel."""
    try:
        from tui_gateway.methods_burooj import _resolve_workspace_for_rpc
        from tools.preview_tool import preview

        raw_routes = params.get("routes")
        if raw_routes is not None and not (
            isinstance(raw_routes, list) and all(isinstance(r, str) for r in raw_routes)
        ):
            return _err(rid, 5094, "'routes' must be an array of strings.")

        workspace = _resolve_workspace_for_rpc(params, _completion_cwd(params))
        result = preview(workspace=workspace, routes=raw_routes)
        return _ok(rid, result)
    except Exception as exc:
        logger.exception("burooj.build.preview failed")
        return _err(rid, 5095, f"burooj.build.preview: {exc}")


# ── Design panel ────────────────────────────────────────────────────────────


def _flatten_color_tokens(node: Any, prefix: str = "", out: Optional[list[dict]] = None) -> list[dict]:
    """Walk a DTCG token tree and return color tokens as {path, value}.

    Handles the nested ``color.background.default`` shape. Values that are
    aliases (``{color.primary}``) are resolved against the same tree.
    """
    if out is None:
        out = []

    if isinstance(node, dict):
        value = node.get("$value")
        if value is not None and node.get("$type") == "color":
            out.append({"path": prefix, "value": value})
            return out
        for key, child in node.items():
            if key.startswith("$"):
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            _flatten_color_tokens(child, child_prefix, out)
    return out


def _tokens_swatches(workspace: Path) -> dict[str, Any]:
    """Color tokens from burooj.design/tokens.json, aliases resolved."""
    tokens_path = workspace / "burooj.design" / "tokens.json"
    if not tokens_path.is_file():
        return {"status": "skip", "reason": "no burooj.design/tokens.json in this workspace"}
    try:
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": f"Cannot read tokens.json: {exc}"}

    flat = _flatten_color_tokens(tokens)
    by_path = {t["path"]: t["value"] for t in flat}

    def resolve(value: Any) -> Any:
        seen = 0
        while isinstance(value, str) and value.startswith("{") and value.endswith("}") and seen < 8:
            target = value[1:-1]
            if target not in by_path:
                return value
            value = by_path[target]
            seen += 1
        return value

    for token in flat:
        token["value"] = resolve(token["value"])
    return {"status": "pass", "tokens": flat}


@method("burooj.design.status")
def _(rid, params: dict) -> dict:
    """Tokens, contrast pairs, lint violations, baselines and last visual diff.

    ``contrast_check`` and ``design_lint`` are deterministic and cheap, so they
    run live. ``visual_diff`` and ``a11y_check`` boot a browser, so the panel
    gets their last recorded results plus a button to re-run them.
    """
    try:
        from tui_gateway.methods_burooj import _resolve_workspace_for_rpc, _tokens_swatches, _workspace_status
        from tools.contrast_check import contrast_check
        from tools.design_lint import design_lint

        workspace = _resolve_workspace_for_rpc(params, _completion_cwd(params))
        status = _workspace_status(workspace)

        status["tokens"] = _tokens_swatches(workspace)
        status["contrast"] = contrast_check(workspace=workspace)
        status["lint"] = design_lint(workspace=workspace)

        from agent.burooj_status import get_design_checks

        checks = get_design_checks(workspace)
        status["visual_diff"] = checks.get("visual_diff")
        status["a11y_check"] = checks.get("a11y_check")
        return _ok(rid, status)
    except Exception as exc:
        logger.exception("burooj.design.status failed")
        return _err(rid, 5096, f"burooj.design.status: {exc}")


@method("burooj.design.checks")
def _(rid, params: dict) -> dict:
    """Run the four design-gate checks on demand."""
    try:
        from tui_gateway.methods_burooj import _resolve_workspace_for_rpc
        from tools.a11y_check import a11y_check
        from tools.contrast_check import contrast_check
        from tools.design_lint import design_lint
        from tools.visual_diff import visual_diff

        workspace = _resolve_workspace_for_rpc(params, _completion_cwd(params))
        lint = design_lint(workspace=workspace)
        contrast = contrast_check(workspace=workspace)
        a11y = a11y_check(workspace=workspace)
        vdiff = visual_diff(workspace=workspace)

        passing = {lint.get("status"), contrast.get("status"), a11y.get("status"), vdiff.get("status")}
        return _ok(rid, {
            "lint": lint,
            "contrast": contrast,
            "a11y_check": a11y,
            "visual_diff": vdiff,
            "passed": passing <= {"pass", "skip"},
        })
    except Exception as exc:
        logger.exception("burooj.design.checks failed")
        return _err(rid, 5097, f"burooj.design.checks: {exc}")
