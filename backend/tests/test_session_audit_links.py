# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An audit row links to the stored session it belongs to (#4904).

* An uploaded session: MEHO finds the audit rows of its MCP calls (same
  person, same tool, same arguments hash, inside the session's time) and
  stores their MCP session id. ``GET /api/v1/audit/show/{id}`` then names
  the session in ``stored_session_id``.
* A hosted agent run: its tool calls carry the run id, which is the id of
  its session.
* No link for a reader who may not read sessions, for a row with no stored
  session, and for a CLI login session.

The object storage is an in-memory fake. Every value in this file is made up.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.operations._audit import SESSION_FROM_CLIENT_KEY
from meho_backplane.operations._validate import compute_audit_params_hash
from meho_backplane.session_store.audit_links import stored_session_for
from meho_backplane.session_store.hosted_runs import HOSTED_RUN_APP, add_run_session
from meho_backplane.session_store.models import AgentSessionLink
from meho_backplane.session_store.object_store import set_object_store_for_testing
from meho_backplane.session_store.tool_calls import tool_calls_in
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SID = "0b5e2a8c-1111-4222-8333-444455556666"
_KEY = make_rsa_keypair("kid-session-links")
_ARGS: dict[str, Any] = {"connector_id": "k8s", "op_id": "pods.list", "params": {"ns": "web"}}


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, keys: Sequence[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    monkeypatch.setenv("SESSION_STORE_ENDPOINT", "https://objects.example.com")
    monkeypatch.setenv("SESSION_STORE_BUCKET", "sessions")
    monkeypatch.setenv("SESSION_STORE_CREDENTIALS_VAULT_PATH", "secret/data/meho/session-store")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    set_object_store_for_testing(_FakeStore())
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(_KEY))
        router.route(host="testserver").pass_through()
        yield TestClient(app)
    set_object_store_for_testing(None)


def _headers(
    *,
    sub: str = "op-alice",
    role: TenantRole = TenantRole.OPERATOR,
    tenant: uuid.UUID = _TENANT_A,
    principal_kind: str | None = None,
) -> dict[str, str]:
    token = mint_token(
        _KEY, sub=sub, tenant_role=role.value, tenant_id=str(tenant), principal_kind=principal_kind
    )
    return {"Authorization": f"Bearer {token}"}


def _tool_use_line(name: str, arguments: dict[str, Any]) -> bytes:
    block = {"type": "tool_use", "id": "t1", "name": name, "input": arguments}
    entry = {"type": "assistant", "message": {"content": [block]}}
    return json.dumps(entry).encode() + b"\n"


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


_NO_START: Any = object()


def _upload(
    client: TestClient,
    *lines: bytes,
    started_at: datetime | None = None,
    app_name: str = "claude-code",
) -> Any:
    """Upload one session. With no ``started_at`` it started 10 minutes ago."""
    data = {"session_id": _SID, "project": "infra", "app": app_name, "state": "ended"}
    if started_at is not _NO_START:
        start = started_at or datetime.now(UTC) - timedelta(minutes=10)
        data["started_at"] = start.isoformat()
    resp = client.post(
        "/api/v1/sessions",
        data=data,
        files={"archive": ("s.tar.gz", _archive({f"{_SID}.jsonl": b"".join(lines)}))},
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _audit_row(
    *,
    agent_session_id: uuid.UUID | None,
    op_id: str = "call_operation",
    arguments: dict[str, Any] | None = None,
    operator_sub: str = "op-alice",
    tenant_id: uuid.UUID = _TENANT_A,
    method: str = "MCP",
    work_ref: str | None = None,
    extra: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> uuid.UUID:
    payload: dict[str, Any] = {
        "op_id": op_id,
        "op_class": "read",
        "params_hash": compute_audit_params_hash(_ARGS if arguments is None else arguments),
        **(extra or {}),
    }
    row_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AuditLog(
                id=row_id,
                occurred_at=occurred_at or datetime.now(UTC),
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                method=method,
                path="/mcp",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload=payload,
                agent_session_id=agent_session_id,
                work_ref=work_ref,
            )
        )
    return row_id


def _stored_session_id(client: TestClient, row_id: uuid.UUID, **header_args: Any) -> Any:
    resp = client.get(f"/api/v1/audit/show/{row_id}", headers=_headers(**header_args))
    assert resp.status_code == 200, resp.text
    return resp.json()["stored_session_id"]


def test_tool_calls_in_reads_only_mcp_tool_calls() -> None:
    content = b"".join(
        [
            _tool_use_line("mcp__meho__call_operation", _ARGS),
            _tool_use_line("mcp__plugin_meho_meho__search_operations", {"query": "pods"}),
            _tool_use_line("Bash", {"command": "ls"}),
            _tool_use_line("mcp__no_tool_part", {}),
            b"not json but tool_use\n",
            b'{"type":"user","message":{"content":"hi"}}\n',
        ]
    )
    assert tool_calls_in(content) == [
        ("call_operation", _ARGS),
        ("search_operations", {"query": "pods"}),
    ]
    assert tool_calls_in(content, limit=1) == [("call_operation", _ARGS)]


@pytest.mark.asyncio
async def test_an_mcp_call_of_an_uploaded_session_links_to_it(client: TestClient) -> None:
    mcp_session = uuid.uuid4()
    row_id = await _audit_row(agent_session_id=mcp_session, work_ref="CHG-7")
    body = _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    assert body["outcome"] == "created"

    assert _stored_session_id(client, row_id) == _SID
    detail = client.get(f"/api/v1/sessions/{_SID}", headers=_headers()).json()
    assert detail["mcp_sessions"] == [{"mcp_session_id": str(mcp_session), "work_ref": "CHG-7"}]
    async with get_sessionmaker()() as session:
        upload_rows = [
            row
            for row in await session.scalars(select(AuditLog))
            if (row.payload or {}).get("op_id") == "sessions.upload"
        ]
    assert [row.payload["linked_mcp_sessions"] for row in upload_rows] == [1]
    # Only the show answer has the field; a query answer keeps its old shape.
    query = client.post("/api/v1/audit/query", json={}, headers=_headers())
    assert query.status_code == 200, query.text
    assert query.json()["rows"]
    assert all("stored_session_id" not in row for row in query.json()["rows"])


@pytest.mark.asyncio
async def test_a_call_of_the_same_mcp_session_links_also_without_its_own_tool_line(
    client: TestClient,
) -> None:
    mcp_session = uuid.uuid4()
    await _audit_row(agent_session_id=mcp_session)
    other_call = await _audit_row(agent_session_id=mcp_session, arguments={"query": "x"})
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    assert _stored_session_id(client, other_call) == _SID


@pytest.mark.asyncio
async def test_rows_that_do_not_match_get_no_link(client: TestClient) -> None:
    other_args = await _audit_row(agent_session_id=uuid.uuid4(), arguments={"x": 1})
    other_tool = await _audit_row(agent_session_id=uuid.uuid4(), op_id="search_operations")
    other_person = await _audit_row(agent_session_id=uuid.uuid4(), operator_sub="op-bob")
    other_tenant = await _audit_row(agent_session_id=uuid.uuid4(), tenant_id=_TENANT_B)
    not_mcp = await _audit_row(agent_session_id=uuid.uuid4(), method="DISPATCH")
    started = datetime.now(UTC)
    too_early = await _audit_row(
        agent_session_id=uuid.uuid4(), occurred_at=started - timedelta(hours=1)
    )
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS), started_at=started)

    for row_id in (other_args, other_tool, other_person, not_mcp, too_early):
        assert _stored_session_id(client, row_id) is None
    assert _stored_session_id(client, other_tenant, tenant=_TENANT_B) is None
    async with get_sessionmaker()() as session:
        assert list(await session.scalars(select(AgentSessionLink))) == []


