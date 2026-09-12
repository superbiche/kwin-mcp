"""Run lifecycle tests with a disposable filesystem, PID and network namespace."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise SystemExit("Lifecycle tests require bubblewrap (bwrap); refusing a host run.")
    temporary_root = root / "tmp"
    temporary_root.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="safety-tests-", dir=temporary_root))
    metadata = scratch.stat()
    command = [
        bwrap,
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/run",
        "--bind",
        str(scratch),
        "/tmp",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "KWIN_MCP_TEST_HOST_PID_NS",
        os.readlink("/proc/self/ns/pid"),
        "--setenv",
        "KWIN_MCP_TEST_HOST_NET_NS",
        os.readlink("/proc/self/ns/net"),
        "--setenv",
        "KWIN_MCP_TEST_TMP_ID",
        f"{metadata.st_dev}:{metadata.st_ino}",
        "--",
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *sys.argv[1:],
    ]
    print(f"Disposable test files retained at: {scratch}", flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
