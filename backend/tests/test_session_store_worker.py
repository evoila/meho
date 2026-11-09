# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CPU work of a session upload runs in a worker process, and uploads are limited.

The object storage is an in-memory fake. Every secret in this file is made up.
"""

from __future__ import annotations

import asyncio
import io
import os
import pickle
import tarfile
import time
from collections.abc import Iterator, Sequence
from concurrent.futures.process import BrokenProcessPool
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.main import app
from meho_backplane.session_store.archive import ArchiveError, read_archive
from meho_backplane.session_store.config import load_config
from meho_backplane.session_store.prepare import prepare_upload
from meho_backplane.session_store.upload import UploadMeta, upload_session
from meho_backplane.session_store.upload_slots import (
    MAX_IN_PROGRESS,
    UploadBusyError,
    upload_slot,
)
from meho_backplane.session_store.worker import run_in_worker, shutdown_worker_pool
from meho_backplane.settings import get_settings

_TENANT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_SID = "7a2b3c4d-aaaa-4bbb-8ccc-ddddeeeeffff"
_PATH = f"{_SID}.jsonl"
_MIB = 1024 * 1024
_LIMIT = 16 * _MIB


class _Store:
    """In-memory object storage."""

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
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://idp.example.com/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(sub: str = "op-alice") -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.READ_ONLY,
        principal_kind=PrincipalKind.USER,
    )


def _archive(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(_PATH)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _exit_now() -> None:
    os._exit(1)


def _meta() -> UploadMeta:
    return UploadMeta(
        session_id=_SID,
        project="infra",
        app="claude-code",
        app_version=None,
        started_at=None,
        ended_at=None,
        state="ended",
        outcome=None,
    )


@pytest.mark.asyncio
async def test_healthz_answers_while_a_maximum_size_upload_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_STORE_MAX_UNPACKED_BYTES", str(_LIMIT))
    # One long line with a secret-like name: the slowest kind of content
    # for the secret check, at the full unpacked limit.
    line = b"password=" + b"a" * (_LIMIT - 10) + b"\n"
    data = _archive(line)
    await run_in_worker(len, b"")  # start the worker processes first
    store = _Store()
    upload = asyncio.create_task(
        upload_session(
            _operator(),
            _meta(),
            data,
            store=store,  # type: ignore[arg-type]
            config=load_config(),
            path="/api/v1/sessions",
        )
    )
    slowest = 0.0
    answers = 0
    pause = 0.02
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        while not upload.done():
            # Time the pause too: a blocked event loop also delays the wake-up.
            started = time.perf_counter()
            await asyncio.sleep(pause)
            response = await client.get("/healthz")
            slowest = max(slowest, time.perf_counter() - started - pause)
            assert response.status_code == 200
            answers += 1
    result = await upload
    assert result.outcome == "created"
    assert result.redactions == 1
    assert len(store.objects) == 1
    assert answers >= 5, "the upload ended before healthz was checked"
    assert slowest < 0.1, f"healthz took {slowest * 1000:.0f} ms during the upload"


@pytest.mark.asyncio
async def test_an_archive_error_from_the_worker_keeps_its_reason() -> None:
    with pytest.raises(ArchiveError) as caught:
        await run_in_worker(read_archive, b"not a gzip tar", max_files=10, max_unpacked_bytes=100)
    assert caught.value.reason == "archive_unreadable"


def test_an_archive_error_pickles_with_its_reason() -> None:
    error = pickle.loads(pickle.dumps(ArchiveError("archive_too_large", "too large")))
    assert isinstance(error, ArchiveError)
    assert (error.reason, str(error)) == ("archive_too_large", "too large")


@pytest.mark.asyncio
async def test_a_dead_worker_fails_one_call_and_the_next_call_works() -> None:
    try:
        with pytest.raises(BrokenProcessPool):
            await run_in_worker(_exit_now)
        assert await run_in_worker(len, b"abc") == 3
    finally:
        shutdown_worker_pool()


def test_prepare_finds_the_tail_checks_it_and_packs_it() -> None:
    first = b'{"type":"user","message":{"content":"list the pods"}}\n'
    secret = b"token: Bearer abcdefghijklmnopqrstuvwxyz0123456789\n"
    prepared = prepare_upload(
        _archive(first + secret),
        max_files=10,
        max_unpacked_bytes=_MIB,
        stored={},
        earlier=[],
        final=True,
    )
    assert prepared.conflicts == []
    assert prepared.archive is not None
    stored = read_archive(prepared.archive.data, max_files=10, max_unpacked_bytes=_MIB)
    assert b"abcdefghijklmnopqrstuvwxyz0123456789" not in stored[_PATH]
    assert prepared.archive.redactions >= 1
    assert prepared.archive.files[_PATH]["added"] == len(first + secret)


def test_prepare_with_no_new_bytes_has_no_archive() -> None:
    content = b'{"type":"user","message":{"content":"list the pods"}}\n'
    first = prepare_upload(
        _archive(content), max_files=10, max_unpacked_bytes=_MIB, stored={}, earlier=[], final=True
    )
    states = {path: state.as_json() for path, state in first.states.items()}
    again = prepare_upload(
        _archive(content),
        max_files=10,
        max_unpacked_bytes=_MIB,
        stored=states,
        earlier=[states],
        final=True,
    )
    assert again.archive is None
    assert again.new_bytes == 0
    changed = prepare_upload(
        _archive(b"x" + content),
        max_files=10,
        max_unpacked_bytes=_MIB,
        stored=states,
        earlier=[states],
        final=True,
    )
    assert changed.conflicts == [_PATH]
    assert changed.archive is None


def test_one_person_has_one_upload_at_a_time() -> None:
    with upload_slot(_operator()):
        with pytest.raises(UploadBusyError) as caught, upload_slot(_operator()):
            pass
        assert caught.value.reason == "session_upload_already_running"
        with upload_slot(_operator("op-bob")):
            pass
    with upload_slot(_operator()):
        pass


def test_the_same_login_name_in_another_tenant_is_another_person() -> None:
    other = Operator(
        sub="op-alice",
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        tenant_role=TenantRole.READ_ONLY,
        principal_kind=PrincipalKind.USER,
    )
    with upload_slot(_operator()), upload_slot(other):
        pass


def test_no_more_than_the_limit_of_uploads_at_a_time() -> None:
    slots = [upload_slot(_operator(f"op-{n}")) for n in range(MAX_IN_PROGRESS)]
    for slot in slots:
        slot.__enter__()
    try:
        with pytest.raises(UploadBusyError) as caught, upload_slot(_operator("op-late")):
            pass
        assert caught.value.reason == "session_uploads_busy"
    finally:
        for slot in slots:
            slot.__exit__(None, None, None)
    with upload_slot(_operator("op-late")):
        pass


def test_a_failed_upload_frees_its_slot() -> None:
    with pytest.raises(RuntimeError), upload_slot(_operator()):
        raise RuntimeError("upload failed")
    with upload_slot(_operator()):
        pass


def test_the_default_unpacked_limit_is_64_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_STORE_MAX_UNPACKED_BYTES", raising=False)
    assert load_config().max_unpacked_bytes == 64 * _MIB
