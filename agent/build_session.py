"""Per-workspace Build session: one workspace, one manifest, one dev server.

Before this existed, ``verify``'s render rung, ``preview``, ``a11y_check`` and
``visual_diff`` each booted and killed their own dev server. A single full
``verify()`` therefore paid three cold Next.js starts, each racing the previous
one's port teardown, and none of them could tell "my server is up" from
"something else is already on port 3000".

A ``BuildSession`` boots the server at most once and hands the same handle to
every caller. Sessions are cached per resolved workspace path, so tools that
never see each other still share the process.

The session also owns the *foreign server* check. ``wait_for_port`` only probes
localhost, so a stale dev server from a previous run would happily answer for a
completely different application and every downstream check would silently
verify the wrong thing. :meth:`BuildSession.ensure_server` refuses to proceed in
that case rather than guessing.
"""

from __future__ import annotations

import atexit
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.build_manifest import BuildManifest, load_manifest
from agent.build_runtime import ProcessHandle, get_runtime

logger = logging.getLogger("hermes.build_session")

# How long to wait for a dev server we started to answer.
DEFAULT_SERVER_TIMEOUT = 45


@dataclass
class ServerStatus:
    """Outcome of an :meth:`BuildSession.ensure_server` call."""

    ready: bool
    base_url: str = ""
    reason: str = ""
    foreign: bool = False
    reused: bool = False
    stdout: list[str] = None  # type: ignore[assignment]
    stderr: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stdout is None:
            self.stdout = []
        if self.stderr is None:
            self.stderr = []


class BuildSession:
    """Workspace + manifest + at most one dev server process."""

    def __init__(self, workspace: Path, manifest: BuildManifest) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self._handle: Optional[ProcessHandle] = None
        self._lock = threading.Lock()

    # ── dev server ──────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.manifest.dev.port}"

    @property
    def server_running(self) -> bool:
        return self._handle is not None and self._handle.is_running

    def ensure_server(
        self, timeout: int = DEFAULT_SERVER_TIMEOUT
    ) -> ServerStatus:
        """Boot the dev server if it is not already up, and wait for it.

        Reuses an already-running server owned by this session. Refuses to use
        a server this session did not start, because it cannot be assumed to be
        serving the workspace under test.
        """
        with self._lock:
            runtime = get_runtime()
            port = self.manifest.dev.port

            # Already ours and alive: reuse, no boot cost.
            if self._handle is not None and self._handle.is_running:
                return ServerStatus(
                    ready=True,
                    base_url=self.base_url,
                    reused=True,
                    stdout=self._handle.get_stdout(),
                    stderr=self._handle.get_stderr(),
                )

            # Someone else is on the port. Do not verify against an unknown app.
            if self._handle is None and runtime.port_status(port).listening:
                return ServerStatus(
                    ready=False,
                    foreign=True,
                    reason=(
                        f"Port {port} is already in use by a process this session "
                        f"did not start. Stop it, or change 'dev.port' in "
                        f"burooj.build.json. Refusing to verify against an "
                        f"unknown server."
                    ),
                )

            # Our previous handle died. Surface why before restarting.
            prior_stderr: list[str] = []
            if self._handle is not None:
                prior_stderr = self._handle.get_stderr()[-20:]
                self._handle = None

            handle = runtime.start_process(
                command=self.manifest.dev.command,
                cwd=self.workspace,
                env={"PORT": str(port)},
            )
            self._handle = handle

            ready = runtime.wait_for_port(
                port=port, timeout=timeout, path=self.manifest.dev.ready
            )
            if not ready:
                stderr = handle.get_stderr()
                reason = f"Dev server did not become ready within {timeout}s"
                if prior_stderr:
                    reason += " (a previous dev server for this session exited)"
                return ServerStatus(
                    ready=False,
                    reason=reason,
                    stdout=handle.get_stdout()[-50:],
                    stderr=(prior_stderr + stderr)[-50:],
                )

            return ServerStatus(
                ready=True,
                base_url=self.base_url,
                stdout=handle.get_stdout(),
                stderr=handle.get_stderr(),
            )

    def server_output(self) -> tuple[list[str], list[str]]:
        """Return (stdout, stderr) captured from the dev server so far."""
        if self._handle is None:
            return ([], [])
        return (self._handle.get_stdout(), self._handle.get_stderr())

    def stop_server(self) -> None:
        """Stop the dev server if this session started one."""
        with self._lock:
            if self._handle is None:
                return
            try:
                get_runtime().stop_process(self._handle)
            finally:
                self._handle = None


# ── Session cache ───────────────────────────────────────────────────────────

_sessions: dict[str, BuildSession] = {}
_sessions_lock = threading.Lock()


def get_session(workspace: Path, *, reload_manifest: bool = False) -> BuildSession:
    """Return the shared :class:`BuildSession` for *workspace*.

    Raises :class:`~agent.build_manifest.ManifestError` when the workspace has
    no valid ``burooj.build.json``.
    """
    key = str(Path(workspace).resolve())
    with _sessions_lock:
        existing = _sessions.get(key)
        if existing is not None and not reload_manifest:
            return existing
        manifest = load_manifest(Path(key))
        if existing is not None:
            existing.manifest = manifest
            return existing
        session = BuildSession(Path(key), manifest)
        _sessions[key] = session
        return session


def stop_all_sessions() -> None:
    """Stop every dev server this process started. Used on shutdown and tests."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        try:
            session.stop_server()
        except Exception:  # never let cleanup mask the real error
            logger.debug("Failed to stop dev server for %s", session.workspace)


# A dev server that outlives the interpreter holds its port, and the next run's
# ensure_server correctly refuses to verify against a server it did not start.
# Registering here rather than at a call site means every entry point that
# reaches a session (verify, preview, a11y_check, visual_diff) is covered.
atexit.register(stop_all_sessions)
