# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.event_source` (#2880).

Coverage matrix (acceptance criteria):

* **CRUD + tenant scoping** — create / list / describe / patch / delete,
  tenant-scoped; operator role denied on writes; unauthenticated 401.
* **Secret custody (criterion 2)** — a create/patch ``secret`` is written
  to Vault at the derived ``secret_ref`` and never echoed in the response;
  a Vault failure fails the request closed (502) with the row rolled back.
* **Pause immediacy (criterion 1)** — a PATCH to ``paused`` is visible on
  the ingest resolver's next lookup.
* **No existence oracle (criterion 3)** — a cross-tenant slug returns the
  same uniform 404 as an absent one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.event_source.resolver import resolve_event_source_by_slug

from ._event_source_helpers import (
    _admin_token,
    _build_app,
    _insert_event_source,
    _isolated_jwks_cache,  # noqa: F401  (autouse fixture)
    _operator_token,
    _settings_env,  # noqa: F401  (autouse fixture)
)
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_B = "00000000-0000-0000-0000-0000000000b2"

_MINIMAL = {
    "name": "Prod Alertmanager",
    "slug": "prod-am",
    "kind": "alertmanager",
    "auth_strategy": "hmac-sha256",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture
def store_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Patch the Vault write seam; record (secret_ref, raw_value) per call."""
    calls: list[tuple[str, str]] = []

    async def _rec(operator: object, secret_ref: str, secret: object) -> None:
        calls.append((secret_ref, secret.get_secret_value()))  # type: ignore[attr-defined]

    monkeypatch.setattr("meho_backplane.api.v1.event_source.store_event_source_secret", _rec)
    return calls


def _req(
    client: TestClient,
    method: str,
    path: str,
    token: str,
    key: object,
    json: object | None = None,
) -> object:
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.request(method, path, headers={"Authorization": f"Bearer {token}"}, json=json)


# --------------------------------------------------------------------------- create


def test_create_without_secret_201(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=_MINIMAL)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "prod-am"
    assert body["secret_ref"] is None
    assert body["created_by_sub"] == "admin-1"
    assert "secret" not in body  # read model never exposes the value field


def test_create_with_secret_writes_to_vault(
    client: TestClient, store_recorder: list[tuple[str, str]]
) -> None:
    key = make_rsa_keypair("kid-A")
    payload = {**_MINIMAL, "secret": "hmac-signing-key"}
    resp = _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # secret_ref is the derived per-tenant path; the value is never echoed.
    assert body["secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/event-sources/prod-am"
    assert "hmac-signing-key" not in resp.text
    # The Vault write got the derived ref + the raw value exactly once.
    assert store_recorder == [
        (f"tenants/{DEFAULT_TENANT_ID}/event-sources/prod-am", "hmac-signing-key")
    ]


def test_create_duplicate_name_409(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=_MINIMAL)
    dup = {**_MINIMAL, "slug": "other-slug"}  # same name, different slug
    resp = _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=dup)
    assert resp.status_code == 409


def test_create_duplicate_slug_409(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=_MINIMAL)
    dup = {**_MINIMAL, "name": "Different Name"}  # same slug, different name
    resp = _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=dup)
    assert resp.status_code == 409


def test_create_as_operator_403(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "POST", "/api/v1/event-sources", _operator_token(key), key, json=_MINIMAL)
    assert resp.status_code == 403


def test_create_unauthenticated_401(client: TestClient) -> None:
    resp = client.post("/api/v1/event-sources", json=_MINIMAL)
    assert resp.status_code == 401


def test_create_vault_failure_502_and_no_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Vault write failure fails closed: 502 and the row is rolled back."""

    async def _boom(operator: object, secret_ref: str, secret: object) -> None:
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr("meho_backplane.api.v1.event_source.store_event_source_secret", _boom)
    key = make_rsa_keypair("kid-A")
    payload = {**_MINIMAL, "secret": "should-not-persist"}
    resp = _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=payload)
    assert resp.status_code == 502
    assert "should-not-persist" not in resp.text
    # The source was never persisted.
    listed = _req(client, "GET", "/api/v1/event-sources", _admin_token(key), key)
    assert listed.json()["items"] == []


# --------------------------------------------------------------------------- list


def test_list_empty(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "GET", "/api/v1/event-sources", _operator_token(key), key)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_list_tenant_scoped(client: TestClient) -> None:
    await _insert_event_source(tenant_id=uuid.UUID(DEFAULT_TENANT_ID), name="mine", slug="mine")
    await _insert_event_source(tenant_id=uuid.UUID(_TENANT_B), name="theirs", slug="theirs")
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "GET", "/api/v1/event-sources", _operator_token(key), key)
    slugs = {i["slug"] for i in resp.json()["items"]}
    assert slugs == {"mine"}