@pytest.mark.asyncio
async def test_a_call_that_two_parallel_mcp_sessions_share_names_neither(
    client: TestClient,
) -> None:
    status: dict[str, Any] = {}
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()
    # The same person runs two agents at the same time. Both call
    # meho_status {}; only session A also lists the pods.
    await _audit_row(agent_session_id=session_a, op_id="meho_status", arguments=status)
    await _audit_row(agent_session_id=session_b, op_id="meho_status", arguments=status)
    row_a = await _audit_row(agent_session_id=session_a)
    row_b = await _audit_row(agent_session_id=session_b, arguments={"query": "nodes"})
    _upload(
        client,
        _tool_use_line("mcp__meho__meho_status", status),
        _tool_use_line("mcp__meho__call_operation", _ARGS),
    )

    assert _stored_session_id(client, row_a) == _SID
    assert _stored_session_id(client, row_b) is None
    async with get_sessionmaker()() as session:
        links = list(await session.scalars(select(AgentSessionLink)))
    assert [link.mcp_session_id for link in links] == [session_a]


@pytest.mark.asyncio
async def test_only_a_shared_call_links_no_session(client: TestClient) -> None:
    status: dict[str, Any] = {}
    row_a = await _audit_row(agent_session_id=uuid.uuid4(), op_id="meho_status", arguments=status)
    row_b = await _audit_row(agent_session_id=uuid.uuid4(), op_id="meho_status", arguments=status)
    body = _upload(client, _tool_use_line("mcp__meho__meho_status", status))
    assert body["outcome"] == "created"
    for row_id in (row_a, row_b):
        assert _stored_session_id(client, row_id) is None


