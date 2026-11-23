# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.events_ingest` (#2881).

Acceptance-criteria coverage:

* oversize body -> 413 before the body is buffered (streaming-guard unit +
  Content-Length endpoint path)
* missing / paused source -> the same 401 as a bad signature
* bad signature -> 401; a non-ASCII signature or a ``nan`` timestamp too
* failed sender checks have their own limit: they do not use up the
  source's delivery limit, and past their limit they make no Vault read
* an empty or blank stored secret refuses every sender
* accepted delivery -> 202 ``{event_id}`` with one ``event_outbox`` row
  (``origin`` = source id, ``dedupe_key`` set) and one synchronous
  ``__ingest__`` audit row (criterion 2)
* duplicate delivery -> 200 with no second outbox/audit row (criterion 1);
  the dedupe key comes from the signed data, never from a request header
* rate-limit -> 429 with ``Retry-After``
* authenticated invalid JSON -> 400
* deeply-nested within-cap body -> parser fails closed (400, never a 500)
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from meho_backplane.api.v1.events_ingest import _read_capped_body, router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError, _resolve_login_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EventOutbox, Tenant
from meho_backplane.events.ingest.rate_limit import IngestRateLimitError
from meho_backplane.events.ingest.secret_cache import (
    _entries,
    bump_secret_version,
    clear_secret_cache,
)
from meho_backplane.events.ingest.service import IngestPayloadError, _parse_json_or_fail
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._event_source_helpers import (
    _insert_event_source,
    _settings_env,  # noqa: F401  (autouse fixture)
)
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID

_SECRET_VALUE = "shared-hmac-key"
_SECRET_REF = f"tenants/{DEFAULT_TENANT_ID}/event-sources/prod-am"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


class _FakePipeline:
    """Runs its queued commands in one step, like a ``MULTI`` transaction."""

    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[tuple[str, int]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str) -> None:
        self._ops.append((name, 1))

    def decr(self, name: str) -> None:
        self._ops.append((name, -1))

    def expire(self, name: str, time: int) -> None:
        return None

    async def execute(self) -> list[object]:
        results: list[object] = []
        for key, step in self._ops:
            self._store[key] = self._store.get(key, 0) + step
            results.append(self._store[key])
        return [results[0], True]


class _FakeValkey:
    """In-memory stand-in for the counters the ingest rate limits keep."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self.store)

    async def get(self, name: str) -> bytes | None:
        value = self.store.get(name)
        return None if value is None else str(value).encode()

    async def incr(self, name: str) -> int:
        self.store[name] = self.store.get(name, 0) + 1
        return self.store[name]


@pytest.fixture(autouse=True)
def _stub_service_seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub the Vault read and the Valkey counters for the endpoint.

    The Vault read returns the known secret (no socket); the rate limits run
    for real against an in-memory fake Valkey, in one fixed window. Individual tests re-patch a
    seam to drive its failure path.
    """

    async def _read_secret(_operator: object, _ref: str) -> SecretStr:
        return SecretStr(_SECRET_VALUE)

    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    monkeypatch.setattr(
        "meho_backplane.events.ingest.rate_limit.get_broadcast_client", lambda: fake
    )
    monkeypatch.setattr(
        "meho_backplane.events.ingest.secret_cache.get_broadcast_client", lambda: fake
    )
    # One fixed rate-limit window, so a test gives the same result when the
    # wall clock passes a full minute while it runs.
    monkeypatch.setattr("meho_backplane.events.ingest.rate_limit.current_window", lambda: 1_000)
    clear_secret_cache()
    yield
    clear_secret_cache()


