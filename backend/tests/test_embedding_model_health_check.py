# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``embedding_model`` readiness item and its switch ``EMBEDDING_MODEL_HEALTH_CHECK``.

These tests start the real app (the lifespan start in ``main.py``) and read
the full ``meho_status`` answer:

* with the switch off (the default), a model mismatch gives the same answer
  as matching models, and the start still logs the warning;
* with the switch on, a mismatch adds the ``embedding_model`` item.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.health import clear_probes, clear_readiness_cache
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

OTHER_MODEL = "example/made-up-other-model"

#: Answer fields that hold times or clock readings; they differ between two starts.
_CLOCK_FIELDS = frozenset(
    {"server_time", "written_at", "seconds_since_last_tick", "seconds_since_last_claim"}
)


def _without_clock(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_clock(v) for k, v in value.items() if k not in _CLOCK_FIELDS}
    if isinstance(value, list):
        return [_without_clock(v) for v in value]
    return value


def _store_model(model: str) -> None:
    """Set the model of the stored vectors in the test database file."""
    path = os.environ["DATABASE_URL"].removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE embedding_state SET model = ?", (model,))


def _start_and_read_status(
    capfd: pytest.CaptureFixture[str],
) -> tuple[dict[str, Any], int]:
    """Start the app, call ``meho_status``; return its answer and the count of mismatch logs.

    The start sets up the JSON log output, so the logs are read from stdout.
    """
    clear_probes()
    clear_readiness_cache()
    operator = build_operator()

    async def _fake_verify() -> Any:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        capfd.readouterr()
        with TestClient(app) as client:
            response = post_mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "meho_status", "arguments": {}},
                },
            )
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    payload: dict[str, Any] = json.loads(response.json()["result"]["content"][0]["text"])
    return payload, capfd.readouterr().out.count('"event": "embedding_model_mismatch"')


def _check_names(payload: dict[str, Any]) -> list[str]:
    return [check["name"] for check in payload["readiness"]["checks"]]


def test_with_the_switch_off_a_mismatch_leaves_the_meho_status_answer_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL_HEALTH_CHECK", raising=False)
    get_settings.cache_clear()
    _store_model(get_settings().retrieval_embedding_model)
    matching, matching_warnings = _start_and_read_status(capfd)

    _store_model(OTHER_MODEL)
    mismatch, mismatch_warnings = _start_and_read_status(capfd)

    assert matching_warnings == 0
    assert mismatch_warnings == 1
    assert "embedding_model" not in _check_names(mismatch)
    assert _without_clock(mismatch) == _without_clock(matching)


def test_with_the_switch_on_a_mismatch_adds_the_embedding_model_item(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_HEALTH_CHECK", "true")
    get_settings.cache_clear()
    _store_model(OTHER_MODEL)

    payload, warnings = _start_and_read_status(capfd)

    assert warnings == 1
    items = [
        check for check in payload["readiness"]["checks"] if check["name"] == "embedding_model"
    ]
    assert len(items) == 1
    assert items[0]["ok"] is True
    assert OTHER_MODEL in items[0]["detail"]
    assert "reembed" in items[0]["detail"]


def test_with_the_switch_on_matching_models_add_no_item(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_HEALTH_CHECK", "true")
    get_settings.cache_clear()
    _store_model(get_settings().retrieval_embedding_model)

    payload, warnings = _start_and_read_status(capfd)

    assert warnings == 0
    assert "embedding_model" not in _check_names(payload)
