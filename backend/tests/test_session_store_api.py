# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests of ``/api/v1/sessions`` (the agent session store, #4901).

The object storage is an in-memory fake (``set_object_store_for_testing``);
the S3 client itself is tested in ``test_session_store_units.py``. Every
secret in this file is made up.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.session_store.models import AgentSessionRecord, AgentSessionUpload
from meho_backplane.session_store.object_store import ObjectStoreError, set_object_store_for_testing
from meho_backplane.session_store.retention import CLEANUP_PATH, cleanup_once
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SID = "0b5e2a8c-1111-4222-8333-444455556666"
_KEY = make_rsa_keypair("kid-sessions")
_LINE_1 = b'{"type":"user","message":{"content":"list the pods"}}\n'
_LINE_2 = b'{"type":"assistant","message":{"content":"three pods run"}}\n'
_LINE_3 = b'{"type":"user","message":{"content":"thanks"}}\n'


class _FakeStore:
    """In-memory object storage."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    async def put(self, key: str, data: bytes) -> None:
        if self.fail:
            raise ObjectStoreError("object storage failed put: test")
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, keys: Sequence[str]) -> None:
        if self.fail:
            raise ObjectStoreError("object storage failed delete: test")
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
def store() -> Iterator[_FakeStore]:
    fake = _FakeStore()
    set_object_store_for_testing(fake)
    yield fake
    set_object_store_for_testing(None)


@pytest.fixture
def client(store: _FakeStore) -> Iterator[TestClient]:
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(_KEY))
        router.route(host="testserver").pass_through()
        yield TestClient(app)


def _headers(
    *,
    sub: str = "op-alice",
    role: TenantRole = TenantRole.OPERATOR,
    tenant: UUID = _TENANT_A,
    principal_kind: str | None = None,
) -> dict[str, str]:
    token = mint_token(
        _KEY, sub=sub, tenant_role=role.value, tenant_id=str(tenant), principal_kind=principal_kind
    )
    return {"Authorization": f"Bearer {token}"}


_ADMIN = {"role": TenantRole.TENANT_ADMIN}


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _unpack(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isreg()}  # type: ignore[union-attr]


def _upload(
    client: TestClient,
    files: dict[str, bytes],
    *,
    session_id: str = _SID,
    state: str = "open",
    raw: bytes | None = None,
    **header_args: Any,
) -> Any:
    return client.post(
        "/api/v1/sessions",
        data={"session_id": session_id, "project": "infra", "app": "claude-code", "state": state},
        files={"archive": ("session.tar.gz", raw if raw is not None else _archive(files))},
        headers=_headers(**header_args),
    )


def _transcript(*lines: bytes) -> dict[str, bytes]:
    return {f"{_SID}.jsonl": b"".join(lines)}


async def _audit(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = list(await session.scalars(select(AuditLog).order_by(AuditLog.occurred_at)))
    return [row for row in rows if (row.payload or {}).get("op_id") == op_id]


async def _records() -> list[AgentSessionRecord]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(AgentSessionRecord)))


@pytest.mark.asyncio
async def test_upload_stores_the_files_and_writes_the_index(
    client: TestClient, store: _FakeStore
) -> None:
    files = {**_transcript(_LINE_1), f"{_SID}/subagents/agent-1.jsonl": _LINE_2}
    resp = _upload(client, files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "created"
    assert body["seq"] == 1
    assert body["state"] == "open"
    assert body["quarantined"] is False

    [key] = store.objects
    assert key.startswith(f"tenants/{_TENANT_A}/sessions/")
    assert _unpack(store.objects[key]) == files

    [record] = await _records()
    assert record.session_id == _SID
    assert record.uploaded_by_sub == "op-alice"
    assert record.project == "infra"
    assert record.app == "claude-code"
    assert set(record.files) == set(files)
    assert len(await _audit("sessions.upload")) == 1


@pytest.mark.asyncio
async def test_same_bytes_again_change_nothing(client: TestClient, store: _FakeStore) -> None:
    files = _transcript(_LINE_1)
    assert _upload(client, files).status_code == 200
    [before] = await _records()
    resp = _upload(client, files)
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "unchanged"
    assert resp.json()["seq"] is None
    assert len(store.objects) == 1
    [after] = await _records()
    assert (after.upload_count, after.last_seen_at, after.files) == (
        before.upload_count,
        before.last_seen_at,
        before.files,
    )
    # A retry is still an upload, so it is on the record.
    assert len(await _audit("sessions.upload")) == 2


@pytest.mark.asyncio
async def test_open_session_three_nights_is_one_record_with_three_uploads(
    client: TestClient, store: _FakeStore
) -> None:
    nights = [(_LINE_1,), (_LINE_1, _LINE_2), (_LINE_1, _LINE_2, _LINE_3)]
    outcomes = [_upload(client, _transcript(*lines)).json()["outcome"] for lines in nights]
    assert outcomes == ["created", "appended", "appended"]

    [record] = await _records()
    assert record.state == "open"
    assert record.upload_count == 3
    assert len(await _audit("sessions.upload")) == 3
    tails = [_unpack(store.objects[key])[f"{_SID}.jsonl"] for key in sorted(store.objects)]
    assert tails == [_LINE_1, _LINE_2, _LINE_3]

    detail = client.get(f"/api/v1/sessions/{_SID}", headers=_headers()).json()
    assert [u["files"][f"{_SID}.jsonl"]["offset"] for u in detail["uploads"]] == [
        0,
        len(_LINE_1),
        len(_LINE_1 + _LINE_2),
    ]


@pytest.mark.asyncio
async def test_a_retry_of_an_older_upload_changes_nothing(
    client: TestClient, store: _FakeStore
) -> None:
    _upload(client, _transcript(_LINE_1))
    _upload(client, _transcript(_LINE_1, _LINE_2))
    resp = _upload(client, _transcript(_LINE_1))
    assert resp.json()["outcome"] == "unchanged"
    [record] = await _records()
    assert record.upload_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        pytest.param((_LINE_2,), id="other-content"),
        pytest.param((_LINE_1[:-5] + b"!!!}\n", _LINE_2), id="edited-stored-part"),
        pytest.param((_LINE_1[:10],), id="shorter-unknown"),
    ],
)
async def test_a_changed_stored_part_is_a_conflict(
    client: TestClient, store: _FakeStore, changed: tuple[bytes, ...]
) -> None:
    _upload(client, _transcript(_LINE_1))
    resp = _upload(client, _transcript(*changed))
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"reason": "session_conflict", "paths": [f"{_SID}.jsonl"]}
    assert len(store.objects) == 1
    [record] = await _records()
    assert record.upload_count == 1
    assert [row.status_code for row in await _audit("sessions.upload")] == [200, 409]


@pytest.mark.asyncio
async def test_another_person_cannot_add_to_a_session(client: TestClient) -> None:
    _upload(client, _transcript(_LINE_1))
    resp = _upload(client, _transcript(_LINE_1, _LINE_2), sub="op-bob")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "session_owned_by_another_person"


@pytest.mark.asyncio
async def test_an_end_sets_the_state_and_an_unfinished_session_keeps_it(
    client: TestClient,
) -> None:
    _upload(client, _transcript(_LINE_1))
    resp = _upload(client, _transcript(_LINE_1), state="ended")
    assert resp.json()["outcome"] == "state_changed"
    assert resp.json()["state"] == "ended"
    # A late retry that says "open" without new bytes does not reopen it.
    assert _upload(client, _transcript(_LINE_1)).json()["state"] == "ended"
    _upload(client, _transcript(_LINE_3), session_id="crashed-session")
    resp = _upload(client, _transcript(_LINE_3), session_id="crashed-session", state="unfinished")
    assert resp.json()["state"] == "unfinished"


@pytest.mark.asyncio
async def test_a_known_secret_is_replaced_before_it_is_stored(
    client: TestClient, store: _FakeStore
) -> None:
    line = b'{"type":"tool","content":"curl -H \\"Authorization: Bearer madeUpToken123456\\""}\n'
    resp = _upload(client, _transcript(line))
    assert resp.json()["redactions"] == 1
    assert resp.json()["quarantined"] is False
    [stored] = store.objects.values()
    content = _unpack(stored)[f"{_SID}.jsonl"]
    assert b"madeUpToken123456" not in content
    assert b"[REDACTED:authorization_header]" in content


@pytest.mark.asyncio
async def test_a_planted_secret_goes_to_quarantine_and_only_an_admin_opens_it(
    client: TestClient, store: _FakeStore
) -> None:
    planted = b'{"type":"tool_use","input":{"user":"svc","password":"Planted-Not-Real-1"}}\n'
    resp = _upload(client, _transcript(_LINE_1, planted))
    assert resp.json()["quarantined"] is True
    [key] = store.objects
    assert key.startswith(f"quarantine/tenants/{_TENANT_A}/sessions/")

    url = f"/api/v1/sessions/{_SID}/uploads/1"
    denied = client.get(url, headers=_headers())
    assert denied.status_code == 403
    assert denied.json()["detail"] == "session_upload_quarantined"
    assert "Planted-Not-Real-1" not in denied.text
    opened = client.get(url, headers=_headers(**_ADMIN))
    assert opened.status_code == 200
    assert _unpack(opened.content)[f"{_SID}.jsonl"].endswith(planted)
    assert [row.status_code for row in await _audit("sessions.download")] == [403, 200]
    assert all("Planted" not in str(row.payload) for row in await _audit("sessions.upload"))


@pytest.mark.asyncio
async def test_list_show_and_download_are_audited(client: TestClient) -> None:
    _upload(client, _transcript(_LINE_1))
    listed = client.get("/api/v1/sessions", headers=_headers())
    assert listed.status_code == 200
    assert [item["session_id"] for item in listed.json()["items"]] == [_SID]
    assert listed.json()["next_cursor"] is None
    shown = client.get(f"/api/v1/sessions/{_SID}", headers=_headers())
    assert shown.status_code == 200
    assert shown.json()["files"][f"{_SID}.jsonl"]["size"] == len(_LINE_1)
    assert client.get(f"/api/v1/sessions/{_SID}/uploads/1", headers=_headers()).status_code == 200
    for op_id in ("sessions.list", "sessions.read", "sessions.download"):
        rows = await _audit(op_id)
        assert len(rows) == 1, op_id
        assert rows[0].operator_sub == "op-alice"
        assert rows[0].tenant_id == _TENANT_A


@pytest.mark.asyncio
async def test_list_pages_to_the_end(client: TestClient) -> None:
    for n in range(3):
        _upload(client, _transcript(_LINE_1), session_id=f"session-{n}")
    first = client.get("/api/v1/sessions?limit=2", headers=_headers()).json()
    assert len(first["items"]) == 2
    second = client.get(
        f"/api/v1/sessions?limit=2&cursor={first['next_cursor']}", headers=_headers()
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    ids = {i["session_id"] for i in first["items"] + second["items"]}
    assert ids == {"session-0", "session-1", "session-2"}


@pytest.mark.asyncio
async def test_without_the_read_right_nothing_is_listed_or_read(client: TestClient) -> None:
    _upload(client, _transcript(_LINE_1))
    reader = {"role": TenantRole.READ_ONLY}
    assert client.get("/api/v1/sessions", headers=_headers(**reader)).status_code == 403
    assert client.get(f"/api/v1/sessions/{_SID}", headers=_headers(**reader)).status_code == 403
    url = f"/api/v1/sessions/{_SID}/uploads/1"
    assert client.get(url, headers=_headers(**reader)).status_code == 403
    # A reader may still upload their own session.
    assert _upload(client, _transcript(_LINE_1), session_id="mine", **reader).status_code == 200


@pytest.mark.asyncio
async def test_a_tenant_never_sees_another_tenants_sessions(client: TestClient) -> None:
    _upload(client, _transcript(_LINE_1))
    other = {"tenant": _TENANT_B, "role": TenantRole.TENANT_ADMIN}
    assert client.get("/api/v1/sessions", headers=_headers(**other)).json()["items"] == []
    assert client.get(f"/api/v1/sessions/{_SID}", headers=_headers(**other)).status_code == 404
    url = f"/api/v1/sessions/{_SID}/uploads/1"
    assert client.get(url, headers=_headers(**other)).status_code == 404
    assert client.delete(f"/api/v1/sessions/{_SID}", headers=_headers(**other)).status_code == 404
    # The same session id in tenant B is a separate session.
    resp = _upload(client, _transcript(_LINE_2), tenant=_TENANT_B)
    assert resp.json()["outcome"] == "created"
    assert len(await _records()) == 2


@pytest.mark.asyncio
async def test_only_an_admin_deletes_and_the_files_go_too(
    client: TestClient, store: _FakeStore
) -> None:
    _upload(client, _transcript(_LINE_1))
    _upload(client, _transcript(_LINE_1, _LINE_2))
    url = f"/api/v1/sessions/{_SID}"
    assert client.delete(url, headers=_headers()).status_code == 403
    resp = client.delete(url, headers=_headers(**_ADMIN))
    assert resp.status_code == 204
    assert store.objects == {}
    assert await _records() == []
    [row] = await _audit("sessions.delete")
    assert row.payload["objects"] == 2
    assert client.delete(url, headers=_headers(**_ADMIN)).status_code == 404


@pytest.mark.asyncio
async def test_a_failed_storage_call_leaves_the_index_unchanged(
    client: TestClient, store: _FakeStore
) -> None:
    store.fail = True
    resp = _upload(client, _transcript(_LINE_1))
    assert resp.status_code == 502
    assert resp.json()["detail"] == "session_store_unavailable"
    assert await _records() == []
    store.fail = False
    _upload(client, _transcript(_LINE_1))
    store.fail = True
    assert client.delete(f"/api/v1/sessions/{_SID}", headers=_headers(**_ADMIN)).status_code == 502
    assert len(await _records()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        pytest.param(b"not an archive", "archive_unreadable", id="not-gzip"),
        pytest.param(_archive({"../etc/passwd": b"x"}), "archive_bad_path", id="dot-dot"),
        pytest.param(_archive({"/abs/file": b"x"}), "archive_bad_path", id="absolute"),
        pytest.param(_archive({}), "archive_empty", id="empty"),
    ],
)
async def test_a_bad_archive_is_refused(
    client: TestClient, store: _FakeStore, raw: bytes, detail: str
) -> None:
    resp = _upload(client, {}, raw=raw)
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail
    assert store.objects == {}
    assert await _records() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"session_id": "../x"}, id="session-id-path"),
        pytest.param({"state": "done"}, id="unknown-state"),
        pytest.param({"started_at": "2026-11-01T10:00:00"}, id="no-time-zone"),
        pytest.param({"project": ""}, id="empty-project"),
    ],
)
async def test_bad_session_fields_are_refused(client: TestClient, fields: dict[str, str]) -> None:
    data = {"session_id": _SID, "project": "infra", "app": "claude-code", **fields}
    resp = client.post(
        "/api/v1/sessions",
        data=data,
        files={"archive": ("s.tar.gz", _archive(_transcript(_LINE_1)))},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert await _records() == []


@pytest.mark.asyncio
async def test_too_large_an_archive_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SESSION_STORE_MAX_UPLOAD_BYTES", "10")
    resp = _upload(client, _transcript(_LINE_1))
    assert resp.status_code == 413
    assert resp.json()["detail"] == "session_archive_too_large"


@pytest.mark.asyncio
async def test_an_agent_login_is_refused(client: TestClient) -> None:
    resp = _upload(client, _transcript(_LINE_1), principal_kind="agent", **_ADMIN)
    assert resp.status_code == 403
    assert await _records() == []


@pytest.mark.asyncio
async def test_not_configured_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    set_object_store_for_testing(None)
    monkeypatch.delenv("SESSION_STORE_BUCKET")
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(_KEY))
        resp = _upload(TestClient(app), _transcript(_LINE_1))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "session_store_not_configured"


@pytest.mark.asyncio
async def test_retention_is_set_per_tenant_by_an_admin(client: TestClient) -> None:
    url = "/api/v1/sessions/retention"
    assert client.get(url, headers=_headers()).status_code == 403
    assert client.get(url, headers=_headers(**_ADMIN)).json() == {
        "retention_days": 90,
        "source": "default",
    }
    resp = client.put(url, json={"retention_days": 7}, headers=_headers(**_ADMIN))
    assert resp.json() == {"retention_days": 7, "source": "tenant"}
    other = client.get(url, headers=_headers(tenant=_TENANT_B, **_ADMIN)).json()
    assert other["source"] == "default"
    bad = client.put(url, json={"retention_days": 0}, headers=_headers(**_ADMIN))
    assert bad.status_code == 422
    resp = client.put(url, json={"retention_days": None}, headers=_headers(**_ADMIN))
    assert resp.json()["source"] == "default"
    assert len(await _audit("sessions.retention.set")) == 2


@pytest.mark.asyncio
async def test_cleanup_removes_an_expired_session_and_its_files(
    client: TestClient, store: _FakeStore
) -> None:
    client.put("/api/v1/sessions/retention", json={"retention_days": 7}, headers=_headers(**_ADMIN))
    _upload(client, _transcript(_LINE_1), session_id="old")
    _upload(client, _transcript(_LINE_1), session_id="new")
    _upload(client, _transcript(_LINE_1), session_id="other-tenant", tenant=_TENANT_B)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentSessionRecord)
            .where(AgentSessionRecord.session_id.in_(["old", "other-tenant"]))
            .values(last_seen_at=long_ago)
        )
        await session.commit()

    assert await cleanup_once() == 1
    assert sorted(r.session_id for r in await _records()) == ["new", "other-tenant"]
    assert len(store.objects) == 2
    async with get_sessionmaker()() as session:
        uploads = list(await session.scalars(select(AgentSessionUpload)))
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.path == CLEANUP_PATH)))
    assert len(uploads) == 2
    assert [(row.tenant_id, row.payload["session_id"]) for row in rows] == [(_TENANT_A, "old")]


@pytest.mark.asyncio
async def test_cleanup_keeps_the_rows_when_the_storage_fails(
    client: TestClient, store: _FakeStore
) -> None:
    _upload(client, _transcript(_LINE_1))
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentSessionRecord).values(last_seen_at=datetime.now(UTC) - timedelta(days=400))
        )
        await session.commit()
    store.fail = True
    assert await cleanup_once() == 0
    assert len(await _records()) == 1
    store.fail = False
    assert await cleanup_once() == 1
    assert store.objects == {}
