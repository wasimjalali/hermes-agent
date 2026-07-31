"""verify tool - runs the Build mode verification ladder.

Ladder order (cheap to expensive, fail-fast):
0. install - dependencies present
1. typecheck - zero errors
2. lint - clean
3. fix - tests that must flip from failing to passing
4. guard - tests that must stay passing
5. build - succeeds
6. render - dev server up, routes respond, zero console errors
7. design_gate - design_lint, contrast_check, a11y_check, visual_diff

Tool schema:
    verify(rungs: list[str] | None = None) -> { results: list[Rung], passed: bool }

Each Rung: { name, status: "pass"|"fail"|"skip"|"error", output, duration_ms, hint }

**On status semantics.** ``skip`` means the project declares no such step and
nothing was checked. ``error`` means the check could not run, which is never a
pass: a gate that cannot execute must not report green. Only ``pass`` and
``skip`` leave the ladder passing.
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent.build_manifest import BuildManifest, ManifestError
from agent.build_session import BuildSession, get_session
from agent.build_runtime import get_runtime
from agent.build_workspace import resolve_workspace
from tools.registry import registry

logger = logging.getLogger("hermes.verify_tool")

# All ladder rungs in canonical order.
LADDER_RUNGS = (
    "install", "typecheck", "lint", "fix", "guard", "build", "render", "design_gate",
)

# Per-command timeouts (seconds). A production Next.js build routinely exceeds
# the two minutes the first cut allowed, and a timeout reads as a failure the
# user then has to debug, so these are generous where the work is genuinely slow.
_RUNG_TIMEOUTS = {
    "install": 600,
    "typecheck": 300,
    "lint": 300,
    "fix": 600,
    "guard": 900,
    "build": 900,
}
_DEFAULT_RUNG_TIMEOUT = 300

# How long to wait for the dev server during the render rung.
_RENDER_SERVER_TIMEOUT = 45

# Sessions that have already shown the user their resolved workspace and
# commands. Keyed by resolved workspace path.
_disclosed: set[str] = set()


@dataclass
class RungResult:
    """Result of a single verification rung."""

    name: str
    status: str  # "pass" | "fail" | "skip" | "error"
    output: str = ""
    duration_ms: int = 0
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "output": self.output,
            "duration_ms": self.duration_ms,
        }
        if self.hint:
            data["hint"] = self.hint
        return data


# Statuses that keep the ladder green. "error" is deliberately absent.
_PASSING = frozenset({"pass", "skip"})

# What to tell the user when a rung fails, per rung.
_HINTS = {
    "install": "Dependencies did not install. Check the lockfile is in sync with package.json.",
    "typecheck": "Fix the type errors above, then re-run verify.",
    "lint": "Run the lint command with --fix where the rule supports it.",
    "fix": "The tests that are supposed to prove this change still fail.",
    "guard": "This change broke a test that was passing. Check the diff against the failure.",
    "build": "A production build failure that typecheck missed is usually an import or env issue.",
    "render": "The app compiles but does not serve. Check the dev server output above.",
    "design_gate": "The app works but does not follow the design system. See the per-check detail above.",
}


def _combine_output(stdout: str, stderr: str, limit: int = 8000) -> str:
    """Merge stdout and stderr into one trimmed blob, newest detail kept."""
    parts = [p for p in (stdout or "", stderr or "") if p.strip()]
    text = "\n".join(parts).strip()
    if len(text) > limit:
        text = "... (output truncated)\n" + text[-limit:]
    return text


def _run_command_rung(
    name: str, command: str, workspace: Path, skip_reason: str = ""
) -> RungResult:
    """Run one shell-command rung and classify the result."""
    if not command:
        return RungResult(
            name=name,
            status="skip",
            output=skip_reason or f"No '{name}' command in burooj.build.json",
        )

    start = time.monotonic()
    result = get_runtime().exec(
        command, workspace, timeout=_RUNG_TIMEOUTS.get(name, _DEFAULT_RUNG_TIMEOUT)
    )
    duration = int((time.monotonic() - start) * 1000)
    output = _combine_output(result.stdout, result.stderr)

    if result.timed_out:
        return RungResult(
            name=name,
            status="error",
            output=output,
            duration_ms=duration,
            hint=f"'{command}' did not finish. Raise the timeout or make the step cheaper.",
        )

    status = "pass" if result.ok else "fail"
    return RungResult(
        name=name,
        status=status,
        output=output,
        duration_ms=duration,
        hint=_HINTS.get(name, "") if status == "fail" else "",
    )


def _run_rung_install(manifest: BuildManifest, session: BuildSession) -> RungResult:
    return _run_command_rung("install", manifest.install, session.workspace)


def _run_rung_typecheck(manifest: BuildManifest, session: BuildSession) -> RungResult:
    return _run_command_rung("typecheck", manifest.typecheck, session.workspace)


def _run_rung_lint(manifest: BuildManifest, session: BuildSession) -> RungResult:
    return _run_command_rung("lint", manifest.lint, session.workspace)


def _test_command(manifest: BuildManifest, targets: list[str]) -> str:
    """Build a test command for *targets*, quoting each path.

    Unquoted interpolation broke on any path containing a space and made the
    command a splicing point for whatever ended up in the manifest's test lists.
    """
    base = manifest.test.command
    if not targets:
        return base
    quoted = " ".join(shlex.quote(t) for t in targets)
    separator = "" if "--" in base else " --"
    return f"{base}{separator} {quoted}"


def _run_rung_fix(manifest: BuildManifest, session: BuildSession) -> RungResult:
    """Run the fix tests (must pass to prove the change works)."""
    if manifest.test is None:
        return RungResult(name="fix", status="skip", output="No 'test' section in burooj.build.json")
    if not manifest.test.fix:
        return RungResult(
            name="fix",
            status="skip",
            output=(
                "No fix tests declared. Add 'test.fix' with the tests that prove "
                "this change, so the ladder can tell a real fix from a no-op."
            ),
        )
    return _run_command_rung(
        "fix", _test_command(manifest, manifest.test.fix), session.workspace
    )


def _run_rung_guard(manifest: BuildManifest, session: BuildSession) -> RungResult:
    """Run the guard tests (must stay passing to prove nothing broke).

    When no guard list is declared the whole suite is the guard, which is the
    right default, but only when the project actually has a test command.
    """
    if manifest.test is None:
        return RungResult(name="guard", status="skip", output="No 'test' section in burooj.build.json")
    return _run_command_rung(
        "guard", _test_command(manifest, manifest.test.guard), session.workspace
    )


def _run_rung_build(manifest: BuildManifest, session: BuildSession) -> RungResult:
    return _run_command_rung("build", manifest.build, session.workspace)


# Dev-server log lines that mention "error" but are routine. Matched
# case-insensitively as substrings, so each entry must be specific enough not
# to appear inside an unrelated word: "ready in" was originally on this list
# and silently matched "address already in use", which hid EADDRINUSE.
_BENIGN_SERVER_PATTERNS = (
    "deprecationwarning", "experimentalwarning",
    "0 errors", "no errors", "found 0 error", "error-free",
    "without errors", "errors: 0",
)

# Lines that genuinely indicate the server is broken.
_FATAL_SERVER_PATTERNS = (
    "eaddrinuse", "cannot find module", "module not found",
    "unhandledpromiserejection", "fatal error", "segmentation fault",
    "econnrefused", "heap out of memory",
)


def _server_problems(lines: list[str]) -> list[str]:
    """Return dev-server log lines that indicate a real failure.

    The first cut flagged any line containing "error", which meant a build
    reporting "compiled with 0 errors" failed the rung. Match known-fatal
    patterns instead, and treat a bare "error" as a problem only when the line
    is not on the benign list.
    """
    problems: list[str] = []
    for line in lines:
        low = line.lower()
        if any(p in low for p in _BENIGN_SERVER_PATTERNS):
            continue
        if any(p in low for p in _FATAL_SERVER_PATTERNS):
            problems.append(line)
            continue
        if "error" in low and not low.lstrip().startswith(("warn", "info", "debug")):
            problems.append(line)
    return problems


def _run_rung_render(manifest: BuildManifest, session: BuildSession) -> RungResult:
    """Boot the shared dev server, check every route responds, check for crashes."""
    import urllib.error
    import urllib.request

    start = time.monotonic()

    if manifest.dev is None:
        return RungResult(
            name="render", status="skip", output="No 'dev' section in burooj.build.json"
        )

    status = session.ensure_server(timeout=_RENDER_SERVER_TIMEOUT)
    duration = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731

    if not status.ready:
        output = status.reason
        if status.stderr:
            output += "\nServer stderr:\n" + "\n".join(status.stderr[-20:])
        return RungResult(
            name="render",
            status="error" if status.foreign else "fail",
            output=output,
            duration_ms=duration(),
            hint=(
                "Free the port or change dev.port, then re-run."
                if status.foreign
                else _HINTS["render"]
            ),
        )

    failures: list[str] = []
    for route in manifest.routes:
        url = f"{status.base_url}{route}"
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=15
            )
            if resp.status >= 400:
                failures.append(f"{route}: HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            failures.append(f"{route}: HTTP {exc.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            failures.append(f"{route}: {exc}")

    _, stderr_lines = session.server_output()
    problems = _server_problems(stderr_lines)

    if failures or problems:
        output_parts = []
        if failures:
            output_parts.append("Route failures:\n" + "\n".join(failures))
        if problems:
            output_parts.append("Server errors:\n" + "\n".join(problems[-10:]))
        return RungResult(
            name="render",
            status="fail",
            output="\n".join(output_parts),
            duration_ms=duration(),
            hint=_HINTS["render"],
        )

    note = f"All {len(manifest.routes)} route(s) responded OK"
    if status.reused:
        note += " (reused the running dev server)"
    return RungResult(name="render", status="pass", output=note, duration_ms=duration())


def _run_rung_design_gate(manifest: BuildManifest, session: BuildSession) -> RungResult:
    """Run the design gate: design_lint, contrast_check, a11y_check, visual_diff.

    All four run independently (no fail-fast within the gate) because they are
    independent signals and a designer wants the whole picture at once. A check
    that errors fails the gate; a check that legitimately has nothing to do
    skips without failing it.
    """
    start = time.monotonic()
    workspace = session.workspace

    from tools.a11y_check import a11y_check
    from tools.contrast_check import contrast_check
    from tools.design_lint import design_lint
    from tools.visual_diff import visual_diff

    lines: list[str] = []
    gate_passed = True

    def record(label: str, result: dict[str, Any], detail: Callable[[], list[str]]) -> None:
        nonlocal gate_passed
        status = result.get("status") or ("pass" if result.get("passed") else "fail")
        if status == "skip":
            lines.append(f"{label}: SKIP ({result.get('reason', 'nothing to check')})")
            return
        if status == "error":
            gate_passed = False
            lines.append(f"{label}: ERROR ({result.get('error', 'check could not run')})")
            return
        if status != "pass":
            gate_passed = False
            lines.extend(detail())
            return
        lines.append(f"{label}: PASS ({result.get('summary', 'ok')})")

    lint_result = design_lint(workspace=workspace)

    def lint_detail() -> list[str]:
        violations = lint_result.get("violations", [])
        out = [f"design_lint: FAIL ({len(violations)} violation(s))"]
        out += [
            f"  {v['file']}:{v['line']} [{v['rule']}] {v['value']}"
            for v in violations[:5]
        ]
        if len(violations) > 5:
            out.append(f"  ... and {len(violations) - 5} more")
        return out

    record("design_lint", lint_result, lint_detail)

    contrast_result = contrast_check(workspace=workspace)

    def contrast_detail() -> list[str]:
        failing = [p for p in contrast_result.get("pairs", []) if not p["passed"]]
        out = [f"contrast_check: FAIL ({len(failing)} pair(s) below threshold)"]
        out += [
            f"  {p['foreground']} on {p['background']}: {p['ratio']}:1 (need {p['required']}:1)"
            for p in failing[:5]
        ]
        return out

    record("contrast_check", contrast_result, contrast_detail)

    a11y_result = a11y_check(workspace=workspace)

    def a11y_detail() -> list[str]:
        total = sum(r.get("error_count", 0) for r in a11y_result.get("routes", []))
        out = [f"a11y_check: FAIL ({total} critical/serious violation(s))"]
        for route in a11y_result.get("routes", []):
            for v in route.get("violations", [])[:3]:
                if v.get("impact") in ("critical", "serious"):
                    out.append(f"  [{v['impact']}] {v['id']}: {v['description']}")
        return out

    record("a11y_check", a11y_result, a11y_detail)

    vdiff_result = visual_diff(workspace=workspace)

    def vdiff_detail() -> list[str]:
        failing = [r for r in vdiff_result.get("routes", []) if not r["passed"]]
        out = [f"visual_diff: FAIL ({len(failing)} route(s) drifted)"]
        out += [
            f"  {r['path']}: {r['diff_pct']}% changed (threshold {r['threshold']}%)"
            for r in failing[:3]
        ]
        return out

    record("visual_diff", vdiff_result, vdiff_detail)

    # Rung 5 of the design gate: VLM critique. Deliberately advisory and
    # opt-in. Vision models cost money, leave the machine, and are not
    # reproducible; a full ladder must not call them by default. Enable with
    # config ``burooj.vlm_critique: true`` or env ``BUROOJ_VLM_CRITIQUE=1``.
    vlm_result = (
        _run_rung_vlm_advisory(session)
        if _vlm_critique_enabled()
        else []
    )

    duration = int((time.monotonic() - start) * 1000)
    return RungResult(
        name="design_gate",
        status="pass" if gate_passed else "fail",
        output="\n".join([*lines, *vlm_result]),
        duration_ms=duration,
        hint=_HINTS["design_gate"] if not gate_passed else "",
    )


def _vlm_critique_enabled() -> bool:
    """Whether the advisory VLM critique runs inside the design gate.

    Off by default. Opt in via ``BUROOJ_VLM_CRITIQUE=1`` or
    ``burooj.vlm_critique: true`` in config.yaml.
    """
    import os

    env = os.environ.get("BUROOJ_VLM_CRITIQUE", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        flag = (cfg.get("burooj") or {}).get("vlm_critique")
        return bool(flag)
    except Exception:
        return False


def _run_rung_vlm_advisory(session: BuildSession) -> list[str]:
    """Run the VLM critique as an advisory report. Never fails the gate.

    Returns output lines. A failing, erroring or crashing critique is still
    just a report: the ladder must not behave differently on identical input
    because a non-deterministic model had a bad run.
    """
    try:
        from tools.vlm_critique import vlm_critique

        result = vlm_critique(workspace=session.workspace)
    except Exception as exc:
        return [
            f"vlm_critique: ADVISORY (could not run: {type(exc).__name__}: {exc})"
        ]

    status = result.get("status", "error")
    if status == "skip":
        return [f"vlm_critique: ADVISORY (skip: {result.get('reason', 'nothing to review')})"]
    if status == "error":
        return [f"vlm_critique: ADVISORY (error: {result.get('error', 'could not run')})"]

    total_issues = sum(
        len(r.get("issues", [])) for r in result.get("routes", [])
    )
    lines = [f"vlm_critique: ADVISORY ({total_issues} issue(s) reported, non-blocking)"]
    for route in result.get("routes", []):
        if route.get("error"):
            lines.append(f"  {route['path']}: {route['error']}")
            continue
        for issue in route.get("issues", [])[:3]:
            lines.append(
                f"  {route['path']} [{issue.get('severity', 'minor')}] "
                f"{issue.get('point', '')}"
            )
    return lines


_RUNG_RUNNERS: dict[str, Callable[[BuildManifest, BuildSession], RungResult]] = {
    "install": _run_rung_install,
    "typecheck": _run_rung_typecheck,
    "lint": _run_rung_lint,
    "fix": _run_rung_fix,
    "guard": _run_rung_guard,
    "build": _run_rung_build,
    "render": _run_rung_render,
    "design_gate": _run_rung_design_gate,
}


def _disclosure(session: BuildSession) -> Optional[dict[str, Any]]:
    """Return the workspace/command disclosure, once per workspace per process.

    The manifest supplies shell commands that this tool runs. The first verify
    of a session states plainly which manifest was picked and what it will
    execute, so a manifest the user did not write cannot run silently.
    """
    key = str(session.workspace)
    if key in _disclosed:
        return None
    _disclosed.add(key)
    m = session.manifest
    commands = {
        k: v
        for k, v in (
            ("install", m.install),
            ("typecheck", m.typecheck),
            ("lint", m.lint),
            ("test", m.test.command if m.test else ""),
            ("build", m.build),
            ("dev", m.dev.command if m.dev else ""),
        )
        if v
    }
    return {
        "workspace": key,
        "manifest": str(session.workspace / "burooj.build.json"),
        "commands": commands,
        "note": "First verify in this workspace. These commands run on your machine.",
    }


def verify(
    workspace: Optional[Path] = None,
    rungs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run the verification ladder on the workspace.

    Parameters
    ----------
    workspace : Path, optional
        Workspace root. Resolved via build_workspace if not provided.
    rungs : list[str], optional
        Specific rungs to run. When None, runs all in canonical order, stopping
        at the first failure.

    Returns
    -------
    dict with keys ``results``, ``passed``, and on the first call per workspace
    a ``disclosure`` describing what is about to be executed.
    """
    if workspace is None:
        workspace = resolve_workspace()

    try:
        session = get_session(Path(workspace))
    except ManifestError as exc:
        return {
            "results": [
                RungResult(
                    name="manifest",
                    status="error",
                    output=str(exc),
                    hint="Create burooj.build.json at the workspace root. The "
                         "scaffold-app skill ships a template.",
                ).to_dict()
            ],
            "passed": False,
        }

    manifest = session.manifest

    # Resolve the rung selection. An unknown name is an error, never a silent
    # drop: dropping it made verify report a green ladder having run nothing.
    if rungs is not None:
        unknown = [r for r in rungs if r not in _RUNG_RUNNERS]
        if unknown:
            return {
                "results": [
                    RungResult(
                        name="selection",
                        status="error",
                        output=(
                            f"Unknown rung(s): {', '.join(unknown)}. "
                            f"Valid rungs: {', '.join(LADDER_RUNGS)}"
                        ),
                    ).to_dict()
                ],
                "passed": False,
            }
        if not rungs:
            return {
                "results": [
                    RungResult(
                        name="selection",
                        status="error",
                        output="No rungs selected. Pass rung names, or omit the "
                               "argument to run the whole ladder.",
                    ).to_dict()
                ],
                "passed": False,
            }
        # Run in canonical ladder order regardless of the order requested.
        selected = [r for r in LADDER_RUNGS if r in set(rungs)]
    else:
        selected = list(LADDER_RUNGS)

    response: dict[str, Any] = {}
    if (disclosure := _disclosure(session)) is not None:
        response["disclosure"] = disclosure

    results: list[dict[str, Any]] = []
    all_passed = True

    try:
        for rung_name in selected:
            result = _RUNG_RUNNERS[rung_name](manifest, session)
            results.append(result.to_dict())

            if result.status not in _PASSING:
                all_passed = False
                # Fail-fast only when running the full ladder. An explicit
                # selection runs everything the caller asked for.
                if rungs is None:
                    break
    finally:
        # The dev server outlives a single verify only while the session is
        # alive; nothing here leaks a process past interpreter exit because
        # start_process runs in its own process group and stop_all_sessions
        # is the shutdown path.
        pass

    response["results"] = results
    response["passed"] = all_passed

    # The desktop Build panel shows the last run. Record it here so a
    # model-driven verify inside a session is visible to the panel.
    from agent.burooj_status import record_verify

    record_verify(session.workspace, response)

    return response