async def _seed_tenant() -> None:
    """Insert the default tenant so ``event_outbox.tenant_id`` FK is satisfied."""
    sm = get_sessionmaker()
    async with sm() as session:
        tid = uuid.UUID(DEFAULT_TENANT_ID)
        if await session.get(Tenant, tid) is None:
            session.add(Tenant(id=tid, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _seed_source(**kwargs: object) -> None:
    await _seed_tenant()
    defaults: dict[str, object] = {
        "slug": "prod-am",
        "name": "prod-am",
        "kind": "alertmanager",
        "auth_strategy": "hmac-sha256",
        "secret_ref": _SECRET_REF,
        "status": "active",
    }
    defaults.update(kwargs)
    await _insert_event_source(**defaults)


def _signed(body: bytes, *, ts: float | None = None, secret: str = _SECRET_VALUE) -> dict[str, str]:
    ts = time.time() if ts is None else ts
    ts_str = f"{ts}"
    sig = hmac.new(secret.encode(), ts_str.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Meho-Signature": sig, "X-Meho-Timestamp": ts_str}


async def _count(model: object) -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# --------------------------------------------------------------------------
# resolution: a missing or paused source answers like a bad credential
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_and_paused_source_answer_like_a_bad_signature(
    client: TestClient,
) -> None:
    """Unknown slug, paused source and wrong signature: one status, one body."""
    await _seed_source()
    await _seed_source(slug="paused-am", name="paused-am", status="paused")
    body = b'{"x":1}'
    bad = _signed(body)
    bad["X-Meho-Signature"] = "0" * 64

    wrong_sig = client.post("/api/v1/events/ingest/prod-am", content=body, headers=bad)
    missing = client.post("/api/v1/events/ingest/nope", content=body, headers=_signed(body))
    paused = client.post("/api/v1/events/ingest/paused-am", content=body, headers=_signed(body))

    assert wrong_sig.status_code == 401
    assert missing.status_code == wrong_sig.status_code
    assert paused.status_code == wrong_sig.status_code
    assert missing.json() == wrong_sig.json()
    assert paused.json() == wrong_sig.json()


def test_missing_source_reads_the_body_under_the_default_cap(client: TestClient) -> None:
    """An oversize body gets 413 whether or not the slug exists."""
    huge = b"x" * (256 * 1024 + 1)
    resp = client.post("/api/v1/events/ingest/nope", content=huge, headers=_signed(huge))
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "payload_too_large"


def test_missing_source_makes_no_secret_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []

    async def _read_secret(_operator: object, ref: str) -> SecretStr:
        reads.append(ref)
        return SecretStr(_SECRET_VALUE)

    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    body = b"{}"
    resp = client.post("/api/v1/events/ingest/nope", content=body, headers=_signed(body))
    assert resp.status_code == 401
    assert reads == []


# --------------------------------------------------------------------------
# body cap / 413
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversize_body_returns_413(client: TestClient) -> None:
    await _seed_source()
    huge = b"x" * (256 * 1024 + 1)
    resp = client.post("/api/v1/events/ingest/prod-am", content=huge, headers=_signed(huge))
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_capped_reader_streams_before_buffering() -> None:
    """The streaming running-total guard rejects even without Content-Length."""

    async def _stream():
        # No content-length header; three chunks that together exceed the cap.
        for _ in range(3):
            yield b"a" * 100

    request = SimpleNamespace(headers={}, stream=_stream)
    with pytest.raises(Exception) as exc:  # _IngestBodyTooLargeError
        await _read_capped_body(request, max_bytes=150)  # type: ignore[arg-type]
    assert type(exc.value).__name__ == "_IngestBodyTooLargeError"


@pytest.mark.asyncio
async def test_capped_reader_returns_wire_bytes_under_content_encoding() -> None:
    """The reader returns the body as sent, even with a ``Content-Encoding`` header.

    The signature is checked over these bytes, and nothing decodes them a
    second time. The cap counts the compressed bytes that came over the wire.
    """
    wire = gzip.compress(b'{"alert":"firing"}' * 50)

    async def _stream():
        yield wire[:10]
        yield wire[10:]

    request = SimpleNamespace(headers={"content-encoding": "gzip"}, stream=_stream)
    assert await _read_capped_body(request, max_bytes=len(wire)) == wire  # type: ignore[arg-type]
    with pytest.raises(Exception) as exc:  # _IngestBodyTooLargeError
        await _read_capped_body(request, max_bytes=len(wire) - 1)  # type: ignore[arg-type]
    assert type(exc.value).__name__ == "_IngestBodyTooLargeError"


# --------------------------------------------------------------------------
# auth / 401
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_signature_returns_401(client: TestClient) -> None:
    await _seed_source()
    body = b'{"alert":"firing"}'
    headers = _signed(body)
    headers["X-Meho-Signature"] = "0" * 64
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_non_ascii_signature_header_returns_401(client: TestClient) -> None:
    """A signature header with a byte outside ASCII is refused like any wrong one."""
    await _seed_source()
    body = b'{"alert":"firing"}'
    ts = _signed(body)["X-Meho-Timestamp"]
    headers = {b"X-Meho-Signature": b"\xe9" * 64, b"X-Meho-Timestamp": ts.encode()}
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_nan_timestamp_returns_401(client: TestClient) -> None:
    await _seed_source()
    body = b'{"alert":"firing"}'
    headers = _signed(body)
    headers["X-Meho-Timestamp"] = "nan"
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert resp.status_code == 401


def _count_secret_reads(monkeypatch: pytest.MonkeyPatch, value: str = _SECRET_VALUE) -> list[str]:
    reads: list[str] = []

    async def _read_secret(_operator: object, ref: str) -> SecretStr:
        reads.append(ref)
        return SecretStr(value)

    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    return reads


def _bad_signature(body: bytes) -> dict[str, str]:
    headers = _signed(body)
    headers["X-Meho-Signature"] = "0" * 64
    return headers


@pytest.mark.asyncio
async def test_bad_deliveries_do_not_use_up_the_source_limit(client: TestClient) -> None:
    """With a limit of 2, two bad deliveries do not block the real sender."""
    await _seed_source(extras={"rate_per_minute": 2})
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"

    bad = [client.post(url, content=body, headers=_bad_signature(body)) for _ in range(2)]
    good = client.post(url, content=body, headers=_signed(body))

    assert [r.status_code for r in bad] == [401, 401]
    assert good.status_code == 202


@pytest.mark.asyncio
async def test_bad_deliveries_over_their_limit_get_429_and_no_vault_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the failed-check limit, bad deliveries get 429 and read no secret;
    the real sender is still checked and accepted."""
    await _seed_source(extras={"rate_per_minute": 2})
    reads = _count_secret_reads(monkeypatch)
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"

    codes = [
        client.post(url, content=body, headers=_bad_signature(body)).status_code for _ in range(5)
    ]
    assert codes == [401, 401, 429, 429, 429]
    assert len(reads) == 1

    over = client.post(url, content=body, headers=_bad_signature(body))
    assert over.headers["Retry-After"].isdigit()
    good = client.post(url, content=body, headers=_signed(body))
    assert good.status_code == 202
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_good_deliveries_over_the_limit_get_429_and_read_the_secret_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Good deliveries past the delivery limit get 429 and read no more secrets."""
    await _seed_source(extras={"rate_per_minute": 2})
    reads = _count_secret_reads(monkeypatch)
    url = "/api/v1/events/ingest/prod-am"
    codes = []
    for i in range(10):
        body = json.dumps({"n": i}).encode()
        codes.append(client.post(url, content=body, headers=_signed(body)).status_code)
    assert codes == [202, 202] + [429] * 8
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_a_changed_secret_works_at_once_while_the_failed_check_limit_is_reached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a secret change, the new secret is accepted and the old one is
    refused, also when the source is over its failed-check limit."""
    await _seed_source(extras={"rate_per_minute": 2})
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"
    for _ in range(2):
        assert client.post(url, content=body, headers=_bad_signature(body)).status_code == 401
    reads = _count_secret_reads(monkeypatch, value="the-new-secret")

    (secret_ref,) = {ref for _source_id, ref in _entries}
    await bump_secret_version(secret_ref)

    new = client.post(url, content=body, headers=_signed(body, secret="the-new-secret"))
    old_body = b'{"alert":"resolved"}'
    old = client.post(url, content=old_body, headers=_signed(old_body))
    assert new.status_code == 202
    assert old.status_code == 429
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_parallel_bad_deliveries_read_the_secret_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad deliveries that arrive at the same time read the secret once; only
    ``limit`` of them get 401, the rest get 429."""
    await _seed_source(extras={"rate_per_minute": 2})
    reads: list[str] = []

    async def _slow_read(_operator: object, ref: str) -> SecretStr:
        reads.append(ref)
        await asyncio.sleep(0.05)
        return SecretStr(_SECRET_VALUE)

    monkeypatch.setattr("meho_backplane.events.ingest.service.read_event_source_secret", _slow_read)
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build_app()), base_url="http://testserver"
    ) as ac:
        responses = await asyncio.gather(
            *(ac.post(url, content=body, headers=_bad_signature(body)) for _ in range(20))
        )
        codes = [r.status_code for r in responses]
        assert len(reads) == 1
        assert codes.count(401) == 2
        assert codes.count(429) == 18

        good = await ac.post(url, content=body, headers=_signed(body))
    assert good.status_code == 202
    assert len(reads) == 1


def _failing_secret_reads(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0) -> list[str]:
    reads: list[str] = []

    async def _read_secret(_operator: object, ref: str) -> SecretStr:
        reads.append(ref)
        await asyncio.sleep(delay)
        raise VaultClientError("vault down")

    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    return reads


@pytest.mark.asyncio
async def test_failed_secret_read_is_not_repeated_in_the_same_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Vault fails, ten deliveries in one window read Vault once; all get 503."""
    await _seed_source(extras={"rate_per_minute": 2})
    reads = _failing_secret_reads(monkeypatch)
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"

    codes = [client.post(url, content=body, headers=_signed(body)).status_code for _ in range(10)]
    assert codes == [503] * 10
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_parallel_deliveries_during_a_vault_failure_read_vault_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty deliveries at the same time during a Vault failure read Vault once."""
    await _seed_source(extras={"rate_per_minute": 2})
    reads = _failing_secret_reads(monkeypatch, delay=0.05)
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build_app()), base_url="http://testserver"
    ) as ac:
        responses = await asyncio.gather(
            *(ac.post(url, content=body, headers=_bad_signature(body)) for _ in range(20))
        )
    assert [r.status_code for r in responses] == [503] * 20
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_after_a_failed_read_a_new_window_or_secret_change_reads_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept read error ends with the window and with a secret change."""
    await _seed_source(extras={"rate_per_minute": 2})
    _failing_secret_reads(monkeypatch)
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"
    assert client.post(url, content=body, headers=_signed(body)).status_code == 503

    reads = _count_secret_reads(monkeypatch)
    assert client.post(url, content=body, headers=_signed(body)).status_code == 503
    for key, entry in list(_entries.items()):
        _entries[key] = replace(entry, window=entry.window - 1)
    assert client.post(url, content=body, headers=_signed(body)).status_code == 202
    assert len(reads) == 1

    _failing_secret_reads(monkeypatch)
    (secret_ref,) = {ref for _source_id, ref in _entries}
    await bump_secret_version(secret_ref)
    other = b'{"alert":"resolved"}'
    assert client.post(url, content=other, headers=_signed(other)).status_code == 503
    reads = _count_secret_reads(monkeypatch, value="the-new-secret")
    await bump_secret_version(secret_ref)
    new = client.post(url, content=other, headers=_signed(other, secret="the-new-secret"))
    assert new.status_code == 202
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_kept_secret_from_an_older_window_is_read_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret kept from an earlier window is not used: the next delivery
    reads the secret again."""
    await _seed_source(extras={"rate_per_minute": 2})
    body = b'{"alert":"firing"}'
    url = "/api/v1/events/ingest/prod-am"
    assert client.post(url, content=body, headers=_bad_signature(body)).status_code == 401
    for key, entry in list(_entries.items()):
        _entries[key] = replace(entry, window=entry.window - 1)
    reads = _count_secret_reads(monkeypatch)

    resp = client.post(url, content=body, headers=_signed(body))
    assert resp.status_code == 202
    assert len(reads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["", "   "])
