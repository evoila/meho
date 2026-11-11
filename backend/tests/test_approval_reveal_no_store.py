# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An answer that carries a revealed secret value says ``Cache-Control: no-store`` (#6273).

``POST /api/v1/approvals/{id}/reveal`` runs an approved reveal once and
answers with the value. ``POST /api/v1/operations/call`` with
``reveal_secret: true`` on a plain read answers with the value at once.
Those answers must not be kept by a browser or a proxy. The answers that
carry no value (a refused fetch, the stored run result, the same call
without ``reveal_secret``) keep their usual headers. A reveal call cannot
run in the background: ``reveal_secret: true`` with ``async: true`` is
refused, and no run starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun
from meho_backplane.main import app
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.no_store import NO_STORE_HEADERS, answer_scope, mark_answer_holds_secret
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._reveal_fetch_helpers import (
    CONNECTOR_ID,
    PLAIN_OP,
    SECRET_VALUE,
    approved_reveal,
    register_plain_read,
    register_vault_read,
    run_without_value,
)
from tests.mcp_test_fixtures import OPERATOR_TENANT_ID

_REQUESTER = Operator(
    sub="requester-6273",
    name="Requester",
    email=None,
    raw_jwt="fixture-jwt-not-real",
    tenant_id=OPERATOR_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
    principal_kind=PrincipalKind.USER,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The app, with every REST call made by the person who asked."""

    async def _requester() -> Operator:
        return _REQUESTER

    app.dependency_overrides[verify_jwt_and_bind] = _requester
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(verify_jwt_and_bind, None)


@pytest.fixture
async def vault_read(client: TestClient) -> AsyncIterator[None]:
    """The ``vault.kv.read`` stand-in, registered after the app started."""
    clear_registry()
    reset_dispatcher_caches()
    await register_vault_read()
    await register_plain_read()
    yield
    clear_registry()
    reset_dispatcher_caches()


def _says_no_store(response: Any) -> bool:
    return all(response.headers.get(name) == value for name, value in NO_STORE_HEADERS.items())


@pytest.mark.usefixtures("vault_read")
async def test_rest_reveal_answer_with_the_value_says_no_store(client: TestClient) -> None:
    """The one answer with the value says no-store; the answers without it do not."""
    request_id = await approved_reveal(_REQUESTER)

    shown = client.post(f"/api/v1/approvals/{request_id}/reveal")
    assert shown.status_code == 200, shown.text
    assert shown.json()["result"]["data"]["password"] == SECRET_VALUE
    assert shown.headers["cache-control"] == "no-store"
    assert shown.headers["pragma"] == "no-cache"

    again = client.post(f"/api/v1/approvals/{request_id}/reveal")
    assert again.status_code == 409, again.text
    assert SECRET_VALUE not in again.text
    assert not _says_no_store(again)

    stored = client.get(f"/api/v1/approvals/{request_id}/result")
    assert stored.status_code == 200, stored.text
    assert SECRET_VALUE not in stored.text
    assert not _says_no_store(stored)


@pytest.mark.usefixtures("vault_read")
async def test_rest_reveal_refused_before_approval_keeps_usual_headers(
    client: TestClient,
) -> None:
    """A fetch of an unknown request is refused with no value and no no-store."""
    missing = client.post("/api/v1/approvals/00000000-0000-0000-0000-000000006273/reveal")
    assert missing.status_code == 404, missing.text
    assert not _says_no_store(missing)


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize("how", ["failed", "refused"])
async def test_rest_reveal_run_without_a_value_keeps_usual_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The approved run ends with an error or is refused: no value, no no-store."""
    request_id = await run_without_value(monkeypatch, _REQUESTER, how)

    answer = client.post(f"/api/v1/approvals/{request_id}/reveal")
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] != "ok", answer.text
    assert SECRET_VALUE not in answer.text
    assert not _says_no_store(answer)
    assert "pragma" not in answer.headers


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize("reveal", [True, False], ids=["reveal", "no-reveal"])
async def test_rest_call_says_no_store_only_when_it_reveals(
    client: TestClient, reveal: bool
) -> None:
    """A person's ``reveal_secret: true`` on a plain read runs at once and says no-store."""
    params: dict[str, Any] = {"reveal_secret": True} if reveal else {}
    answer = client.post(
        "/api/v1/operations/call",
        json={"connector_id": CONNECTOR_ID, "op_id": PLAIN_OP, "params": params},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "ok", answer.text
    assert (SECRET_VALUE in answer.text) is reveal
    assert _says_no_store(answer) is reveal
    if not reveal:
        assert "pragma" not in answer.headers


async def _run_count() -> int:
    async with get_sessionmaker()() as session:
        return int(await session.scalar(select(func.count()).select_from(OperationRun)) or 0)


@pytest.mark.usefixtures("vault_read")
async def test_rest_reveal_call_cannot_run_in_the_background(client: TestClient) -> None:
    """``reveal_secret: true`` with ``async: true`` is refused with a 422; no run starts."""
    before = await _run_count()
    answer = client.post(
        "/api/v1/operations/call",
        json={
            "connector_id": CONNECTOR_ID,
            "op_id": PLAIN_OP,
            "params": {"reveal_secret": True},
            "async": True,
        },
    )
    assert answer.status_code == 422, answer.text
    detail = answer.json()["detail"]
    assert detail["error_code"] == "reveal_secret_with_async"
    assert "without `async`" in detail["message"]
    assert SECRET_VALUE not in answer.text
    assert await _run_count() == before


def test_answer_scope_marks_only_inside_and_starts_unset() -> None:
    """A mark outside a scope does nothing; each scope starts unset."""
    mark_answer_holds_secret()
    with answer_scope() as first:
        assert first.holds_secret is False
        mark_answer_holds_secret()
    assert first.holds_secret is True
    with answer_scope() as second:
        assert second.holds_secret is False
