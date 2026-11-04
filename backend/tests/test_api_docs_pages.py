# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The API docs pages are off unless ``API_DOCS_ENABLED`` is set.

``/openapi.json``, ``/docs`` and ``/redoc`` need no login, so the app
serves them only when the deployment asks for it. ``app.openapi()`` keeps
working either way: the CLI's committed snapshot is written from it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meho_backplane.main import api_docs_enabled, app


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_docs_pages_are_off_by_default(path: str) -> None:
    response = TestClient(app).get(path)
    assert response.status_code == 404


def test_openapi_schema_still_builds_in_process() -> None:
    assert "/healthz" in app.openapi()["paths"]


def test_switch_is_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    assert api_docs_enabled() is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nonsense"])
def test_switch_is_off_for_false_and_unknown_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("API_DOCS_ENABLED", value)
    assert api_docs_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_switch_is_on_for_true_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("API_DOCS_ENABLED", value)
    assert api_docs_enabled() is True
