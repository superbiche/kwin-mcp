"""Cleanup for subprocess groups created and owned by this server."""

from __future__ import annotations

import os
import signal
import subprocess
import time


def process_running(process: subprocess.Popen[bytes]) -> bool:
    """Observe an owned child without reaping it or releasing its PID for reuse.

    Keep leaders waitable until all group signals have been sent. Do not use
    Popen.poll()/wait() on these children before stop_processes().
    """
    if process.returncode is not None:
        return False
    try:
        result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as exc:
        raise RuntimeError("Owned child was reaped outside session cleanup") from exc
    return result is None


def stop_processes(processes: list[subprocess.Popen[bytes]], timeout: float = 2.0) -> None:
    """Terminate owned process groups, escalate surviving descendants, and reap leaders.

    Every process passed here MUST have been launched with start_new_session=True.
    Deliberately detached descendants are outside this process-group guarantee.
    """
    remaining: set[int] = set()
    owned: list[subprocess.Popen[bytes]] = []
    errors: list[Exception] = []
    for process in processes:
        if process.returncode is not None:
            # Already reaped: its numeric PID is no longer safe to signal.
            continue
        try:
            process_running(process)
        except RuntimeError as exc:
            errors.append(exc)
        else:
            remaining.add(process.pid)
            owned.append(process)

    def send(pgid: int, sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return False
        except OSError as exc:
            errors.append(exc)
            return False
        return True

    remaining = {pgid for pgid in remaining if send(pgid, signal.SIGTERM)}
    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        remaining = {pgid for pgid in remaining if send(pgid, 0)}
        if remaining:
            time.sleep(0.02)
    for pgid in remaining:
        send(pgid, signal.SIGKILL)
    # Reap only AFTER the last group signal, retaining PID ownership until then.
    for process in owned:
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Could not stop all owned processes", errors)
