# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""arm64 first-run regression test.

Boots the full compose stack via the local-TEI fallback path on an arm64 host
and asserts the backend and frontend are reachable. Guards the first-run
contract described in docs/codebase/first-run-experience.md and the Rosetta/
QEMU emulation contract described in docs/development/arm64-notes.md.

Designed for CI runners (ubuntu-24.04-arm) and clean arm64 dev machines. Will
fail with port conflicts if run against a live ``docker compose up`` on
8000/5173.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from collections.abc import Iterator

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        platform.machine() not in ("arm64", "aarch64"),
        reason="arm64-only first-run test",
    ),
]

TEST_PROJECT = "meho-firstrun-arm64-test"
HEALTH_TIMEOUT_S = 180
FRONTEND_TIMEOUT_S = 60


def _compose_env() -> dict[str, str]:
    env = os.environ.copy()
    # docker-compose.yml uses ``${CREDENTIAL_ENCRYPTION_KEY:?...}`` -- compose
    # errors at yaml parse time if unset. The value is a dummy; the test does
    # not exercise any credential-encryption code path.
    env.setdefault(
        "CREDENTIAL_ENCRYPTION_KEY",
        "arm64-first-run-test-fernet-key-32plus-chars",
    )
    return env


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # All argv entries are literals or test-controlled constants; no user input.
    # `docker` resolved via PATH is the idiomatic way to invoke it -- see
    # docker/setup-qemu-action and every other CI/test harness in this repo.
    argv = ["docker", "compose", "-p", TEST_PROJECT, *args]  # noqa: S607
    return subprocess.run(  # noqa: S603
        argv,
        check=check,
        env=_compose_env(),
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clean_compose_stack() -> Iterator[None]:
    _compose("down", "--volumes", check=False)
    try:
        yield
    finally:
        logs = _compose("logs", "--no-color", check=False).stdout
        if logs:
            print("---- docker compose logs ----\n" + logs)
        _compose("down", "--volumes", check=False)


def _wait_for(url: str, timeout: int) -> httpx.Response:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                return r
        except Exception as e:  # noqa: BLE001 -- polling, all exceptions are "not ready yet"
            last_error = e
        time.sleep(3)
    raise AssertionError(f"Timed out waiting for {url}: {last_error}")


@pytest.mark.timeout(600)
def test_first_run_arm64_local_tei_path(clean_compose_stack: None) -> None:
    """Literal README local-TEI fallback path boots on arm64."""
    _compose(
        "--profile",
        "tei",
        "up",
        "-d",
        "--build",
    )

    # Guard against the silent false-positive: an amd64 container crashing
    # under broken emulation while compose up -d reports success because
    # containers were "launched". Without this, the test stays green even
    # when the thing we are validating (Rosetta/QEMU emulation) is broken.
    time.sleep(5)
    exited = _compose(
        "ps",
        "--status",
        "exited",
        "--format",
        "{{.Name}}",
        check=False,
    ).stdout.strip()
    assert not exited, f"containers exited immediately after up: {exited}"

    backend = _wait_for("http://localhost:8000/health", HEALTH_TIMEOUT_S)
    assert backend.status_code == 200

    frontend = _wait_for("http://localhost:5173", FRONTEND_TIMEOUT_S)
    assert "<html" in frontend.text.lower()
