"""Refuse lifecycle tests unless the tracked sandbox runner established containment."""

import os
from pathlib import Path

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    reasons = []
    for namespace, variable in (
        ("pid", "KWIN_MCP_TEST_HOST_PID_NS"),
        ("net", "KWIN_MCP_TEST_HOST_NET_NS"),
    ):
        original = os.environ.get(variable)
        if not original or os.readlink(f"/proc/self/ns/{namespace}") == original:
            reasons.append(f"private {namespace} namespace missing")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        reasons.append("host root is writable")
    metadata = Path("/tmp").stat()
    if os.environ.get("KWIN_MCP_TEST_TMP_ID") != f"{metadata.st_dev}:{metadata.st_ino}":
        reasons.append("runner-owned /tmp bind missing")
    if reasons:
        raise pytest.UsageError(
            "Lifecycle tests require 'uv run python scripts/test_safety.py': " + "; ".join(reasons)
        )