@pytest.mark.parametrize("strategy", ["hmac-sha256", "basic"])
async def test_blank_stored_secret_refuses_every_sender(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stored: str, strategy: str
) -> None:
    """A source whose stored secret is empty or blank accepts no sender, also one
    that signs or logs in with that same empty value."""
    await _seed_source(auth_strategy=strategy, extras={"basic_username": "am"})
    _count_secret_reads(monkeypatch, value=stored)
    body = b'{"alert":"firing"}'
    if strategy == "basic":
        token = base64.b64encode(f"am:{stored}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
    else:
        headers = _signed(body, secret=stored)

    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert resp.status_code == 401
    assert await _count(EventOutbox) == 0


@pytest.mark.asyncio
async def test_basic_source_with_a_real_secret_accepts_the_sender(client: TestClient) -> None:
    await _seed_source(auth_strategy="basic", extras={"basic_username": "am"})
    body = b'{"alert":"firing"}'
    token = base64.b64encode(f"am:{_SECRET_VALUE}".encode()).decode()
    resp = client.post(
        "/api/v1/events/ingest/prod-am",
        content=body,
        headers={"Authorization": f"Basic {token}"},
    )
    assert resp.status_code == 202
    assert await _count(EventOutbox) == 1


@pytest.mark.asyncio
async def test_source_without_secret_fails_closed_401(client: TestClient) -> None:
    await _seed_source(slug="no-secret", name="no-secret", secret_ref=None)
    body = b"{}"
    resp = client.post("/api/v1/events/ingest/no-secret", content=body, headers=_signed(body))
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# accept / 202 + audit + outbox
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_event_publishes_outbox_and_audit(client: TestClient) -> None:
    await _seed_source()
    body = json.dumps({"type": "alert", "severity": "critical"}).encode()
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["deduplicated"] is False
    assert isinstance(payload["event_id"], int)

    sm = get_sessionmaker()
    async with sm() as session:
        outbox = (await session.execute(select(EventOutbox))).scalars().all()
        assert len(outbox) == 1
        row = outbox[0]
        assert row.origin is not None  # source id
        assert row.dedupe_key is not None
        # This synthetic body carries no Alertmanager ``status``, so the
        # alertmanager normaliser (#2882) derives the default ``event`` type.
        assert row.event_kind == "external.alertmanager.event"
        assert row.payload["source"]["slug"] == "prod-am"
        assert row.payload["raw"] == {"type": "alert", "severity": "critical"}

        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.method == "INGEST")))
            .scalars()
            .all()
        )
        assert len(audits) == 1
        audit = audits[0]
        assert audit.operator_sub == "__ingest__"
        assert audit.path == "events.ingest"
        assert audit.status_code == 202
        assert audit.payload["origin"] == row.origin


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent_200(client: TestClient) -> None:
    await _seed_source()
    body = json.dumps({"type": "alert"}).encode()

    headers = _signed(body)

    first = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert first.status_code == 202
    # A redelivery repeats the same signed request -> same dedupe_key -> an
    # idempotent ack.
    second = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["event_id"] == first.json()["event_id"]

    # No second outbox row, no second audit row.
    assert await _count(EventOutbox) == 1
    async with get_sessionmaker()() as session:
        n_audit = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.method == "INGEST")
            )
        ).scalar_one()
    assert n_audit == 1


