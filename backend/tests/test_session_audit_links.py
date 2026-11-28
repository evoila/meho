# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An audit row links to the stored session it belongs to (#4904).

* An uploaded session: MEHO finds the audit rows of its MCP calls (same
  person, same tool, same arguments hash, inside the session's time) and
  stores their MCP session id. ``GET /api/v1/audit/show/{id}`` then names
  the session in ``stored_session_id``.
* A hosted agent run: its tool calls carry the run id, which is the id of
  its session.
* A ``meho`` CLI command: each of its audit rows links on its own (same
  person, a CLI row, its ``op_id`` is a word of the command, written while
  the command ran), since many conversations share one login session.
* No link for a reader who may not read sessions, for a row with no stored
  session, and for a CLI login session id alone.
* All of it is behind the switch ``AUDIT_SESSION_LINKS``; with it off the
  answers are the same as before.

SQLite checks foreign keys here, like PostgreSQL, so a link row written
before its session row fails the upload. The object storage is an
in-memory fake. Every value in this file is made up.
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

from meho_backplane.audit_query import AuditEntry
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AgentRunTrigger, AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.operations._audit import SESSION_FROM_CLIENT_KEY
from meho_backplane.operations._validate import compute_audit_params_hash
from meho_backplane.session_store.audit_links import stored_session_for
from meho_backplane.session_store.hosted_runs import HOSTED_RUN_APP, add_run_session
from meho_backplane.session_store.models import AgentSessionAuditLink, AgentSessionLink
from meho_backplane.session_store.object_store import set_object_store_for_testing
from meho_backplane.session_store.tool_calls import cli_calls_in, tool_calls_in
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
    monkeypatch.setenv("AUDIT_SESSION_LINKS", "true")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _enforce_sqlite_foreign_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Check foreign keys like PostgreSQL does, so a link row can never come before its session."""
    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()


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


def _bash_lines(command: str, started: datetime, ended: datetime, *, call_id: str = "b1") -> bytes:
    """A ``Bash`` call line and its answer line, with their times."""
    block = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    call = {"type": "assistant", "timestamp": started.isoformat(), "message": {"content": [block]}}
    answer_block = {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}
    answer = {
        "type": "user",
        "timestamp": ended.isoformat(),
        "message": {"content": [answer_block]},
    }
    return json.dumps(call).encode() + b"\n" + json.dumps(answer).encode() + b"\n"


def _upload(
    client: TestClient,
    *lines: bytes,
    started_at: datetime | None = None,
    app_name: str = "claude-code",
    session_id: str = _SID,
) -> Any:
    """Upload one session. With no ``started_at`` it started 10 minutes ago."""
    data = {"session_id": session_id, "project": "infra", "app": app_name, "state": "ended"}
    if started_at is not _NO_START:
        start = started_at or datetime.now(UTC) - timedelta(minutes=10)
        data["started_at"] = start.isoformat()
    resp = client.post(
        "/api/v1/sessions",
        data=data,
        files={"archive": ("s.tar.gz", _archive({f"{session_id}.jsonl": b"".join(lines)}))},
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
    return resp.json().get("stored_session_id")


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
        assert list(await session.scalars(select(AgentSessionAuditLink))) == []


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
            session,
            tenant_id=_TENANT_A,
            audit_id=uuid.uuid4(),
            agent_session_id=mcp_session,
            payload={},
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
    assert "stored_session_id" not in resp.json()
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
async def test_a_hosted_run_session_names_the_run_and_its_work_ref(client: TestClient) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="tenant-a", name="Tenant A"))
        await session.flush()
        run = await run_lifecycle.create_run(
            session,
            tenant_id=_TENANT_A,
            identity_sub="op-alice",
            trigger=AgentRunTrigger.DIRECT,
            model_tier="cheap",
            work_ref="CHG-9",
        )
        add_run_session(
            session, run_id=run.id, tenant_id=_TENANT_A, started_by="op-alice", agent_name="ops"
        )
        run_id = run.id
        await session.commit()
    detail = client.get(f"/api/v1/sessions/{run_id}", headers=_headers())
    assert detail.status_code == 200, detail.text
    assert detail.json()["mcp_sessions"] == [{"mcp_session_id": str(run_id), "work_ref": "CHG-9"}]


@pytest.mark.asyncio
async def test_an_upload_named_like_a_hosted_run_lists_no_run(client: TestClient) -> None:
    _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS), app_name=HOSTED_RUN_APP)
    detail = client.get(f"/api/v1/sessions/{_SID}", headers=_headers()).json()
    assert detail["mcp_sessions"] == []


@pytest.mark.asyncio
async def test_each_cli_call_links_to_its_own_conversation_of_one_login(
    client: TestClient,
) -> None:
    login = uuid.uuid4()
    other_sid = "0b5e2a8c-7777-4222-8333-444455556666"
    base = datetime.now(UTC) - timedelta(minutes=5)
    cli = {"method": "DISPATCH", "extra": {SESSION_FROM_CLIENT_KEY: True}}
    # Conversation A lists pods at minute 0; conversation B lists nodes at
    # minute 2. Both run with the same login session id.
    pods = await _audit_row(
        agent_session_id=login, op_id="pods.list", occurred_at=base + timedelta(seconds=1), **cli
    )
    nodes = await _audit_row(
        agent_session_id=login,
        op_id="nodes.list",
        occurred_at=base + timedelta(minutes=2, seconds=1),
        **cli,
    )
    # A row of the same login outside both commands, and another person's row.
    idle = await _audit_row(
        agent_session_id=login, op_id="pods.list", occurred_at=base + timedelta(minutes=4), **cli
    )
    bob = await _audit_row(
        agent_session_id=uuid.uuid4(),
        op_id="pods.list",
        operator_sub="op-bob",
        occurred_at=base + timedelta(seconds=1),
        **cli,
    )
    a_lines = _bash_lines("meho call k8s pods.list --json", base, base + timedelta(seconds=2))
    b_lines = _bash_lines(
        "cd /tmp && meho call k8s nodes.list",
        base + timedelta(minutes=2),
        base + timedelta(minutes=2, seconds=2),
    )
    first = _upload(client, a_lines, started_at=base - timedelta(minutes=1))
    _upload(client, b_lines, started_at=base - timedelta(minutes=1), session_id=other_sid)

    assert _stored_session_id(client, pods) == _SID
    assert _stored_session_id(client, nodes) == other_sid
    assert _stored_session_id(client, idle) is None
    assert _stored_session_id(client, bob) is None
    assert first["outcome"] == "created"
    async with get_sessionmaker()() as session:
        linked = {row.audit_id for row in await session.scalars(select(AgentSessionAuditLink))}
    assert linked == {pods, nodes}


@pytest.mark.asyncio
async def test_a_cli_row_that_two_conversations_claim_links_to_neither(
    client: TestClient,
) -> None:
    login = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(minutes=5)
    row = await _audit_row(
        agent_session_id=login,
        op_id="pods.list",
        method="DISPATCH",
        extra={SESSION_FROM_CLIENT_KEY: True},
        occurred_at=base + timedelta(seconds=1),
    )
    lines = _bash_lines("meho call k8s pods.list", base, base + timedelta(seconds=2))
    _upload(client, lines, started_at=base - timedelta(minutes=1))
    assert _stored_session_id(client, row) == _SID
    _upload(
        client,
        lines,
        started_at=base - timedelta(minutes=1),
        session_id="0b5e2a8c-8888-4222-8333-444455556666",
    )
    assert _stored_session_id(client, row) is None


def test_cli_calls_in_reads_only_finished_meho_commands() -> None:
    start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(seconds=3)
    no_answer = {
        "type": "assistant",
        "timestamp": start.isoformat(),
        "message": {
            "content": [
                {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "meho a"}}
            ]
        },
    }
    content = b"".join(
        [
            _bash_lines("meho call k8s pods.list", start, end, call_id="a"),
            _bash_lines("ls -la", start, end, call_id="b"),
            _bash_lines("echo mehoo x", start, end, call_id="c"),
            json.dumps(no_answer).encode() + b"\n",
            b"not json but tool_use\n",
        ]
    )
    (call,) = cli_calls_in(content)
    assert "pods.list" in call.words
    assert (call.started_at, call.ended_at) == (start, end)
    # A line with no time zone gives no time: the command is left out.
    naive = _bash_lines("meho x", start.replace(tzinfo=None), end.replace(tzinfo=None))
    assert cli_calls_in(naive) == []
    assert cli_calls_in(content, limit=0) == []


@pytest.mark.asyncio
async def test_with_the_switch_off_nothing_links_and_answers_are_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_SESSION_LINKS", "false")
    get_settings.cache_clear()
    mcp_session = uuid.uuid4()
    row_id = await _audit_row(agent_session_id=mcp_session, work_ref="CHG-7")
    body = _upload(client, _tool_use_line("mcp__meho__call_operation", _ARGS))
    assert body["outcome"] == "created"

    show = client.get(f"/api/v1/audit/show/{row_id}", headers=_headers())
    assert show.status_code == 200, show.text
    # ``secret_read_as`` has its own switch, off here. A row with no kind of
    # caller (``CUSTOM_POLICY`` off) has no ``caller_kind`` field.
    assert set(show.json()) == set(AuditEntry.model_fields) - {"secret_read_as", "caller_kind"}
    detail = client.get(f"/api/v1/sessions/{_SID}", headers=_headers()).json()
    assert "mcp_sessions" not in detail
    async with get_sessionmaker()() as session:
        assert list(await session.scalars(select(AgentSessionLink))) == []
        assert list(await session.scalars(select(AgentSessionAuditLink))) == []
        upload_rows = [
            row
            for row in await session.scalars(select(AuditLog))
            if (row.payload or {}).get("op_id") == "sessions.upload"
        ]
    assert [
        set(row.payload) & {"linked_mcp_sessions", "linked_cli_rows"} for row in upload_rows
    ] == [set()]


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
        assert list(await session.scalars(select(AgentSessionAuditLink))) == []
