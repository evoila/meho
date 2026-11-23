# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The preview of a credential write shows each secret input as the fixed mark.

``preview_operation`` of a call declared ``credential_write`` shows the
request it would send. Each value under a secret name is the fixed mark
``***REDACTED***``: the same mark and the same name rule the echo of a
waiting call uses (:func:`~meho_backplane.operations._preview._redact_echoed_params`).
The method, the path and the other inputs stay as they are. A call that is
not a credential write keeps its literal body and query.

The tests search the whole answer for the value, on the shared funnel
(:func:`~meho_backplane.operations.meta_tools.preview_operation`), on the MCP
tool handler and on the REST route. A preview adds no operation audit row
and logs no input value: the tests count the audit rows before and after the
preview, and search the logs for the value. The REST test mounts the route
alone, so its count holds operation rows and no row of the HTTP request. A hosted
agent run saves the answer in its run record; a test searches the saved event for the value.

Every value below is made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane import op_classes
from meho_backplane.api.v1 import operations as operations_api
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.tools.operations import _preview_operation_handler
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._request_preview import (
    PARAMS_DIGEST_KEY,
    compute_preview_hash,
    preview_dispatch,
)
from meho_backplane.operations.dispatcher import _destructive_binding_refusal
from meho_backplane.operations.meta_tools import preview_operation
from meho_backplane.redaction import clear_overrides
from meho_backplane.session_store.models import AgentSessionEvent
from meho_backplane.settings import get_settings
from tests._hosted_run_helpers import record_hosted_run
from tests.test_operations_request_preview import (
    _TENANT,
    _FakeTarget,
    _handler_returning_ok,
    _insert_ingested_descriptor,
    _make_operator,
    _RecordingHttpConnector,
    _register_recording_gh_connector,
)

_MARK = "***REDACTED***"
_SECRET = "FAKE-made-up-pw-7731"
_OTHER_SECRET = "FAKE-made-up-pw-7732"
_WRITE_OP = "PUT:/repos/{owner}/{repo}/actions/secrets/{name}"
_PLAIN_OP = "POST:/repos/{owner}/{repo}/issues"
_TYPED_OP = "demo.user.set_password"

_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "owner": {"type": "string", "x-meho-param-loc": "path"},
        "repo": {"type": "string", "x-meho-param-loc": "path"},
        "name": {"type": "string", "x-meho-param-loc": "path"},
        "api_key": {"type": "string", "x-meho-param-loc": "query"},
        "visibility": {"type": "string", "x-meho-param-loc": "query"},
        "body": {"type": "object", "x-meho-param-loc": "body"},
    },
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset caches, the connector registry, redaction overrides and the declared classes."""
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    op_classes.declare_op_classes(credential_write=(_WRITE_OP, _TYPED_OP))
    yield
    op_classes._DECLARED.pop(_WRITE_OP, None)
    op_classes._DECLARED.pop(_TYPED_OP, None)
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so descriptor inserts do not pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _seed_write_op(session: AsyncSession, stub: AsyncMock) -> _RecordingHttpConnector:
    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id=_WRITE_OP,
        embedding=stub.encode_one.return_value,
        method="PUT",
        path="/repos/{owner}/{repo}/actions/secrets/{name}",
        parameter_schema=_WRITE_SCHEMA,
    )
    return connector


async def _seed_target() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name="gh-prod",
                aliases=[],
                product="gh",
                version="3",
                host="api.github.com",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )


def _write_arguments(secret: str = _SECRET) -> dict[str, Any]:
    return {
        "connector_id": "gh-rest-3",
        "op_id": _WRITE_OP,
        "target": "gh-prod",
        "params": {
            "owner": "evoila",
            "repo": "meho",
            "name": "DEPLOY_KEY",
            "api_key": secret,
            "visibility": "private",
            "body": {
                "encrypted_value": "visible-text",
                "password": secret,
                "users": [{"name": "svc", "client_secret": secret}],
            },
        },
    }


def _assert_masked(envelope: dict[str, Any]) -> None:
    assert envelope["status"] == "ok", envelope
    assert envelope["method"] == "PUT"
    assert envelope["resolved_path"] == "/repos/evoila/meho/actions/secrets/DEPLOY_KEY"
    assert envelope["query"] == {"api_key": _MARK, "visibility": "private"}
    assert envelope["redacted_body"] == {
        "encrypted_value": "visible-text",
        "password": _MARK,
        "users": [{"name": "svc", "client_secret": _MARK}],
    }
    assert PARAMS_DIGEST_KEY not in envelope
    assert _SECRET not in json.dumps(envelope, default=str)


async def _audit_row_count() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        return (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one()


@pytest.mark.asyncio
async def test_preview_of_a_generic_credential_write_shows_the_mark(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The shared funnel: secret inputs are the mark, the rest is the literal request."""
    connector = await _seed_write_op(session, stub_embedding_service)
    await _seed_target()

    rows_before = await _audit_row_count()
    with capture_logs() as logs:
        envelope = await preview_operation(_make_operator(), _write_arguments())

    _assert_masked(envelope)
    assert connector.calls == []
    assert _SECRET not in json.dumps(logs, default=str)
    assert await _audit_row_count() == rows_before