@pytest.mark.asyncio
async def test_same_signed_delivery_with_another_delivery_id_is_a_duplicate(
    client: TestClient,
) -> None:
    """The dedupe key comes from the signed data; a request header cannot vary it."""
    await _seed_source()
    body = json.dumps({"type": "alert"}).encode()
    signed = _signed(body)

    first = client.post(
        "/api/v1/events/ingest/prod-am",
        content=body,
        headers={**signed, "X-Meho-Delivery-Id": "one"},
    )
    second = client.post(
        "/api/v1/events/ingest/prod-am",
        content=body,
        headers={**signed, "X-Meho-Delivery-Id": "two"},
    )
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["event_id"] == first.json()["event_id"]
    assert await _count(EventOutbox) == 1


@pytest.mark.asyncio
async def test_same_body_signed_at_another_time_is_a_new_event(client: TestClient) -> None:
    """A sender that re-fires an identical body later makes a second event."""
    await _seed_source()
    body = json.dumps({"type": "alert"}).encode()
    now = time.time()

    first = client.post(
        "/api/v1/events/ingest/prod-am", content=body, headers=_signed(body, ts=now)
    )
    second = client.post(
        "/api/v1/events/ingest/prod-am", content=body, headers=_signed(body, ts=now + 1)
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["event_id"] != first.json()["event_id"]
    assert await _count(EventOutbox) == 2


@pytest.mark.asyncio
async def test_static_header_source_dedupes_on_the_body(client: TestClient) -> None:
    """Without a signed timestamp the body is the whole dedupe basis."""
    await _seed_source(slug="harbor", name="harbor", kind="harbor", auth_strategy="static-header")
    body = json.dumps({"type": "PUSH_ARTIFACT"}).encode()
    headers = {"Authorization": _SECRET_VALUE}

    first = client.post("/api/v1/events/ingest/harbor", content=body, headers=headers)
    second = client.post("/api/v1/events/ingest/harbor", content=body, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert await _count(EventOutbox) == 1


# --------------------------------------------------------------------------
# rate-limit / 429 mapping, invalid payload / 400
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_returns_429_with_retry_after(client: TestClient, monkeypatch) -> None:
    await _seed_source()

    async def _limited(*_a: object, **_k: object) -> None:
        raise IngestRateLimitError(limit=1, window_seconds=60, retry_after_seconds=42)

    monkeypatch.setattr("meho_backplane.events.ingest.service.enforce_ingest_rate_limit", _limited)
    body = b'{"type":"alert"}'
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "42"
    assert resp.json()["detail"]["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_authenticated_invalid_json_returns_400(client: TestClient) -> None:
    await _seed_source()
    body = b"this is not json"
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_payload"
    # Nothing published on a rejected payload.
    assert await _count(EventOutbox) == 0


def test_deeply_nested_body_fails_closed_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body that overflows the JSON parser's recursion guard fails closed (400, never 500).

    ``json.loads`` can raise ``RecursionError`` -- not ``JSONDecodeError`` -- on a
    deeply-nested body. The exact nesting depth that trips the C-stack ceiling is
    environment-dependent (interpreter build, C-stack size, worker thread), so rather
    than rely on a specific depth -- which is flaky across CPython builds -- we force
    ``json.loads`` to raise ``RecursionError`` and assert ``_parse_json_or_fail`` maps
    it to :class:`IngestPayloadError`. The endpoint turns that into its clean 400 -- the
    ``IngestPayloadError`` -> 400 mapping is covered end-to-end by
    :func:`test_authenticated_invalid_json_returns_400`. Before the fix the
    ``RecursionError`` propagated uncaught and the request 500'd.
    """

    def _raise_recursion(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded while decoding JSON")

    # ``service`` calls the stdlib ``json.loads``; patch it to raise deterministically.
    monkeypatch.setattr(json, "loads", _raise_recursion)
    with pytest.raises(IngestPayloadError) as exc:
        _parse_json_or_fail(b"[]")
    # It is specifically the recursion path that was caught and re-raised.
    assert isinstance(exc.value.__cause__, RecursionError)


# --------------------------------------------------------------------------
# Vault login role of the secret read
# --------------------------------------------------------------------------


def _capture_ingest_operator(monkeypatch: pytest.MonkeyPatch) -> list[Operator]:
    """Re-patch the Vault read seam so the test sees the operator it logs in with."""
    seen: list[Operator] = []

    async def _runner_jwt() -> str:
        return "runner-jwt"

    async def _read_secret(operator: Operator, _ref: str) -> SecretStr:
        seen.append(operator)
        return SecretStr(_SECRET_VALUE)

    monkeypatch.setattr("meho_backplane.events.ingest.service.check_runner_jwt", _runner_jwt)
    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    return seen


@pytest.mark.asyncio
async def test_secret_read_logs_in_under_the_check_runner_role(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Event intake presents the check runner's token, so it uses the runner's role.

    The Vault recipe gives the runner client its own audience. After that
    the shared ``VAULT_OIDC_ROLE`` no longer accepts the runner's token, so a
    login under it would refuse every signed event. The read must resolve
    ``VAULT_CHECK_RUNNER_ROLE`` instead, like a Sensor check does.
    """
    monkeypatch.setenv("VAULT_CHECK_RUNNER_ROLE", "meho-check-runner")
    get_settings.cache_clear()
    seen = _capture_ingest_operator(monkeypatch)
    await _seed_source()
    body = b'{"alert":"firing"}'

    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))

    assert resp.status_code == 202
    (operator,) = seen
    assert operator.raw_jwt == "runner-jwt"
    assert operator.check_runner_dispatch is True
    assert str(operator.tenant_id) == DEFAULT_TENANT_ID
    assert _resolve_login_role(operator, get_settings(), None) == "meho-check-runner"


@pytest.mark.asyncio
async def test_secret_read_keeps_the_shared_role_when_no_runner_role_is_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``VAULT_CHECK_RUNNER_ROLE`` the read stays on ``VAULT_OIDC_ROLE``, as before."""
    monkeypatch.delenv("VAULT_CHECK_RUNNER_ROLE", raising=False)
    get_settings.cache_clear()
    seen = _capture_ingest_operator(monkeypatch)
    await _seed_source()
    body = b'{"alert":"firing"}'

    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))

    assert resp.status_code == 202
    (operator,) = seen
    assert _resolve_login_role(operator, get_settings(), None) == get_settings().vault_oidc_role