@pytest.mark.asyncio
async def test_a_session_with_no_start_time_links_nothing(client: TestClient) -> None:
    row_id = await _audit_row(agent_session_id=uuid.uuid4())
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS), started_at=_NO_START)
    assert _stored_session_id(client, row_id) is None


@pytest.mark.asyncio
async def test_a_call_after_the_upload_time_does_not_match(client: TestClient) -> None:
    # A session that has not ended yet ends at the latest at the upload.
    later = await _audit_row(
        agent_session_id=uuid.uuid4(), occurred_at=datetime.now(UTC) + timedelta(hours=1)
    )
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    assert _stored_session_id(client, later) is None


@pytest.mark.asyncio
async def test_an_upload_with_the_hosted_run_app_name_is_not_a_hosted_run(
    client: TestClient,
) -> None:
    mcp_session = uuid.uuid4()
    await _audit_row(agent_session_id=mcp_session)
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS), app_name=HOSTED_RUN_APP)
    async with get_sessionmaker()() as session:
        link = await stored_session_for(
            session, tenant_id=_TENANT_A, agent_session_id=mcp_session, payload={}
        )
    assert link is not None
    assert link.session_id == _SID
    assert link.hosted_run is False


@pytest.mark.asyncio
async def test_a_reader_without_the_session_read_right_sees_no_link(client: TestClient) -> None:
    row_id = await _audit_row(agent_session_id=uuid.uuid4())
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    assert _stored_session_id(client, row_id, sub="op-carol") == _SID
    agent = {"principal_kind": "agent", "role": TenantRole.TENANT_ADMIN}
    resp = client.get(f"/api/v1/audit/show/{row_id}", headers=_headers(**agent))
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored_session_id"] is None
    assert _SID not in resp.text


@pytest.mark.asyncio
async def test_a_hosted_run_tool_call_links_to_the_run_session(client: TestClient) -> None:
    run_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        add_run_session(
            session, run_id=run_id, tenant_id=_TENANT_A, started_by="op-alice", agent_name="ops"
        )
    row_id = await _audit_row(agent_session_id=run_id, method="DISPATCH")
    assert _stored_session_id(client, row_id) == str(run_id)
    # The same id in another tenant is another tenant's row: no link.
    foreign = await _audit_row(agent_session_id=run_id, tenant_id=_TENANT_B)
    assert _stored_session_id(client, foreign, tenant=_TENANT_B) is None


@pytest.mark.asyncio
async def test_rows_with_no_stored_session_or_a_cli_session_get_no_link(
    client: TestClient,
) -> None:
    mcp_session = uuid.uuid4()
    await _audit_row(agent_session_id=mcp_session)
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    no_session = await _audit_row(agent_session_id=None)
    unknown = await _audit_row(agent_session_id=uuid.uuid4(), arguments={"y": 2})
    cli_row = await _audit_row(
        agent_session_id=mcp_session, method="DISPATCH", extra={SESSION_FROM_CLIENT_KEY: True}
    )
    for row_id in (no_session, unknown, cli_row):
        assert _stored_session_id(client, row_id) is None


@pytest.mark.asyncio
async def test_deleting_the_session_removes_its_links(client: TestClient) -> None:
    row_id = await _audit_row(agent_session_id=uuid.uuid4())
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    resp = client.delete(f"/api/v1/sessions/{_SID}", headers=_headers(role=TenantRole.TENANT_ADMIN))
    assert resp.status_code == 204
    assert _stored_session_id(client, row_id) is None
    async with get_sessionmaker()() as session:
        assert list(await session.scalars(select(AgentSessionLink))) == []