@pytest.mark.asyncio
async def test_mcp_preview_of_a_credential_write_shows_the_mark(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The MCP tool handler gives the same masked answer."""
    await _seed_write_op(session, stub_embedding_service)
    await _seed_target()

    rows_before = await _audit_row_count()
    envelope = await _preview_operation_handler(_make_operator(), _write_arguments())

    _assert_masked(envelope)
    assert await _audit_row_count() == rows_before


@pytest.mark.asyncio
async def test_rest_preview_of_a_credential_write_shows_the_mark(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """``POST /api/v1/operations/preview`` sends the masked answer."""
    await _seed_write_op(session, stub_embedding_service)
    await _seed_target()
    app = FastAPI()
    app.include_router(operations_api.router)

    async def _operator() -> Operator:
        return _make_operator()

    app.dependency_overrides[verify_jwt_and_bind] = _operator
    transport = httpx.ASGITransport(app=app)
    rows_before = await _audit_row_count()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/operations/preview", json=_write_arguments())

    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    _assert_masked(response.json())
    assert await _audit_row_count() == rows_before


@pytest.mark.asyncio
async def test_hosted_run_record_of_a_credential_write_preview_holds_the_mark(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """A hosted agent run saves the preview answer as a tool result event with the mark."""
    await _seed_write_op(session, stub_embedding_service)
    await _seed_target()
    operator = _make_operator()
    envelope = await _preview_operation_handler(operator, _write_arguments())

    run_id = await record_hosted_run(
        tenant_id=_TENANT,
        sub=operator.sub,
        events=[("tool_result", {"tool_name": "preview_operation", "content": envelope})],
    )

    async with get_sessionmaker()() as s:
        saved = (
            (
                await s.execute(
                    select(AgentSessionEvent).where(AgentSessionEvent.record_id == run_id)
                )
            )
            .scalars()
            .all()
        )
    assert [event.kind for event in saved] == ["tool_result"]
    content = saved[0].data["content"]
    assert content["query"]["api_key"] == _MARK
    assert content["redacted_body"]["password"] == _MARK
    assert content["redacted_body"]["users"][0]["client_secret"] == _MARK
    assert _SECRET not in json.dumps([event.data for event in saved], default=str)


@pytest.mark.asyncio
async def test_preview_hash_of_a_credential_write_changes_with_a_hidden_value(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """A changed hidden value changes the hash, and the hash check refuses the old hash."""
    await _seed_write_op(session, stub_embedding_service)
    await _seed_target()
    first_params = _write_arguments(_SECRET)["params"]
    second_params = _write_arguments(_OTHER_SECRET)["params"]

    first = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id=_WRITE_OP,
        target=_FakeTarget(name="gh-prod"),
        params=first_params,
    )
    second = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id=_WRITE_OP,
        target=_FakeTarget(name="gh-prod"),
        params=second_params,
    )

    assert first["query"] == second["query"]
    assert first["redacted_body"] == second["redacted_body"]
    assert PARAMS_DIGEST_KEY not in first
    assert PARAMS_DIGEST_KEY not in second
    assert first["preview_hash"] != second["preview_hash"]
    assert _SECRET not in json.dumps(first, default=str)

    async def _refusal(params: dict[str, Any]) -> Any:
        return await _destructive_binding_refusal(
            op_id=_WRITE_OP,
            connector_id="gh-rest-3",
            operator=_make_operator(),
            target=_FakeTarget(name="gh-prod"),
            params=params,
            preview_hash=first["preview_hash"],
            proposed_effect=None,
            duration_ms=0.0,
        )

    changed = await _refusal(second_params)
    assert changed is not None
    assert changed.extras["error_code"] == "preview_hash_mismatch"
    same = await _refusal(first_params)
    assert same is not None
    assert same.extras["error_code"] == "blast_radius_required"


@pytest.mark.asyncio
async def test_preview_of_a_call_that_is_not_a_credential_write_is_unchanged(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """A plain write keeps its literal body and query: only a credential write gets the mark."""
    _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id=_PLAIN_OP,
        embedding=stub_embedding_service.encode_one.return_value,
    )

    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id=_PLAIN_OP,
        target=_FakeTarget(name="gh-prod"),
        params={
            "owner": "evoila",
            "repo": "meho",
            "body": {"title": "plain", "password": "plain-field-text"},
        },
    )

    assert envelope["status"] == "ok"
    assert envelope["query"] is None
    assert envelope["redacted_body"] == {"title": "plain", "password": "plain-field-text"}
    assert PARAMS_DIGEST_KEY not in envelope
    assert envelope["preview_hash"] == compute_preview_hash(envelope)


@pytest.mark.asyncio
async def test_preview_of_a_typed_credential_write_shows_the_mark(
    stub_embedding_service: AsyncMock,
) -> None:
    """A typed credential write shows its inputs with the same mark."""
    register_connector_v2(product="demo", version="", impl_id="", cls=_RecordingHttpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=_TYPED_OP,
        handler=_handler_returning_ok,
        summary="Set a user's password.",
        description="Typed credential write.",
        parameter_schema={
            "type": "object",
            "properties": {"user": {"type": "string"}, "password": {"type": "string"}},
        },
        safety_level="destructive",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    rows_before = await _audit_row_count()
    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id=_TYPED_OP,
        target=_FakeTarget(product="demo", version="1.x", name="demo-prod"),
        params={"user": "svc", "password": _SECRET},
    )

    assert envelope["status"] == "ok", envelope
    assert envelope["redacted_body"] == {"user": "svc", "password": _MARK}
    assert _SECRET not in json.dumps(envelope, default=str)
    assert await _audit_row_count() == rows_before
