# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Smoke test conftest -- auto-skip when the MEHO server is not reachable.

Smoke tests make real HTTP requests and require a running server.
This conftest prevents hangs in environments without one (CI, offline dev).
"""

import socket

import pytest

_SERVER_HOST = "localhost"
_SERVER_PORT = 8000
_PROBE_TIMEOUT = 1.0


def _server_is_reachable() -> bool:
    """Quick TCP probe -- returns True if something is listening."""
    try:
        with socket.create_connection((_SERVER_HOST, _SERVER_PORT), timeout=_PROBE_TIMEOUT):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every item in this directory when the server is unreachable."""
    if _server_is_reachable():
        return
    skip_marker = pytest.mark.skip(
        reason=f"MEHO server not reachable at {_SERVER_HOST}:{_SERVER_PORT}"
    )
    for item in items:
        item.add_marker(skip_marker)
