"""Tests for DockerRuntime, the container Build backend (B5).

The real docker daemon is not required: a fake ``docker`` CLI script
emulates the container behaviours the runtime depends on (run with named
containers, rm -f, inspect state, logs -f). This keeps the suite hermetic
while exercising the real subprocess code paths end to end, including the
ladder passing with only a ``set_runtime()`` swap.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent.build_runtime import (
    ContainerProcessHandle,
    DockerRuntime,
    DockerUnavailable,
    set_runtime,
)


@pytest.fixture
def fake_docker(tmp_path: Path) -> str:
    """Install a fake docker CLI into a temp dir and return its path.

    The fake runs every ``docker run sh -lc <cmd>`` as a local ``sh -lc``
    process, which is exactly how the real container would behave for the
    ladder's echo/typecheck-style commands. Named containers are tracked in a
    state file so inspect/rm/logs behave like the real daemon.
    """
    script = textwrap.dedent(
        r"""#!/bin/sh
        # Fake docker CLI for DockerRuntime tests. State: $FAKE_DOCKER_STATE.
        set -u
        STATE_DIR="${FAKE_DOCKER_STATE:?}"
        mkdir -p "$STATE_DIR"
        CMD="$1"; shift
        case "$CMD" in
          version)
            echo "24.0.0"
            exit 0
            ;;
          run)
            # docker run [-d] [--name X] [--rm] [-v ...] [-w ...] [-p ...] [-e K=V] IMAGE sh -lc CMD
            NAME=""; DETACH=""
            while [ "$#" -gt 0 ]; do
              case "$1" in
                -d) DETACH="1"; shift ;;
                --name) NAME="$2"; shift 2 ;;
                --rm|--init) shift ;;
                -v|-w|-p|-e) shift 2 ;;
                -*) shift ;;
                *) break ;;
              esac
            done
            IMAGE="$1"; shift
            # remaining args: either `sh -lc CMD` (exec/start_process) or a
            # plain argv command like `cat path` (read/write). Normalize both
            # to one shell command string so all four behave like the daemon.
            if [ "$1" = "sh" ] && [ "$2" = "-lc" ]; then
              shift 2
              CMDSTR="$*"
            else
              CMDSTR="$*"
            fi
            if [ "$IMAGE" = "fail-image" ]; then
              echo "pull access denied" >&2
              exit 1
            fi
            if [ -n "$DETACH" ]; then
              # Detached: fully detach like a real daemon. The child must not
              # hold the CLI's stdout/stderr pipes or it dies when the caller
              # closes them; its log goes to a file `docker logs` can replay.
              # nohup + </dev/null keeps it alive after the parent shell exits
              # (portable: macOS has no setsid).
              nohup sh -lc "$CMDSTR" > "$STATE_DIR/$NAME.log" 2>&1 < /dev/null &
              echo "$!" > "$STATE_DIR/$NAME.pid"
              echo "$NAME"
              exit 0
            fi
            sh -lc "$CMDSTR"
            exit $?
            ;;
          rm)
            # docker rm -f NAME
            while [ "$#" -gt 0 ]; do
              case "$1" in
                -f) shift ;;
                *)
                  if [ -f "$STATE_DIR/$1.pid" ]; then
                    kill "$(cat "$STATE_DIR/$1.pid")" 2>/dev/null
                    rm -f "$STATE_DIR/$1.pid"
                  fi
                  shift
                  ;;
              esac
            done
            exit 0
            ;;
          inspect)
            # docker inspect --format {{.State.Running}} NAME
            NAME=""
            while [ "$#" -gt 0 ]; do
              case "$1" in
                --format) shift 2 ;;
                *) NAME="$1"; shift ;;
              esac
            done
            if [ -f "$STATE_DIR/$NAME.pid" ] && kill -0 "$(cat "$STATE_DIR/$NAME.pid")" 2>/dev/null; then
              echo "true"
            else
              echo "false"
            fi
            exit 0
            ;;
          logs)
            # docker logs -f --tail 0 NAME — read the captured log if any.
            NAME=""
            while [ "$#" -gt 0 ]; do
              case "$1" in
                -f) shift ;;
                --tail) shift 2 ;;
                *) NAME="$1"; shift ;;
              esac
            done
            if [ -f "$STATE_DIR/$NAME.log" ]; then
              cat "$STATE_DIR/$NAME.log"
            fi
            # Block until the fake container exits (like a real log follow).
            if [ -f "$STATE_DIR/$NAME.pid" ]; then
              PID="$(cat "$STATE_DIR/$NAME.pid")"
              while kill -0 "$PID" 2>/dev/null; do sleep 0.1; done
            else
              sleep 300
            fi
            exit 0
            ;;
          *)
            echo "fake docker: unknown command $CMD" >&2
            exit 127
            ;;
        esac
        """
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "docker"
    cli.write_text(script, encoding="utf-8")
    cli.chmod(0o755)
    os.environ["FAKE_DOCKER_STATE"] = str(tmp_path / "state")
    return str(cli)


def make_runtime(fake_docker: str, image: str = "node:22") -> DockerRuntime:
    return DockerRuntime(image=image, docker_cmd=fake_docker)


class TestDockerAvailability:
    def test_missing_cli_raises_loudly(self, tmp_path):
        rt = DockerRuntime(image="x", docker_cmd=str(tmp_path / "nope"))
        with pytest.raises(DockerUnavailable, match="docker CLI unavailable"):
            rt.exec("echo hi", tmp_path)

    def test_unreachable_daemon_raises(self, tmp_path):
        cli = tmp_path / "docker"
        cli.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        cli.chmod(0o755)
        rt = DockerRuntime(image="x", docker_cmd=str(cli))
        with pytest.raises(DockerUnavailable, match="daemon not reachable"):
            rt.exec("echo hi", tmp_path)


class TestDockerExec:
    def test_exec_runs_in_container(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        result = rt.exec("echo container-hello", tmp_path)
        assert result.ok
        assert "container-hello" in result.stdout

    def test_exec_reports_failure(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        result = rt.exec("exit 3", tmp_path)
        assert result.exit_code == 3
        assert not result.ok

    def test_exec_timeout_marks_timed_out(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        result = rt.exec("sleep 30", tmp_path, timeout=1)
        assert result.timed_out
        assert not result.ok

    def test_exec_timeout_cleans_up_named_container(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        rt.exec("sleep 30", tmp_path, timeout=1)
        # A second run with the same command must not hit "name in use".
        result = rt.exec("echo after-timeout", tmp_path, timeout=10)
        assert result.ok
        assert "after-timeout" in result.stdout

    def test_exec_publishes_port_from_env(self, fake_docker, tmp_path, monkeypatch):
        calls: list[str] = []
        rt = make_runtime(fake_docker)

        original = subprocess.run
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: (calls.append(str(a[0])), original(*a, **kw))[1],
        )
        rt.exec("true", tmp_path, env={"PORT": "4321"})
        assert any("-p" in call and "127.0.0.1:4321:4321" in call for call in calls)

    def test_image_failure_is_loud(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker, image="fail-image")
        result = rt.exec("true", tmp_path)
        assert not result.ok
        assert "pull access denied" in result.stderr


class TestDockerProcess:
    def test_start_stop_and_liveness(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        handle = rt.start_process("sleep 30", tmp_path, env={"PORT": "3000"})
        assert isinstance(handle, ContainerProcessHandle)
        assert handle.is_running
        rt.stop_process(handle)
        assert not handle.is_running

    def test_duplicate_start_replaces_previous(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        first = rt.start_process("sleep 30", tmp_path)
        second = rt.start_process("sleep 30", tmp_path)
        # Same (workspace, command) names the same container; the second start
        # removes the first. Both handles report the same container, and the
        # second stays alive.
        assert first.container_id == second.container_id
        assert second.is_running

    def test_stop_is_idempotent(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        handle = rt.start_process("sleep 30", tmp_path)
        rt.stop_process(handle)
        rt.stop_process(handle)  # must not raise


class TestDockerFiles:
    def test_write_then_read_roundtrip(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        target = tmp_path / "out" / "file.txt"
        rt.write(target, b"container-bytes")
        assert rt.read(target) == b"container-bytes"

    def test_read_missing_raises(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        with pytest.raises(FileNotFoundError):
            rt.read(tmp_path / "missing.txt")


class TestDockerPorts:
    def test_port_status_and_wait_use_host_probe(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        # Nothing listening on an unassigned port.
        assert rt.port_status(65534).listening is False
        assert rt.wait_for_port(65534, timeout=1) is False

    def test_snapshot_refuses_to_fabricate(self, fake_docker, tmp_path):
        rt = make_runtime(fake_docker)
        with pytest.raises(NotImplementedError):
            rt.snapshot(tmp_path)


class TestLadderWithContainerRuntime:
    """The acceptance: the full ladder passes with only a set_runtime() swap."""

    def test_verify_passes_against_container_backend(self, fake_docker, tmp_path, monkeypatch):
        from agent.build_runtime import get_runtime
        from tools.verify_tool import verify

        (tmp_path / "burooj.build.json").write_text(
            '{"typecheck": "echo types-ok", "lint": "echo lint-ok", '
            '"build": "echo build-ok"}',
            encoding="utf-8",
        )
        set_runtime(make_runtime(fake_docker))
        try:
            result = verify(workspace=tmp_path)
        finally:
            set_runtime(None)  # reset to lazy local default
            get_runtime()  # re-instantiate LocalRuntime for later tests

        assert result["passed"] is True
        by_name = {r["name"]: r for r in result["results"]}
        assert by_name["typecheck"]["status"] == "pass"
        assert by_name["lint"]["status"] == "pass"
        assert by_name["build"]["status"] == "pass"

    def test_failure_propagates_from_container(self, fake_docker, tmp_path):
        from tools.verify_tool import verify

        (tmp_path / "burooj.build.json").write_text(
            '{"typecheck": "exit 2"}', encoding="utf-8"
        )
        set_runtime(make_runtime(fake_docker))
        try:
            result = verify(workspace=tmp_path, rungs=["typecheck"])
        finally:
            set_runtime(None)
        assert result["passed"] is False
        assert result["results"][0]["status"] == "fail"
