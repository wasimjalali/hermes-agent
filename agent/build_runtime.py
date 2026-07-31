"""Runtime seam for Build mode.

A thin abstraction (exec, read, write, ports, snapshot) that wraps the local
backend. A container or cloud backend can drop in later by implementing the
same protocol. For now only the local implementation exists.

The architecture spec (section 4.7) requires this seam to exist so that Build
tools never shell out directly but go through a backend that can be swapped.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
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


class Runtime(ABC):
    """Abstract runtime backend for Build mode.

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

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
            return ExecResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                exit_code=1,
                stderr=f"Command timed out after {timeout}s: {command}",
                timed_out=True,
            )
        except OSError as exc:
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
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
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
        """Delegate to Hermes checkpoint_manager if available, else no-op."""
        # The checkpoint system is already wired at a higher level.
        # This method exists for the interface contract. A container runtime
        # would take a filesystem snapshot here.
        return f"local:{int(time.time())}"


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
