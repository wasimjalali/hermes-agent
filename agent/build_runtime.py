"""Runtime seam for Build mode.

A thin abstraction (exec, read, write, ports, snapshot) that wraps the local
backend. A container or cloud backend can drop in later by implementing the
same protocol. The architecture spec (section 4.7) requires this seam to
exist so that Build tools never shell out directly but go through a backend
that can be swapped. Today: :class:`LocalRuntime` and :class:`DockerRuntime`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ExecResult:
    """Result of a command execution."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class PortStatus:
    """Status of a network port."""

    port: int
    listening: bool = False
    pid: Optional[int] = None


@dataclass
class ProcessHandle:
    """Handle to a long-running process managed by the runtime."""

    pid: int
    command: str
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    _process: Any = field(default=None, repr=False)
    _stdout_thread: Any = field(default=None, repr=False)
    _stderr_thread: Any = field(default=None, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    def get_stdout(self, since: int = 0) -> list[str]:
        """Get stdout lines collected since index *since*."""
        with self._lock:
            return self.stdout_lines[since:]

    def get_stderr(self, since: int = 0) -> list[str]:
        """Get stderr lines collected since index *since*."""
        with self._lock:
            return self.stderr_lines[since:]


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGTERM then SIGKILL a process and everything it spawned.

    Both calls target the process group, which only exists because the process
    was started with ``start_new_session=True`` / ``os.setsid``. Killing the
    leader alone leaves the real worker (webpack, tsc, vitest) running.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        delivered = False
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                delivered = True
            except (OSError, ProcessLookupError):
                return
            except Exception:
                # Some environments forbid group signals (restricted
                # sandboxes, and the test suite's live-system guard). Fall
                # back to signalling the leader directly: it kills less, but
                # killing nothing is worse.
                pgid = None
        if not delivered:
            try:
                proc.send_signal(sig)
            except (OSError, ProcessLookupError, ValueError):
                return
            except Exception:
                # Signal delivery refused by the environment. Nothing more we
                # can do, and failing to clean up must not raise into the
                # caller's exec() result.
                return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


class Runtime(ABC):
    """Abstract runtime backend for Build mode.

    **On ``shell=True``.** Manifest commands are shell strings by design
    (``npm run build``, ``npx tsc --noEmit``), so the local backend runs them
    through a shell and cannot switch to argv lists without changing the
    manifest format. The command source, not the invocation, is therefore the
    control that matters: ``agent/build_workspace.py`` bounds which
    ``burooj.build.json`` can be picked up, and ``verify`` surfaces the
    resolved workspace and the exact commands before the first run of a
    session. Never pass model-authored or user-message text through ``exec``;
    it takes manifest commands only.

    Every Build tool operation goes through this interface. The local
    implementation shells out directly. A future container backend would
    send commands over a socket/API instead.
    """

    @abstractmethod
    def exec(
        self,
        command: str,
        cwd: Path,
        timeout: int = 120,
        env: Optional[dict[str, str]] = None,
    ) -> ExecResult:
        """Run a command to completion and return structured output."""
        ...

    @abstractmethod
    def start_process(
        self,
        command: str,
        cwd: Path,
        env: Optional[dict[str, str]] = None,
    ) -> ProcessHandle:
        """Start a long-running process with continuous stdout/stderr capture."""
        ...

    @abstractmethod
    def stop_process(self, handle: ProcessHandle, timeout: int = 5) -> None:
        """Stop a previously started process."""
        ...

    @abstractmethod
    def read(self, path: Path) -> bytes:
        """Read a file's contents."""
        ...

    @abstractmethod
    def write(self, path: Path, content: bytes) -> None:
        """Write content to a file (creates parent dirs)."""
        ...

    @abstractmethod
    def port_status(self, port: int) -> PortStatus:
        """Check whether a port is listening."""
        ...

    @abstractmethod
    def wait_for_port(
        self, port: int, timeout: int = 30, path: str = "/"
    ) -> bool:
        """Wait for a port to accept HTTP connections at *path*."""
        ...

    @abstractmethod
    def snapshot(self, workspace: Path) -> str:
        """Take a workspace snapshot (checkpoint). Returns a snapshot id."""
        ...


class LocalRuntime(Runtime):
    """Local execution backend. Commands run directly on the host."""

    def exec(
        self,
        command: str,
        cwd: Path,
        timeout: int = 120,
        env: Optional[dict[str, str]] = None,
    ) -> ExecResult:
        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        # ``subprocess.run(shell=True, timeout=...)`` kills only the shell on
        # timeout, orphaning the process that actually does the work (a webpack
        # build, a test runner) still holding the workspace. Run the shell in
        # its own process group and kill the whole group instead.
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=run_env,
                start_new_session=True,
            )
        except OSError as exc:
            return ExecResult(exit_code=1, stderr=f"Failed to run: {exc}")

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return ExecResult(
                exit_code=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # Drain whatever was produced before the kill; it usually explains
            # why the command hung.
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                stdout, stderr = "", ""
            message = f"Command timed out after {timeout}s: {command}"
            return ExecResult(
                exit_code=1,
                stdout=stdout or "",
                stderr=(f"{stderr}\n{message}" if stderr else message),
                timed_out=True,
            )
        except OSError as exc:
            _kill_process_group(proc)
            return ExecResult(exit_code=1, stderr=f"Failed to run: {exc}")

    def start_process(
        self,
        command: str,
        cwd: Path,
        env: Optional[dict[str, str]] = None,
    ) -> ProcessHandle:
        run_env = os.environ.copy()
        run_env["BROWSER"] = "none"
        if env:
            run_env.update(env)

        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
            env=run_env,
        )

        handle = ProcessHandle(
            pid=proc.pid,
            command=command,
            _process=proc,
        )

        # Background threads to continuously read stdout/stderr.
        def _reader(pipe: Any, target: list[str], lock: threading.Lock) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    with lock:
                        target.append(line.rstrip("\n"))
                pipe.close()
            except (ValueError, OSError):
                pass

        handle._stdout_thread = threading.Thread(
            target=_reader,
            args=(proc.stdout, handle.stdout_lines, handle._lock),
            daemon=True,
        )
        handle._stderr_thread = threading.Thread(
            target=_reader,
            args=(proc.stderr, handle.stderr_lines, handle._lock),
            daemon=True,
        )
        handle._stdout_thread.start()
        handle._stderr_thread.start()

        return handle

    def stop_process(self, handle: ProcessHandle, timeout: int = 5) -> None:
        proc = handle._process
        if proc is None:
            return
        _kill_process_group(proc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        handle._process = None

    def read(self, path: Path) -> bytes:
        return path.read_bytes()

    def write(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def port_status(self, port: int) -> PortStatus:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(("localhost", port))
            return PortStatus(port=port, listening=(result == 0))
        finally:
            sock.close()

    def wait_for_port(
        self, port: int, timeout: int = 30, path: str = "/"
    ) -> bool:
        import urllib.error
        import urllib.request

        url = f"http://localhost:{port}{path}"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(url, method="HEAD")
                resp = urllib.request.urlopen(req, timeout=2)
                if resp.status < 500:
                    return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.5)

        return False

    def snapshot(self, workspace: Path) -> str:
        """Delegate to Hermes checkpoint_manager.

        Hermes already snapshots before every mutating turn, so the local
        backend forwards to that rather than duplicating it. When the
        checkpoint system is unavailable this raises instead of returning a
        fabricated id: a caller that believes it has a restore point when it
        does not is worse off than one that knows it has none.
        """
        try:
            from tools.checkpoint_manager import CheckpointManager
        except ImportError as exc:
            raise NotImplementedError(
                "LocalRuntime.snapshot requires tools.checkpoint_manager. "
                "Hermes checkpointing already covers mutating turns, so prefer "
                "the agent's own manager over the Runtime seam."
            ) from exc

        manager = CheckpointManager(enabled=True)
        manager.new_turn()
        # ensure_checkpoint returns a bool and never raises; a False means the
        # snapshot did not happen, which the caller must not mistake for one.
        if not manager.ensure_checkpoint(str(workspace), reason="build_runtime.snapshot"):
            raise RuntimeError(
                f"Checkpoint failed for {workspace}. Not returning a snapshot id "
                f"for a snapshot that was not taken."
            )
        return f"checkpoint:{workspace}:{int(time.time())}"


# Module-level singleton. Tools import this.
_runtime: Optional[Runtime] = None


def get_runtime() -> Runtime:
    """Get the active Runtime backend (lazy-initialized to LocalRuntime)."""
    global _runtime
    if _runtime is None:
        _runtime = LocalRuntime()
    return _runtime


def set_runtime(runtime: Runtime) -> None:
    """Swap the runtime backend (for testing or container mode)."""
    global _runtime
    _runtime = runtime


# ── Container backend ───────────────────────────────────────────────────────


class DockerUnavailable(RuntimeError):
    """Raised when the docker CLI cannot reach a daemon."""


@dataclass
class ContainerProcessHandle(ProcessHandle):
    """ProcessHandle for a named container.

    Liveness comes from ``docker inspect`` rather than a Popen, and the
    container id is the handle's identity.
    """

    container_id: str = ""
    _runtime: Any = field(default=None, repr=False)
    _log_proc: Any = field(default=None, repr=False)

    @property
    def is_running(self) -> bool:
        if not self.container_id:
            return False
        check = getattr(self._runtime, "_container_running", None)
        if check is None:
            return False
        return check(self.container_id)


class DockerRuntime(Runtime):
    """Container execution backend via the docker CLI (arm's-length API).

    Runs every Build operation inside a container built from a Node image.
    The workspace is bind-mounted at its own absolute path, so manifest
    commands, relative paths and read/write paths behave exactly as they do
    on the local backend: no tool file needs to know a container is involved.

    **Port model.** The dev server's port travels through the environment as
    ``PORT`` (the BuildSession sets it from the manifest). The container
    publishes ``127.0.0.1:<PORT>:<PORT>`` so ``port_status`` and
    ``wait_for_port`` on the host observe the same port the manifest names.
    When ``PORT`` is absent no port is published and the server is only
    reachable inside the container.

    **Deliberately not a wrapper library.** This shells out to the ``docker``
    CLI. Daytona (AGPL-3.0) and WebContainers (commercial license) are
    excluded by the architecture spec; a CLI is the arm's-length API.

    Parameters
    ----------
    image : str, optional
        Container image for Build work. Defaults to the ``BUROOJ_RUNTIME_IMAGE``
        env var or ``node:22-bookworm-slim`` (Node 22 + npm, the pinned stack's
        runtime). Must contain a POSIX shell and whatever the manifest commands
        need.
    docker_cmd : str, optional
        Path to the docker CLI. Defaults to ``docker``.
    """

    def __init__(
        self,
        image: Optional[str] = None,
        docker_cmd: str = "docker",
    ) -> None:
        self.image = image or os.environ.get(
            "BUROOJ_RUNTIME_IMAGE", "node:22-bookworm-slim"
        )
        self.docker_cmd = docker_cmd
        self._available: Optional[bool] = None
        self._availability_lock = threading.Lock()

    # ── docker plumbing ────────────────────────────────────────────────────

    def _ensure_available(self) -> None:
        """Verify the docker CLI exists and a daemon answers. Fail loud."""
        if self._available:
            return
        with self._availability_lock:
            if self._available:
                return
            try:
                proc = subprocess.run(
                    [self.docker_cmd, "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DockerUnavailable(
                    f"docker CLI unavailable ({exc}). Install Docker and start "
                    f"the daemon to use the container Build backend."
                ) from exc
            if proc.returncode != 0 or not proc.stdout.strip():
                raise DockerUnavailable(
                    f"docker daemon not reachable: {proc.stderr.strip() or proc.stdout.strip()}"
                )
            self._available = True

    def _run_docker(
        self,
        args: list[str],
        timeout: int = 120,
    ) -> subprocess.CompletedProcess:
        self._ensure_available()
        try:
            return subprocess.run(
                [self.docker_cmd, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerUnavailable(
                f"docker {args[0]} timed out after {timeout}s: "
                f"{exc.stderr or exc.stdout or ''}"
            ) from exc

    def _run_args(
        self,
        cwd: Path,
        env: Optional[dict[str, str]],
    ) -> list[str]:
        """Shared ``docker run`` flags: mount the workspace, publish PORT.

        Returns the flags after ``docker run``; callers prepend ``run`` and
        any name/label flags they need.
        """
        args = [
            "--rm", "--init",
            "-v", f"{cwd}:{cwd}",
            "-w", str(cwd),
        ]
        port = (env or {}).get("PORT")
        if port:
            try:
                int(port)
                args += ["-p", f"127.0.0.1:{port}:{port}"]
            except ValueError:
                pass  # malformed PORT: container-internal only, like no PORT
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args += [self.image]
        return args

    # ── Runtime protocol ───────────────────────────────────────────────────

    def exec(
        self,
        command: str,
        cwd: Path,
        timeout: int = 120,
        env: Optional[dict[str, str]] = None,
    ) -> ExecResult:
        """Run *command* to completion inside a throwaway container."""
        self._ensure_available()
        name = self._container_label(cwd, command)
        # A stale container from a timed-out run with the same command must
        # not fail the next run with "name already in use". The rm is
        # best-effort: availability was already proven above.
        try:
            self._run_docker(["rm", "-f", name], timeout=30)
        except (DockerUnavailable, OSError):
            pass
        args = ["run", "--name", name]
        args += self._run_args(cwd, env)
        args += ["sh", "-lc", command]
        try:
            proc = subprocess.run(
                [self.docker_cmd, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecResult(
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            # ``docker run --rm`` would orphan the container on CLI timeout.
            # Kill the named container; --rm removes it afterwards.
            self._kill_matching_containers(cwd, command)
            return ExecResult(
                exit_code=1,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + f"\nCommand timed out after {timeout}s: {command}",
                timed_out=True,
            )

    def _container_label(self, cwd: Path, command: str) -> str:
        """Deterministic container label for one (workspace, command) pair."""
        import hashlib

        digest = hashlib.sha256(f"{cwd}\0{command}".encode()).hexdigest()[:12]
        return f"burooj-{digest}"

    def _kill_matching_containers(self, cwd: Path, command: str) -> None:
        """Kill and remove any container running this exact command.

        Used on exec timeout, where the CLI died but the container did not.
        Matching on the label keeps cleanup surgical: only this workspace's
        instance of this command is touched.
        """
        label = self._container_label(cwd, command)
        try:
            self._run_docker(["rm", "-f", label], timeout=30)
        except (DockerUnavailable, OSError):
            pass  # cleanup must not mask the timeout result

    def start_process(
        self,
        command: str,
        cwd: Path,
        env: Optional[dict[str, str]] = None,
    ) -> ProcessHandle:
        """Start *command* as a named, long-running container.

        The container gets a deterministic name per (workspace, command) so a
        duplicate start replaces the previous one instead of leaking a second
        server on the same published port.
        """
        self._ensure_available()
        name = self._container_label(cwd, command)
        args = ["run", "-d", "--rm", "--init", "--name", name]
        args += self._run_args(cwd, env)
        args += ["sh", "-lc", command]

        # Remove a stale container with the same name first, so a restarted
        # dev server does not fail with "name already in use".
        try:
            self._run_docker(["rm", "-f", name], timeout=30)
        except (DockerUnavailable, OSError):
            pass

        proc = self._run_docker(args, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run failed for '{command}': {proc.stderr.strip() or proc.stdout.strip()}"
            )

        handle = ContainerProcessHandle(
            pid=0, command=command, _process=None, container_id=name, _runtime=self
        )

        # One docker logs -f process. Drain stdout and stderr of that CLI
        # process on separate threads so a chatty stream cannot fill a pipe
        # buffer and block. docker logs merges container streams onto its
        # own stdout by default; stderr is the CLI's diagnostic stream.
        # Starting two docker logs processes was the old bug: every line
        # landed twice and one stderr pipe was never read.
        try:
            log_proc = subprocess.Popen(
                [self.docker_cmd, "logs", "-f", "--tail", "0", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(f"docker logs failed to start for {name}: {exc}") from exc
        handle._log_proc = log_proc

        def _drain(stream, target: list[str], lock: threading.Lock) -> None:
            if stream is None:
                return
            try:
                for line in iter(stream.readline, ""):
                    with lock:
                        target.append(line.rstrip("\n"))
                stream.close()
            except (ValueError, OSError, AttributeError):
                pass

        handle._stdout_thread = threading.Thread(
            target=_drain,
            args=(log_proc.stdout, handle.stdout_lines, handle._lock),
            daemon=True,
        )
        handle._stderr_thread = threading.Thread(
            target=_drain,
            args=(log_proc.stderr, handle.stderr_lines, handle._lock),
            daemon=True,
        )
        handle._stdout_thread.start()
        handle._stderr_thread.start()
        return handle

    def _container_running(self, name: str) -> bool:
        try:
            proc = self._run_docker(
                ["inspect", "--format", "{{.State.Running}}", name], timeout=30
            )
        except (DockerUnavailable, OSError):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def stop_process(self, handle: ProcessHandle, timeout: int = 5) -> None:
        name = getattr(handle, "container_id", None)
        log_proc = getattr(handle, "_log_proc", None)
        if log_proc is not None:
            try:
                log_proc.terminate()
            except (OSError, AttributeError):
                pass
        if not name:
            return
        try:
            self._run_docker(["rm", "-f", name], timeout=timeout)
        except (DockerUnavailable, OSError, subprocess.TimeoutExpired):
            pass

    def read(self, path: Path) -> bytes:
        """Read a file inside the container for a workspace path.

        Uses a throwaway container with the file's parent bind-mounted, so the
        container's view is authoritative. This is the honest container
        semantics: the bind mount makes host and container identical, but
        reading through the container is what a future remote backend would do.
        """
        self._ensure_available()
        name = f"burooj-read-{uuid.uuid4().hex[:8]}"
        proc = self._run_docker(
            [
                "run", "--name", name, "--rm",
                "-v", f"{path.parent}:{path.parent}",
                self.image,
                "cat", str(path),
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            raise FileNotFoundError(str(path))
        return proc.stdout.encode()

    def write(self, path: Path, content: bytes) -> None:
        """Write a file inside the container for a workspace path."""
        self._ensure_available()
        # docker exec cannot take stdin from a completed subprocess easily;
        # write via a heredoc-free sh -c with base64 to stay byte-exact.
        import base64

        encoded = base64.b64encode(content).decode()
        name = f"burooj-write-{uuid.uuid4().hex[:8]}"
        proc = self._run_docker(
            [
                "run", "--name", name, "--rm",
                "-v", f"{path.parent}:{path.parent}",
                self.image,
                "sh", "-lc",
                f"mkdir -p {path.parent} && echo {encoded} | base64 -d > {path}",
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            raise OSError(f"docker write failed for {path}: {proc.stderr.strip()}")

    def port_status(self, port: int) -> PortStatus:
        """Check the published host port. The container publishes
        ``127.0.0.1:<port>:<port>``, so the host probe sees the real server."""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(("localhost", port))
            return PortStatus(port=port, listening=(result == 0))
        finally:
            sock.close()

    def wait_for_port(
        self, port: int, timeout: int = 30, path: str = "/"
    ) -> bool:
        import urllib.error
        import urllib.request

        url = f"http://localhost:{port}{path}"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(url, method="HEAD")
                resp = urllib.request.urlopen(req, timeout=2)
                if resp.status < 500:
                    return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.5)

        return False

    def snapshot(self, workspace: Path) -> str:
        raise NotImplementedError(
            "DockerRuntime.snapshot is not implemented. Container snapshots "
            "need a checkpoint protocol over the docker API; the local "
            "checkpoint_manager covers host-side workspaces only."
        )
