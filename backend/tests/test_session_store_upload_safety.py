# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of ``upload_session`` itself (#4901): whole lines and uploads at the same time.

The object storage is an in-memory fake. Every secret in this file is made up.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tarfile
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import ClauseElement

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.session_store.config import load_config
from meho_backplane.session_store.models import AgentSessionRecord, AgentSessionUpload
from meho_backplane.session_store.retention import _expired, _remove
from meho_backplane.session_store.schemas import SessionUploadResult
from meho_backplane.session_store.service import delete_session
from meho_backplane.session_store.upload import SessionConflictError, UploadMeta, upload_session
from meho_backplane.settings import get_settings

_TENANT = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_SID = "5d1c0f3e-aaaa-4bbb-8ccc-ddddeeeeffff"
_PATH = f"{_SID}.jsonl"
_LINE_1 = b'{"type":"user","message":{"content":"list the pods"}}\n'


class _Store:
    """In-memory object storage. ``gate`` holds each put until two puts arrived."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.gate: asyncio.Event | None = None
        self._puts = 0

    async def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data
        if self.gate is not None:
            self._puts += 1
            if self._puts >= 2:
                self.gate.set()
            # A database that locks the row lets the second upload wait in the
            # read, so it never reaches put while the first one waits here.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.gate.wait(), timeout=2)

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, keys: Sequence[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://idp.example.com/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_OPERATOR = Operator(
    sub="op-alice",
    raw_jwt="<test-raw-jwt>",
    tenant_id=_TENANT,
    tenant_role=TenantRole.OPERATOR,
    principal_kind=PrincipalKind.USER,
)


def _archive(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(_PATH)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _tail(data: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        handle = tar.extractfile(_PATH)
        assert handle is not None
        return handle.read()


async def _upload(store: _Store, content: bytes, state: str = "open") -> SessionUploadResult:
    meta = UploadMeta(
        session_id=_SID,
        project="infra",
        app="claude-code",
        app_version=None,
        started_at=None,
        ended_at=None,
        state=state,
        outcome=None,
    )
    return await upload_session(
        _OPERATOR,
        meta,
        _archive(content),
        store=store,  # type: ignore[arg-type]
        config=load_config(),
        path="/api/v1/sessions",
    )


async def _upload_rows() -> list[AgentSessionUpload]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(AgentSessionUpload).order_by(AgentSessionUpload.seq))
        return list(rows)


@pytest.mark.asyncio
async def test_an_unfinished_last_line_waits_until_the_session_ends() -> None:
    store = _Store()
    partial = b'{"type":"assistant","mess'
    first = await _upload(store, _LINE_1 + partial)
    assert first.new_bytes == len(_LINE_1)
    # Nothing new but the unfinished line: nothing is stored.
    assert (await _upload(store, _LINE_1 + partial)).outcome == "unchanged"
    ended = await _upload(store, _LINE_1 + partial, state="ended")
    assert (ended.outcome, ended.state) == ("appended", "ended")
    tails = [_tail(store.objects[row.object_key]) for row in await _upload_rows()]
    assert tails == [_LINE_1, partial]
    # A late retry of the same bytes changes nothing.
    assert (await _upload(store, _LINE_1 + partial)).outcome == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "cut", "value"),
    [
        pytest.param(
            b'{"type":"tool_result","content":"export password=MadeUpValue4242 done"}\n',
            b"password=Made",
            b"MadeUpValue4242",
            id="password-pair",
        ),
        pytest.param(
            b'{"type":"tool","content":"curl -H \\"Authorization: Bearer madeUpToken123456\\""}\n',
            b"Bearer madeU",
            b"madeUpToken123456",
            id="bearer-token",
        ),
    ],
)
async def test_a_line_sent_in_two_uploads_is_checked_whole(
    line: bytes, cut: bytes, value: bytes
) -> None:
    store = _Store()
    split = line.index(cut) + len(cut)
    await _upload(store, _LINE_1 + line[:split])
    second = await _upload(store, _LINE_1 + line)
    assert second.redactions == 1
    stored = b"".join(_tail(store.objects[row.object_key]) for row in await _upload_rows())
    assert stored.startswith(_LINE_1)
    for part in (value[:4], value[4:]):
        assert part not in stored


@pytest.mark.asyncio
async def test_a_private_key_sent_in_two_uploads_is_never_stored() -> None:
    store = _Store()
    # The markers are split so the secret scanners of the repo do not flag them.
    key = (
        (b"-----BEGIN " + b"RSA PRIVATE KEY-----\n")
        + b"MIIEMadeUpKeyMaterialLineOneNotARealKey000\n"
        + b"MadeUpKeyMaterialLineTwoNotARealKey111\n"
        + (b"-----END " + b"RSA PRIVATE KEY-----\n")
    )
    cut = key.index(b"MadeUpKeyMaterialLineTwo")
    first = await _upload(store, _LINE_1 + b"cat key.pem\n" + key[:cut])
    second = await _upload(store, _LINE_1 + b"cat key.pem\n" + key + b"done\n")
    assert first.quarantined and second.quarantined
    stored = b"".join(_tail(store.objects[row.object_key]) for row in await _upload_rows())
    assert b"MadeUpKeyMaterial" not in stored
    assert stored.endswith(b"[REDACTED:pem_private_key]\ndone\n")


async def _make_old() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentSessionRecord).values(last_seen_at=datetime.now(UTC) - timedelta(days=400))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_cleanup_keeps_a_session_that_got_an_upload_after_the_search() -> None:
    store = _Store()
    await _upload(store, _LINE_1)
    await _make_old()
    found = await _expired(datetime.now(UTC))
    assert len(found) == 1
    line_2 = b'{"type":"user","message":{"content":"one more"}}\n'
    await _upload(store, _LINE_1 + line_2)
    tenant_id, record_id, cutoff = found[0]
    assert not await _remove(store, tenant_id, record_id, cutoff)
    rows = await _upload_rows()
    assert len(rows) == 2
    assert sorted(store.objects) == sorted(row.object_key for row in rows)


@pytest.fixture
def statements() -> Iterator[list[str]]:
    """The SQL of every ORM statement, as PostgreSQL would run it."""
    seen: list[str] = []

    def record(state: ORMExecuteState) -> None:
        statement = state.statement
        assert isinstance(statement, ClauseElement)
        seen.append(str(statement.compile(dialect=PGDialect())))

    event.listen(Session, "do_orm_execute", record)
    yield seen
    event.remove(Session, "do_orm_execute", record)


def _locks_before_listing_files(seen: list[str]) -> bool:
    locked = next(
        n for n, sql in enumerate(seen) if "agent_session_record" in sql and "FOR UPDATE" in sql
    )
    listed = next(n for n, sql in enumerate(seen) if "agent_session_upload.object_key" in sql)
    return locked < listed


@pytest.mark.asyncio
async def test_delete_locks_the_session_before_it_lists_the_files(
    statements: list[str],
) -> None:
    store = _Store()
    await _upload(store, _LINE_1)
    statements.clear()
    await delete_session(_OPERATOR, _SID, store=store, path="/api/v1/sessions")
    assert _locks_before_listing_files(statements)
    assert store.objects == {}


@pytest.mark.asyncio
async def test_cleanup_locks_the_session_before_it_lists_the_files(
    statements: list[str],
) -> None:
    store = _Store()
    await _upload(store, _LINE_1)
    await _make_old()
    (tenant_id, record_id, cutoff), *_ = await _expired(datetime.now(UTC))
    statements.clear()
    assert await _remove(store, tenant_id, record_id, cutoff)
    assert _locks_before_listing_files(statements)
    assert store.objects == {}


@pytest.mark.asyncio
async def test_two_uploads_at_the_same_time_keep_the_stored_archive() -> None:
    store = _Store()
    await _upload(store, _LINE_1)
    store.gate = asyncio.Event()
    line_2 = b'{"type":"assistant","message":{"content":"three pods run"}}\n'
    line_3 = b'{"type":"user","message":{"content":"thanks"}}\n'
    results = await asyncio.gather(
        _upload(store, _LINE_1 + line_2),
        _upload(store, _LINE_1 + line_2 + line_3),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, SessionConflictError)
            assert result.reason == "session_upload_in_progress"
    # Every upload row points to an archive that is there, and no archive
    # is left without a row.
    rows = await _upload_rows()
    assert sorted(store.objects) == sorted(row.object_key for row in rows)
    assert len({row.object_key for row in rows}) == len(rows)
