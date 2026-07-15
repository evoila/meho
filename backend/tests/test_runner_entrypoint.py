# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``python -m meho_backplane.runner`` entrypoint (#2497).

The subprocess tests prove the two process-level contracts of the third
execution mode:

* it starts **headless** — no ``DATABASE_URL`` / ``KEYCLOAK_*`` /
  ``BROADCAST_REDIS_URL`` in the environment — emits ``runner_started``
  and at least one tick event on stdout, and exits 0 on SIGTERM;
* a missing required ``MEHO_RUNNER_*`` var exits 1 with a message naming
  the variable.

The unit tests cover :func:`get_runner_settings`' env mapping + defaults
and its :class:`RunnerConfigError` on a missing variable.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from meho_backplane.runner.settings import (
    DEFAULT_SPOOL_DIR,
    DEFAULT_SPOOL_MAX_FILES,
    DEFAULT_TICK_INTERVAL_SECONDS,
    RunnerConfigError,
    get_runner_settings,
)

_CHASSIS_ENV_VARS = (
    "DATABASE_URL",
    "KEYCLOAK_ISSUER_URL",
    "KEYCLOAK_AUDIENCE",
    "BROADCAST_REDIS_URL",
)
_RUNNER_ENV_VARS = (
    "MEHO_RUNNER_CENTRAL_URL",
    "MEHO_RUNNER_ID",
    "MEHO_RUNNER_TOKEN",
    "MEHO_RUNNER_TICK_INTERVAL_SECONDS",
    "MEHO_RUNNER_SPOOL_DIR",
    "MEHO_RUNNER_SPOOL_MAX_FILES",
)


def _base_env() -> dict[str, str]:
    """A clean env: real PATH etc., but no chassis or runner vars."""
    env = dict(os.environ)
    for key in (*_CHASSIS_ENV_VARS, *_RUNNER_ENV_VARS):
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------------------
# Subprocess contract tests
# ---------------------------------------------------------------------------


def test_headless_start_emits_events_and_sigterm_exits_clean(tmp_path: Path) -> None:
    env = _base_env()
    env.update(
        {
            # Unreachable port: fetch fails each tick, loop keeps ticking.
            "MEHO_RUNNER_CENTRAL_URL": "http://127.0.0.1:9",
            "MEHO_RUNNER_ID": "r1",
            "MEHO_RUNNER_TOKEN": "x",
            "MEHO_RUNNER_TICK_INTERVAL_SECONDS": "0.2",
            "MEHO_RUNNER_SPOOL_DIR": str(tmp_path / "spool"),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "meho_backplane.runner"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    lines: list[str] = []
    lock = threading.Lock()

    def _drain() -> None:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            with lock:
                lines.append(line)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    def _seen(marker: str) -> bool:
        with lock:
            return any(marker in ln for ln in lines)

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _seen("runner_started") and _seen("runner_tick"):
                break
            time.sleep(0.1)
        assert _seen("runner_started"), "".join(lines)
        assert _seen("runner_tick"), "".join(lines)
        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    reader.join(timeout=5)

    assert returncode == 0, "".join(lines)


def test_missing_required_env_exits_1_naming_var(tmp_path: Path) -> None:
    env = _base_env()
    # Provide ID + TOKEN but omit CENTRAL_URL: the error must name it.
    env.update({"MEHO_RUNNER_ID": "r1", "MEHO_RUNNER_TOKEN": "x"})
    result = subprocess.run(
        [sys.executable, "-m", "meho_backplane.runner"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert "MEHO_RUNNER_CENTRAL_URL" in result.stderr


# ---------------------------------------------------------------------------
# RunnerSettings unit tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_runner_settings.cache_clear()
    yield
    get_runner_settings.cache_clear()


def test_settings_reads_env_and_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _RUNNER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MEHO_RUNNER_CENTRAL_URL", "https://central.example")
    monkeypatch.setenv("MEHO_RUNNER_ID", "runner-9")
    monkeypatch.setenv("MEHO_RUNNER_TOKEN", "sekret")

    settings = get_runner_settings()

    assert settings.central_url == "https://central.example"
    assert settings.runner_id == "runner-9"
    assert settings.runner_token == "sekret"
    assert settings.tick_interval_seconds == DEFAULT_TICK_INTERVAL_SECONDS
    assert settings.spool_dir == DEFAULT_SPOOL_DIR
    assert settings.spool_max_files == DEFAULT_SPOOL_MAX_FILES
    # The bearer token must not leak through repr.
    assert "sekret" not in repr(settings)


def test_settings_missing_required_var_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEHO_RUNNER_CENTRAL_URL", raising=False)
    monkeypatch.setenv("MEHO_RUNNER_ID", "runner-9")
    monkeypatch.setenv("MEHO_RUNNER_TOKEN", "sekret")

    with pytest.raises(RunnerConfigError, match="MEHO_RUNNER_CENTRAL_URL"):
        get_runner_settings()


def test_settings_malformed_numeric_var_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEHO_RUNNER_CENTRAL_URL", "https://central.example")
    monkeypatch.setenv("MEHO_RUNNER_ID", "runner-9")
    monkeypatch.setenv("MEHO_RUNNER_TOKEN", "sekret")
    monkeypatch.setenv("MEHO_RUNNER_TICK_INTERVAL_SECONDS", "not-a-number")

    with pytest.raises(RunnerConfigError, match="MEHO_RUNNER_TICK_INTERVAL_SECONDS"):
        get_runner_settings()