# ── Tool registration ───────────────────────────────────────────────────────

VERIFY_SCHEMA = {
    "name": "verify",
    "description": (
        "Run the Burooj Build verification ladder on the current workspace: "
        "install, typecheck, lint, fix tests, guard tests, build, render, "
        "design gate. Returns a structured pass/fail per rung. Call this after "
        "every edit. Work is not done until the ladder is green; compiling is "
        "rung 5 of 8."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rungs": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(LADDER_RUNGS),
                },
                "description": (
                    "Optional subset of rungs to run. Omit to run the whole "
                    "ladder fail-fast. Use a subset to re-check one thing fast."
                ),
            },
        },
        "required": [],
    },
}


def handle_verify(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for the verify tool."""
    raw = args.get("rungs")
    if raw is not None and not isinstance(raw, list):
        return json.dumps({"error": "'rungs' must be an array of rung names."})
    if raw is not None and not all(isinstance(r, str) for r in raw):
        return json.dumps({"error": "'rungs' entries must be strings."})
    try:
        result = verify(rungs=raw)
    except Exception as exc:  # a gate that crashes must say so, not vanish
        logger.exception("verify failed")
        return json.dumps({"error": f"verify crashed: {type(exc).__name__}: {exc}", "passed": False})
    return json.dumps(result, indent=2)


registry.register(
    name="verify",
    toolset="burooj_build",
    schema=VERIFY_SCHEMA,
    handler=handle_verify,
    emoji="🪜",
)
