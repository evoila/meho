# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.read_docs` (#3948).

* An entitled operator reads around a hit of a read-enabled collection: 200
  with the text, the public source link (never a ``gs://`` path) and the
  cursors; the transport gets the handle and the window.
* Every refusal is the **same** 404 body: an unknown collection, a
  collection the caller is not entitled to, a disabled collection, a
  collection without read, and a handle the backend refuses.
* 409 asks for a new search, 429 carries ``Retry-After``, a rebuilding
  collection and an unavailable backend are 503.
* A missing ``collection`` is 422.
* The audit row carries ``op_id = meho.docs.read``, the collection and the
  mode, and never the handle or the cursor.

The transport is patched at the ``corpus-http`` adapter's seam
(``corpus_http.read_corpus``); the DB-backed resolve uses the autouse SQLite
engine, seeded with a global ``vmware`` collection.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.read_docs import router as read_docs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.corpus import CorpusReadError, CorpusUnavailable, UpstreamRead
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DocCollection
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_READ_SEAM = "meho_backplane.docs_search.backends.corpus_http.read_corpus"
_ENTITLED_CAPS = ["meho-docs", "meho-docs:vmware"]
_READ_ON: dict[str, Any] = {"type": "corpus-http", "ref": {"read": "upstream"}}
_HANDLE = "eyJ2IjoxLCJ0IjoiaCJ9.cmVzdC1oYW5kbGU"
_CURSOR = "eyJjIjozfQ.cmVzdC1jdXJzb3I"
_NOT_FOUND_BODY = {"detail": {"error": "not_found", "message": "docs source not found"}}

_READ_REPLY = UpstreamRead.model_validate(
    {
        "mode": "around",
        "text": "Before.\nThe hit.\nAfter.",
        "title": "vSAN disk groups",
        "source_uri": "gs://private-bucket/docs/vsan.html",
        "upstream_url": "https://docs.vendor.test/vsan",
        "disclosure": "full",
        "located": True,
        "truncated": False,
        "next": "next-cursor",
    }
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("CORPUS_URL", "https://corpus.test/search")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _seed(*, status: str = "ready", backend: dict[str, Any] | None = None) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                DocCollection(
                    tenant_id=None,
                    collection_key="vmware",
                    vendor="VMware by Broadcom",
                    products=["vsphere"],
                    description="VMware vendor docs.",
                    when_to_use="VMware product questions.",
                    backend=backend if backend is not None else {"type": "corpus-http"},
                    status=status,
                ),
            )

    asyncio.run(_do())


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(read_docs_router)
    yield TestClient(app)


class _FakeRead:
    def __init__(self, result: UpstreamRead | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, operator: Operator, read_handle: str, **kwargs: Any) -> UpstreamRead:
        self.calls.append({"read_handle": read_handle, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _post(
    client: TestClient,
    body: dict[str, Any],
    fake: _FakeRead,
    *,
    capabilities: list[str] | None = None,
) -> Any:
    key = _make_rsa_keypair("kid-read")
    token = _mint_token(
        key,
        sub="op-reader",
        tenant_role=TenantRole.OPERATOR.value,
        capabilities=_ENTITLED_CAPS if capabilities is None else capabilities,
    )
    with respx.mock as mock_router, patch(_READ_SEAM, new=fake):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.post(
            "/api/v1/read_docs",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


def test_read_docs_returns_text_and_public_source(client: TestClient) -> None:
    _seed(backend=_READ_ON)
    fake = _FakeRead(_READ_REPLY)
    response = _post(
        client,
        {
            "collection": "vmware",
            "read_handle": _HANDLE,
            "mode": "around",
            "before": 3,
            "after": 2,
            "cursor": _CURSOR,
        },
        fake,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "Before.\nThe hit.\nAfter."
    assert body["source_url"] == "https://docs.vendor.test/vsan"
    assert body["disclosure"] == "full"
    assert body["next"] == "next-cursor"
    assert "gs://" not in response.text
    (call,) = fake.calls
    assert call["read_handle"] == _HANDLE
    assert (call["mode"], call["before"], call["after"]) == ("around", 3, 2)
    assert call["cursor"] == _CURSOR
    assert call["read_url"] == "https://corpus.test/read"


@pytest.mark.parametrize(
    ("case", "capabilities"),
    [
        ("unknown_collection", _ENTITLED_CAPS),
        ("not_entitled", ["meho-docs"]),
        ("disabled", _ENTITLED_CAPS),
        ("read_not_enabled", _ENTITLED_CAPS),
        ("upstream_404", _ENTITLED_CAPS),
    ],
)
def test_every_refusal_is_one_identical_404(
    client: TestClient, case: str, capabilities: list[str]
) -> None:
    if case == "disabled":
        _seed(backend=_READ_ON, status="disabled")
    elif case == "read_not_enabled":
        _seed()
    elif case != "unknown_collection":
        _seed(backend=_READ_ON)
    refusal = CorpusReadError("x", kind=CorpusReadError.KIND_NOT_FOUND, status=404)
    fake = _FakeRead(refusal if case == "upstream_404" else _READ_REPLY)
    response = _post(
        client, {"collection": "vmware", "read_handle": _HANDLE}, fake, capabilities=capabilities
    )
    assert response.status_code == 404
    assert response.json() == _NOT_FOUND_BODY
    assert len(fake.calls) == (1 if case == "upstream_404" else 0)


def test_search_again_is_409(client: TestClient) -> None:
    _seed(backend=_READ_ON)
    fake = _FakeRead(CorpusReadError("x", kind=CorpusReadError.KIND_SEARCH_AGAIN, status=409))
    response = _post(client, {"collection": "vmware", "read_handle": _HANDLE}, fake)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "search_again"
    assert _HANDLE not in response.text


@pytest.mark.parametrize(("retry_after", "header"), [(30, "30"), (None, None)])
def test_rate_limit_is_429_with_retry_after(
    client: TestClient, retry_after: int | None, header: str | None
) -> None:
    _seed(backend=_READ_ON)
    fake = _FakeRead(
        CorpusReadError(
            "x", kind=CorpusReadError.KIND_RATE_LIMITED, status=429, retry_after=retry_after
        )
    )
    response = _post(client, {"collection": "vmware", "read_handle": _HANDLE}, fake)
    assert response.status_code == 429
    assert response.headers.get("retry-after") == header
    assert response.json()["detail"]["error"] == "rate_limited"
    assert response.json()["detail"]["retry_after"] == retry_after


def test_not_ready_and_unavailable_are_503(client: TestClient) -> None:
    _seed(backend=_READ_ON, status="rebuilding")
    response = _post(
        client, {"collection": "vmware", "read_handle": _HANDLE}, _FakeRead(_READ_REPLY)
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "collection_not_ready"


def test_unavailable_backend_is_503(client: TestClient) -> None:
    _seed(backend=_READ_ON)
    fake = _FakeRead(CorpusUnavailable("corpus returned HTTP 503", status=503))
    response = _post(client, {"collection": "vmware", "read_handle": _HANDLE}, fake)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "read_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"read_handle": _HANDLE},
        {"collection": "vmware"},
        {"collection": "vmware", "read_handle": _HANDLE, "before": 4},
        {"collection": "vmware", "read_handle": _HANDLE, "mode": "everything"},
        {"collection": "vmware", "read_handle": _HANDLE, "scope": {"product": "x"}},
    ],
)
def test_malformed_body_is_422(client: TestClient, body: dict[str, Any]) -> None:
    response = _post(client, body, _FakeRead(_READ_REPLY))
    assert response.status_code == 422


def test_audit_row_carries_collection_and_mode_never_the_handle(client: TestClient) -> None:
    _seed(backend=_READ_ON)
    response = _post(
        client,
        {"collection": "vmware", "read_handle": _HANDLE, "mode": "page", "cursor": _CURSOR},
        _FakeRead(_READ_REPLY),
    )
    assert response.status_code == 200

    async def _rows() -> list[AuditLog]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.path == "/api/v1/read_docs")
            )
            return list(result.scalars().all())

    (row,) = asyncio.run(_rows())
    assert row.payload["op_id"] == "meho.docs.read"
    assert row.payload["op_class"] == "read"
    assert row.payload["collection"] == "vmware"
    assert row.payload["read_mode"] == "page"
    serialised = json.dumps(row.payload)
    assert _HANDLE not in serialised
    assert _CURSOR not in serialised
