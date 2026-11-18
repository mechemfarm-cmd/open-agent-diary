"""Supervisor mode: the recorder creates the scope, so the join is structural.

The recorder needs to know which cgroup belongs to which run. In supervisor mode
it does not have to guess: it launches the workload inside a scope it created
itself, so the scope name is known by construction and the cgroup inode is
measured from systemd rather than inferred.

That is the whole reason Stage 3 is supervisor-mode only. Observer mode has to
answer "did a scope appear and disappear between my polls", which is a real
question with wrong answers available; this one does not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class SupervisorError(Exception):
    pass


@dataclass
class SystemdScope:
    """A running scope, and the cgroup that identifies it."""

    unit: str
    process: subprocess.Popen[bytes]
    _control_group: str

    @property
    def name(self) -> str:
        return self.unit

    @property
    def cgroup_inode(self) -> str:
        path = Path("/sys/fs/cgroup") / self._control_group.lstrip("/")
        try:
            return str(path.stat().st_ino)
        except OSError as error:
            raise SupervisorError(
                f"cannot measure the cgroup for {self.unit}: {error}"
            ) from error

    def wait(self, timeout: float) -> int:
        """Wait for the workload to finish, or for the window to elapse.

        A workload that outlives the window is left running: the recorder's job
        is to record for the window, not to police the workload's lifetime. The
        scope is stopped in ``stop`` either way.
        """
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return -1

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        # The transient scope goes when its last process goes; ask systemd to
        # make sure nothing is left behind under it.
        subprocess.run(
            ["systemctl", "stop", self.unit], capture_output=True, check=False
        )


class SystemdSupervisor:
    """Launches a workload in a transient scope and measures its cgroup."""

    def __init__(self, *, capture_id: str, settle_timeout: float = 10.0) -> None:
        self.capture_id = capture_id
        self.settle_timeout = settle_timeout

    def start(self, workload: Sequence[str]) -> SystemdScope:
        if not workload:
            raise SupervisorError("a workload is required")
        if shutil.which("systemd-run") is None:
            raise SupervisorError("systemd-run is not available")

        unit = f"mp-{self.capture_id}.scope"
        command = [
            "systemd-run",
            "--scope",
            "--quiet",
            f"--unit={unit}",
            "--",
            *workload,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        control_group = self._await_control_group(unit, process)
        return SystemdScope(unit=unit, process=process, _control_group=control_group)

    def _await_control_group(
        self, unit: str, process: subprocess.Popen[bytes]
    ) -> str:
        """Wait until systemd reports a control group for the scope.

        Racily reading ``systemctl show`` immediately after ``Popen`` returns an
        empty string, and an empty control group silently measures ``/`` — which
        would attribute the whole machine's activity to the workload. Failing
        here is correct; guessing is not.
        """
        deadline = time.monotonic() + self.settle_timeout
        last = ""
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["systemctl", "show", unit, "-p", "ControlGroup", "--value"],
                capture_output=True,
                text=True,
                check=False,
            )
            last = result.stdout.strip()
            if last:
                return last
            if process.poll() is not None:
                stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
                raise SupervisorError(
                    f"scope {unit} exited before it was established: {stderr.strip()[:200]}"
                )
            time.sleep(0.05)
        raise SupervisorError(
            f"scope {unit} has no control group after {self.settle_timeout}s"
        )


def current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-boot"


def require_root(role: str = "recorder") -> None:
    """Fail loudly rather than letting bpftrace fail obscurely later."""
    if os.geteuid() != 0:
        raise PermissionError(
            f"the {role} must run as root: attaching probes is not permitted for "
            "an unprivileged process on this kernel configuration"
        )
