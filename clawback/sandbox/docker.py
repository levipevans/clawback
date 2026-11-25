"""Docker-backed tool executor.

This is the security boundary. The container gets:

* /work              rw   (the one sandbox folder)
* /ro/<name>         ro   (explicit read-only roots)
* /opt/clawback      ro   (this tool runtime)
* no network, a read-only root filesystem, all capabilities dropped,
  no-new-privileges, resource limits, and a non-root user.

Nothing else from the host is mounted. If Docker is unavailable we refuse to
run tools; we never fall back to touching host files directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..policy import Mount, Policy

IMAGE = "clawback-sandbox:v1"
CONTAINER = "clawback-sandbox"
RUNTIME_MOUNT = "/opt/clawback"


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class ExecResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    duration: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


class DockerSandbox:
    def __init__(
        self,
        policy: Policy,
        runtime_dir: Path | None = None,
        *,
        container_name: str = CONTAINER,
    ):
        self.policy = policy
        self.runtime_dir = (runtime_dir or Path(__file__).parent).resolve()
        self.container_name = container_name
        self._lock = threading.RLock()
        self._docker_exe = shutil.which("docker")

    # -- discovery --------------------------------------------------------
    def docker_path(self) -> str:
        if not self._docker_exe:
            raise SandboxUnavailable(
                "Docker CLI not found on PATH. Install Docker Desktop and restart the terminal."
            )
        return self._docker_exe

    def available(self) -> bool:
        try:
            result = self._run(["info", "--format", "{{.ServerVersion}}"], timeout=15)
        except SandboxUnavailable:
            return False
        return result.returncode == 0

    def _run(
        self,
        args: list[str],
        *,
        timeout: float,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = [self.docker_path(), *args]
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.run(
                cmd,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"docker {' '.join(args[:2])} timed out") from exc
        except FileNotFoundError as exc:
            raise SandboxUnavailable("Docker CLI disappeared from PATH") from exc

    # -- image / container lifecycle -------------------------------------
    def ensure_image(self) -> None:
        if not self.available():
            raise SandboxUnavailable(
                "Docker Desktop is not running. Start Docker Desktop; Clawback will not "
                "run tools against the host as a fallback."
            )
        inspect = self._run(["image", "inspect", IMAGE], timeout=20)
        if inspect.returncode == 0:
            return
        build = self._run(
            [
                "build",
                "-t",
                IMAGE,
                "-f",
                str(self.runtime_dir / "Dockerfile"),
                str(self.runtime_dir),
            ],
            timeout=900,
        )
        if build.returncode != 0:
            tail = (build.stderr or build.stdout or b"").decode("utf-8", "replace")[-2000:]
            raise SandboxUnavailable(f"failed to build sandbox image:\n{tail}")

    def _mount_args(self) -> list[str]:
        args: list[str] = []
        mounts: list[Mount] = list(self.policy.mounts())
        mounts.append(Mount(self.runtime_dir, RUNTIME_MOUNT, True))
        for mount in mounts:
            spec = f"{mount.host}:{mount.container}"
            if mount.read_only:
                spec += ":ro"
            args.extend(["-v", spec])
        return args

    def _fingerprint(self) -> str:
        payload = {
            "image": IMAGE,
            "mounts": [
                [str(m.host).lower() if sys.platform == "win32" else str(m.host), m.container, m.read_only]
                for m in self.policy.mounts()
            ],
            "runtime": str(self.runtime_dir),
            "flags": "v1",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def _inspect(self) -> tuple[bool, str]:
        result = self._run(
            [
                "inspect",
                "--format",
                '{{.State.Running}}|{{index .Config.Labels "clawback.fingerprint"}}',
                self.container_name,
            ],
            timeout=15,
        )
        if result.returncode != 0:
            return False, ""
        text = (result.stdout or b"").decode("utf-8", "replace").strip()
        running, _, fingerprint = text.partition("|")
        return running.lower() == "true", fingerprint

    def start(self) -> None:
        with self._lock:
            self.ensure_image()
            fingerprint = self._fingerprint()
            running, current = self._inspect()
            if running and current == fingerprint:
                return
            if running or current:
                self._run(["rm", "-f", self.container_name], timeout=30)

            args = [
                "run",
                "-d",
                "--name",
                self.container_name,
                "--label",
                f"clawback.fingerprint={fingerprint}",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "128",
                "--memory",
                "512m",
                "--cpus",
                "1.0",
                "--init",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                "--user",
                "1000:1000",
                "--workdir",
                "/work",
                "-e",
                "HOME=/tmp",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PYTHONUNBUFFERED=1",
                *self._mount_args(),
                IMAGE,
                "sleep",
                "infinity",
            ]
            result = self._run(args, timeout=120)
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or b"").decode("utf-8", "replace")[-2000:]
                raise SandboxUnavailable(f"failed to start sandbox container:\n{tail}")

    def _ensure_running(self) -> None:
        running, _ = self._inspect()
        if not running:
            self.start()

    def stop(self, *, remove: bool = True) -> None:
        with self._lock:
            if not self.available():
                return
            try:
                self._run(["stop", "-t", "2", self.container_name], timeout=30)
            except Exception:
                pass
            if remove:
                try:
                    self._run(["rm", "-f", self.container_name], timeout=30)
                except Exception:
                    pass

    def status(self) -> dict:
        if not self.available():
            return {"available": False, "running": False, "container": self.container_name}
        try:
            running, fingerprint = self._inspect()
        except Exception as exc:
            return {"available": True, "running": False, "error": str(exc), "container": self.container_name}
        return {
            "available": True,
            "running": running,
            "container": self.container_name,
            "fingerprint": fingerprint,
        }

    # -- execution --------------------------------------------------------
    def _truncate(self, text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]", True

    def exec_ops(self, op: str, args: dict, *, timeout: float, max_output_bytes: int) -> ExecResult:
        started = time.time()
        try:
            self._ensure_running()
        except SandboxUnavailable as exc:
            return ExecResult(False, None, "", str(exc))
        payload = json.dumps({"op": op, **args}).encode("utf-8")
        cmd = [
            "exec",
            "-i",
            "--workdir",
            "/work",
            "-e",
            "HOME=/tmp",
            self.container_name,
            "timeout",
            "-s",
            "KILL",
            str(max(1, int(timeout))),
            "python",
            "-I",
            f"{RUNTIME_MOUNT}/ops.py",
        ]
        try:
            result = self._run(cmd, timeout=timeout + 10, input_bytes=payload)
        except TimeoutError:
            return ExecResult(False, None, "", "sandbox operation timed out", timed_out=True,
                              duration=time.time() - started)
        stdout = (result.stdout or b"").decode("utf-8", "replace").strip()
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        duration = time.time() - started
        timed_out = result.returncode in (124, 137)
        if timed_out:
            return ExecResult(False, result.returncode, stdout, stderr or "timed out",
                              timed_out=True, duration=duration)
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            out, truncated = self._truncate(stdout or stderr, max_output_bytes)
            return ExecResult(
                False,
                result.returncode,
                out,
                f"sandbox returned invalid output: {stderr[:500]}",
                truncated=truncated,
                duration=duration,
            )
        text = json.dumps(parsed, ensure_ascii=False)
        text, truncated = self._truncate(text, max_output_bytes)
        return ExecResult(
            ok=bool(parsed.get("ok")),
            exit_code=result.returncode,
            stdout=text,
            stderr=stderr,
            truncated=truncated,
            duration=duration,
        )

    def run_bash(self, command: str, *, timeout: float, max_output_bytes: int) -> ExecResult:
        started = time.time()
        try:
            self._ensure_running()
        except SandboxUnavailable as exc:
            return ExecResult(False, None, "", str(exc))
        cmd = [
            "exec",
            "-i",
            "--workdir",
            "/work",
            "-e",
            "HOME=/tmp",
            self.container_name,
            "timeout",
            "-s",
            "KILL",
            str(max(1, int(timeout))),
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]
        try:
            result = self._run(cmd, timeout=timeout + 10)
        except TimeoutError:
            return ExecResult(False, None, "", "command timed out", timed_out=True,
                              duration=time.time() - started)
        stdout = (result.stdout or b"").decode("utf-8", "replace")
        stderr = (result.stderr or b"").decode("utf-8", "replace")
        stdout, truncated_out = self._truncate(stdout, max_output_bytes)
        stderr, truncated_err = self._truncate(stderr, max_output_bytes)
        return ExecResult(
            ok=result.returncode == 0,
            exit_code=result.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.returncode in (124, 137),
            truncated=truncated_out or truncated_err,
            duration=time.time() - started,
        )