@pytest.mark.asyncio
async def test_list_status_filter(client: TestClient) -> None:
    await _insert_event_source(name="a", slug="a", status="active")
    await _insert_event_source(name="p", slug="p", status="paused")
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "GET", "/api/v1/event-sources?status=paused", _operator_token(key), key)
    slugs = {i["slug"] for i in resp.json()["items"]}
    assert slugs == {"p"}


@pytest.mark.asyncio
async def test_list_pagination_cursor(client: TestClient) -> None:
    for n in ("a", "b", "c"):
        await _insert_event_source(name=n, slug=n)
    key = make_rsa_keypair("kid-A")
    first = _req(client, "GET", "/api/v1/event-sources?limit=2", _operator_token(key), key).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] == "b"
    nxt = _req(
        client, "GET", "/api/v1/event-sources?limit=2&cursor=b", _operator_token(key), key
    ).json()
    assert [i["name"] for i in nxt["items"]] == ["c"]
    assert nxt["next_cursor"] is None


# --------------------------------------------------------------------------- describe


@pytest.mark.asyncio
async def test_describe_found(client: TestClient) -> None:
    await _insert_event_source(name="am", slug="am-prod")
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "GET", "/api/v1/event-sources/am-prod", _operator_token(key), key)
    assert resp.status_code == 200
    assert resp.json()["slug"] == "am-prod"


def test_describe_missing_404(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "GET", "/api/v1/event-sources/nope", _operator_token(key), key)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_describe_cross_tenant_is_uniform_404(client: TestClient) -> None:
    """A slug owned by another tenant looks identical to an absent one."""
    await _insert_event_source(tenant_id=uuid.UUID(_TENANT_B), name="am", slug="b-owned")
    key = make_rsa_keypair("kid-A")
    cross = _req(client, "GET", "/api/v1/event-sources/b-owned", _operator_token(key), key)
    absent = _req(client, "GET", "/api/v1/event-sources/absent", _operator_token(key), key)
    assert cross.status_code == absent.status_code == 404
    assert cross.json() == {"detail": {"error": "no_event_source", "slug": "b-owned"}}


# --------------------------------------------------------------------------- patch


@pytest.mark.asyncio
async def test_patch_pause_takes_effect_immediately(client: TestClient) -> None:
    """PATCH status=paused is visible on the ingest resolver's next lookup."""
    await _insert_event_source(name="am", slug="am-prod", status="active")
    key = make_rsa_keypair("kid-A")
    async with get_sessionmaker()() as session:
        before = await resolve_event_source_by_slug(session, "am-prod")
    assert before is not None and before.status == "active"

    resp = _req(
        client,
        "PATCH",
        "/api/v1/event-sources/am-prod",
        _admin_token(key),
        key,
        json={"status": "paused"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    # No cache: the very next ingest-path lookup sees the pause.
    async with get_sessionmaker()() as session:
        after = await resolve_event_source_by_slug(session, "am-prod")
    assert after is not None and after.status == "paused"


@pytest.mark.asyncio
async def test_patch_rotates_secret(
    client: TestClient, store_recorder: list[tuple[str, str]]
) -> None:
    """PATCH ``secret`` rotates the value at the derived ref and homes secret_ref."""
    await _insert_event_source(name="am", slug="am-prod", secret_ref=None)
    key = make_rsa_keypair("kid-A")
    resp = _req(
        client,
        "PATCH",
        "/api/v1/event-sources/am-prod",
        _admin_token(key),
        key,
        json={"secret": "rotated-key"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/event-sources/am-prod"
    assert store_recorder == [(f"tenants/{DEFAULT_TENANT_ID}/event-sources/am-prod", "rotated-key")]
    assert "rotated-key" not in resp.text


@pytest.mark.asyncio
async def test_patch_as_operator_403(client: TestClient) -> None:
    await _insert_event_source(name="am", slug="am-prod")
    key = make_rsa_keypair("kid-A")
    resp = _req(
        client,
        "PATCH",
        "/api/v1/event-sources/am-prod",
        _operator_token(key),
        key,
        json={"status": "paused"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- delete


@pytest.mark.asyncio
async def test_delete_soft_and_slug_reusable(client: TestClient) -> None:
    await _insert_event_source(name="am", slug="am-prod")
    key = make_rsa_keypair("kid-A")
    deleted = _req(client, "DELETE", "/api/v1/event-sources/am-prod", _admin_token(key), key)
    assert deleted.status_code == 204
    # Gone from reads.
    gone = _req(client, "GET", "/api/v1/event-sources/am-prod", _operator_token(key), key)
    assert gone.status_code == 404
    # The slug + name free up for a fresh registration.
    recreate = _req(client, "POST", "/api/v1/event-sources", _admin_token(key), key, json=_MINIMAL)
    assert recreate.status_code == 201


@pytest.mark.asyncio
async def test_delete_as_operator_403(client: TestClient) -> None:
    await _insert_event_source(name="am", slug="am-prod")
    key = make_rsa_keypair("kid-A")
    resp = _req(client, "DELETE", "/api/v1/event-sources/am-prod", _operator_token(key), key)
    assert resp.status_code == 403
