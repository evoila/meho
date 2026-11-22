# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A ``meho`` CLI command of an uploaded session links its audit rows (#4904).

* A command that picks its operation itself (``meho vmware vm list`` sends
  ``GET:/vcenter/vm``) links by the command path the CLI sends in the
  ``Meho-Command`` header. The test sends the header through the real
  request path, so the audit middleware stores it on the row.
* With the switch ``AUDIT_SESSION_LINKS`` off the row does not get the path.
* An upload can end between a command and its answer. The next upload
  still links the command: MEHO finds its call line in the part stored
  before.

Every value in this file is made up.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.operations._audit import (
    CLI_COMMAND_KEY,
    SESSION_FROM_CLIENT_KEY,
    parse_command_header,
)
from meho_backplane.session_store.object_store import set_object_store_for_testing
from meho_backplane.session_store.tool_calls import cli_calls_in
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_SID = "0b5e2a8c-2222-4222-8333-444455556666"
_KEY = make_rsa_keypair("kid-session-links-cli")


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
    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()
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


def _headers(**extra: str) -> dict[str, str]:
    token = mint_token(
        _KEY, sub="op-alice", tenant_role=TenantRole.OPERATOR.value, tenant_id=str(_TENANT)
    )
    return {"Authorization": f"Bearer {token}", **extra}


def _line(entry_type: str, when: datetime, block: dict[str, Any]) -> bytes:
    entry = {"type": entry_type, "timestamp": when.isoformat(), "message": {"content": [block]}}
    return json.dumps(entry).encode() + b"\n"


def _bash_call(command: str, when: datetime, call_id: str) -> bytes:
    block = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    return _line("assistant", when, block)


def _bash_answer(when: datetime, call_id: str) -> bytes:
    return _line("user", when, {"type": "tool_result", "tool_use_id": call_id, "content": "ok"})


def _upload(client: TestClient, content: bytes, *, state: str, started_at: datetime) -> Any:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{_SID}.jsonl")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    resp = client.post(
        "/api/v1/sessions",
        data={
            "session_id": _SID,
            "project": "infra",
            "app": "claude-code",
            "state": state,
            "started_at": started_at.isoformat(),
        },
        files={"archive": ("s.tar.gz", buffer.getvalue())},
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _cli_rows(login: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(AuditLog).where(AuditLog.agent_session_id == login))
        return list(rows)


def _stored_session_id(client: TestClient, row_id: uuid.UUID) -> Any:
    resp = client.get(f"/api/v1/audit/show/{row_id}", headers=_headers())
    assert resp.status_code == 200, resp.text
    return resp.json().get("stored_session_id")


async def _run_vm_list(client: TestClient, login: uuid.UUID) -> AuditLog:
    """Send the request ``meho vmware vm list --target vc-1`` sends, with its headers."""
    client.post(
        "/api/v1/operations/call",
        json={"connector_id": "vmware-rest-9.0", "op_id": "GET:/vcenter/vm", "target": "vc-1"},
        headers=_headers(**{"Meho-Session-Id": str(login), "Meho-Command": "vmware vm list"}),
    )
    rows = await _cli_rows(login)
    assert rows, "the request wrote no audit row"
    return rows[-1]


@pytest.mark.asyncio
async def test_a_command_that_picks_its_operation_links_by_its_path(client: TestClient) -> None:
    login = uuid.uuid4()
    started = datetime.now(UTC) - timedelta(seconds=5)
    row = await _run_vm_list(client, login)
    ended = datetime.now(UTC) + timedelta(seconds=1)

    assert row.payload[SESSION_FROM_CLIENT_KEY] is True
    assert row.payload[CLI_COMMAND_KEY] == "vmware vm list"
    content = _bash_call("meho vmware vm list --target vc-1", started, "b1") + _bash_answer(
        ended, "b1"
    )
    body = _upload(client, content, state="ended", started_at=started - timedelta(minutes=1))

    assert body["outcome"] == "created"
    assert _stored_session_id(client, row.id) == _SID


@pytest.mark.asyncio
async def test_another_command_does_not_link_the_row(client: TestClient) -> None:
    login = uuid.uuid4()
    started = datetime.now(UTC) - timedelta(seconds=5)
    row = await _run_vm_list(client, login)
    ended = datetime.now(UTC) + timedelta(seconds=1)
    content = _bash_call("meho vmware host list", started, "b1") + _bash_answer(ended, "b1")
    _upload(client, content, state="ended", started_at=started - timedelta(minutes=1))

    assert _stored_session_id(client, row.id) is None


@pytest.mark.asyncio
async def test_with_the_switch_off_the_row_gets_no_command_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_SESSION_LINKS", "false")
    get_settings.cache_clear()
    row = await _run_vm_list(client, uuid.uuid4())

    assert row.payload[SESSION_FROM_CLIENT_KEY] is True
    assert CLI_COMMAND_KEY not in row.payload


@pytest.mark.asyncio
async def test_a_command_split_across_two_uploads_still_links(client: TestClient) -> None:
    login = uuid.uuid4()
    started = datetime.now(UTC) - timedelta(seconds=5)
    call_line = _bash_call("meho vmware vm list --target vc-1", started, "b1")
    first = _upload(client, call_line, state="open", started_at=started - timedelta(minutes=1))
    row = await _run_vm_list(client, login)
    answer_line = _bash_answer(datetime.now(UTC) + timedelta(seconds=1), "b1")
    second = _upload(
        client, call_line + answer_line, state="ended", started_at=started - timedelta(minutes=1)
    )

    assert (first["outcome"], second["outcome"]) == ("created", "appended")
    assert _stored_session_id(client, row.id) == _SID


def test_cli_calls_in_finds_the_call_line_in_the_stored_part() -> None:
    start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    earlier = _bash_call("meho vmware vm list", start, "b1") + _bash_call("ls", start, "b2")
    tail = _bash_answer(start + timedelta(seconds=2), "b2") + _bash_answer(
        start + timedelta(seconds=3), "b1"
    )

    assert cli_calls_in(tail) == []
    (call,) = cli_calls_in(tail, earlier=earlier)
    assert (call.started_at, call.ended_at) == (start, start + timedelta(seconds=3))
    assert {"vmware", "vmware vm", "vmware vm list"} <= call.paths


def test_command_paths_skip_flags_and_take_each_meho_word() -> None:
    start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    command = "/usr/local/bin/meho --json audit show x && meho k8s pods list"
    content = _bash_call(command, start, "b1") + _bash_answer(start, "b1")
    (call,) = cli_calls_in(content)

    assert {"audit show", "k8s pods list"} <= call.paths
    assert "json" not in " ".join(call.paths).split()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"vmware vm list", "vmware vm list"),
        (b" vcf-automation catalog item list ", "vcf-automation catalog item list"),
        (b"vmware  vm", None),
        (b"Vmware vm", None),
        (b"vmware vm list --target x", None),
        (b"a" * 201, None),
        ("änderung".encode(), None),
    ],
)
def test_parse_command_header_keeps_only_a_command_path(raw: bytes, expected: str | None) -> None:
    assert parse_command_header(raw) == expected
