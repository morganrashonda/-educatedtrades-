"""Process-level regression tests for the external watchdog loop."""

import os
from pathlib import Path
import signal
import subprocess
import time

import pytest


BACKEND = Path(__file__).resolve().parents[1]
WATCHDOG_LOOP = BACKEND / "watchdog_loop.sh"


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_loop_child_and_removes_pid_file(
    tmp_path: Path, stop_signal: signal.Signals,
) -> None:
    """Stop signals must not return control to the infinite loop."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        '#!/bin/sh\necho $$ > "$WATCHDOG_CHILD_PID_FILE"\nexec sleep 30\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    data_dir = tmp_path / "paper"
    key = str(data_dir).replace("/", "_")
    pid_file = Path(f"/tmp/watchdog_loop{key}.pid")
    child_pid_file = tmp_path / "child.pid"
    env = os.environ.copy()
    env["DATA_DIR"] = str(data_dir)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["WATCHDOG_CHILD_PID_FILE"] = str(child_pid_file)

    process = subprocess.Popen(
        ["bash", str(WATCHDOG_LOOP)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_until(pid_file.exists), "watchdog did not create its PID file"
        assert pid_file.read_text(encoding="utf-8").strip() == str(process.pid)
        assert _wait_until(child_pid_file.exists), "watchdog child did not start"
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())

        os.kill(process.pid, stop_signal)
        assert process.wait(timeout=3) == 0
        assert _wait_until(lambda: not pid_file.exists())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        pid_file.unlink(missing_ok=True)
