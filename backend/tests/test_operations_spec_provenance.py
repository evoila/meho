# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for persisted spec-ingest provenance (#2291).

Coverage matrix (Task #2291 acceptance criteria):

* ``classify_spec_origin`` maps ``(uri, content)`` to
  ``fetched`` / ``inline`` / ``shipped``.
* ``parse_openapi_with_provenance`` hashes the **raw bytes** (fetched
  body or uploaded content) — a fetched https spec records
  ``origin=fetched`` with ``sha256 == sha256(raw body)``; an inline
  upload records ``origin=inline``; a ``spec:``-labelled inline upload
  (the catalog shipped on-ramp) records ``origin=shipped``.
* ``upsert_spec_provenance`` / ``load_spec_provenance`` — a first ingest
  inserts, a re-ingest of the same ``(triple, uri, scope)`` updates the
  row in place (new sha256, no duplicate), and tenant-scoped + global
  ingests write correctly-scoped rows that coexist (mirrors the
  descriptor scope tests).
* The connector review payload surfaces the provenance rows for its
  scope, and renders an empty list for a connector ingested before the
  table landed.

The fetch path is served through respx with a patched ``getaddrinfo``
so the SSRF destination guard passes without real DNS — the same shape
``test_operations_ingest_openapi`` uses.
"""

from __future__ import annotations

import hashlib
import socket
import uuid
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, SpecProvenance
from meho_backplane.operations.ingest.openapi import (
    classify_spec_origin,
    parse_openapi_with_provenance,
)
from meho_backplane.operations.ingest.service import ReviewService
from meho_backplane.operations.ingest.spec_provenance import (
    load_spec_provenance,
    upsert_spec_provenance,
)
from meho_backplane.settings import get_settings

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
PETSTORE_30 = FIXTURES / "petstore_30.yaml"

_FIXTURE_HOST = "https://specs.example.test"
_PUBLIC_TEST_IP = "93.184.216.34"
_TEST_HOSTS = frozenset({"specs.example.test"})

_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"
_CONNECTOR_ID = "vmware-rest-9.0"
_FAKE_JWT = "header.payload.signature"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _getaddrinfo_for_tests(
    host: str, port: object, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Resolve known test hosts to a public IP so the SSRF guard passes."""
    if host in _TEST_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_PUBLIC_TEST_IP, 443))]
    return socket.getaddrinfo(host, port, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _mock_getaddrinfo_for_test_hosts() -> Generator[None, None, None]:
    """Patch ``getaddrinfo`` for the SSRF guard so test HTTPS URLs resolve public."""
    with patch(
        "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
        side_effect=_getaddrinfo_for_tests,
    ):
        yield


def _make_operator(*, tenant_id: uuid.UUID, sub: str = "user:alice") -> Operator:
    """Build a frozen :class:`Operator` with default test fields."""
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# classify_spec_origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "content", "expected"),
    [
        ("https://vendor.example/openapi.yaml", None, "fetched"),
        ("file:///home/op/internal.yaml", "openapi: 3.0.0", "inline"),
        ("https://vendor.example/openapi.yaml", "openapi: 3.0.0", "inline"),
        ("docs:vcenter/vcenter.yaml", "openapi: 3.0.0", "inline"),
        ("spec:vcenter.yaml", "openapi: 3.0.0", "shipped"),
    ],
)
def test_classify_spec_origin(uri: str, content: str | None, expected: str) -> None:
    """Origin is derived from the fetched-vs-inline bit + the shipped label."""
    assert classify_spec_origin(uri, content) == expected


# ---------------------------------------------------------------------------
# parse_openapi_with_provenance — hash over raw bytes
# ---------------------------------------------------------------------------


def test_parse_with_provenance_inline_hashes_uploaded_content() -> None:
    """An inline upload records origin=inline + sha256 over the raw content bytes."""
    text = PETSTORE_30.read_text(encoding="utf-8")
    protos, provenance = parse_openapi_with_provenance(
        "file:///uploads/petstore.yaml", spec_source="petstore.yaml", content=text
    )
    assert protos, "expected at least one parsed operation"
    assert provenance.uri == "file:///uploads/petstore.yaml"
    assert provenance.origin == "inline"
    assert provenance.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_parse_with_provenance_shipped_label_records_shipped_origin() -> None:
    """A ``spec:``-labelled inline upload (catalog on-ramp) records origin=shipped."""
    text = PETSTORE_30.read_text(encoding="utf-8")
    _protos, provenance = parse_openapi_with_provenance(
        "spec:petstore.yaml", spec_source="spec:petstore.yaml", content=text
    )
    assert provenance.origin == "shipped"
    assert provenance.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_parse_with_provenance_fetched_hashes_response_body() -> None:
    """A fetched https spec records origin=fetched + sha256 over the raw body."""
    body = PETSTORE_30.read_bytes()
    url = f"{_FIXTURE_HOST}/petstore_30.yaml"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200, content=body, headers={"content-type": "application/yaml"}
            )
        )
        _protos, provenance = parse_openapi_with_provenance(
            url, spec_source="spec:petstore_30.yaml"
        )
    assert provenance.uri == url
    assert provenance.origin == "fetched"
    assert provenance.sha256 == hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# upsert_spec_provenance / load_spec_provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_inserts_then_load_returns_global_row() -> None:
    """A first global ingest inserts a row load_spec_provenance reads back."""
    sessionmaker = get_sessionmaker()
    await upsert_spec_provenance(
        sessionmaker,
        tenant_id=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        uri="https://vendor.example/openapi.yaml",
        sha256="a" * 64,
        origin="fetched",
        operator_sub="user:alice",
    )
    async with sessionmaker() as session:
        rows = await load_spec_provenance(
            session,
            tenant_id=None,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.uri == "https://vendor.example/openapi.yaml"
    assert row.sha256 == "a" * 64
    assert row.origin == "fetched"
    assert row.operator_sub == "user:alice"
    assert row.tenant_id is None


@pytest.mark.asyncio
async def test_reingest_same_key_updates_in_place_without_duplicate() -> None:
    """Re-ingesting the same (triple, uri, scope) updates the row, no duplicate."""
    sessionmaker = get_sessionmaker()
    common: dict[str, object] = {
        "tenant_id": None,
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL_ID,
        "uri": "https://vendor.example/openapi.yaml",
        "origin": "fetched",
        "operator_sub": "user:alice",
    }
    await upsert_spec_provenance(sessionmaker, sha256="1" * 64, **common)  # type: ignore[arg-type]
    await upsert_spec_provenance(sessionmaker, sha256="2" * 64, **common)  # type: ignore[arg-type]

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(SpecProvenance).where(
                        SpecProvenance.product == _PRODUCT,
                        SpecProvenance.uri == "https://vendor.example/openapi.yaml",
                        SpecProvenance.tenant_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "re-ingest must update in place, not accumulate duplicates"
    assert rows[0].sha256 == "2" * 64, "the stored digest must track the latest content"


@pytest.mark.asyncio
async def test_tenant_and_global_scopes_write_distinct_rows() -> None:
    """A tenant-scoped ingest and a global ingest of the same uri coexist."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    shared: dict[str, object] = {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL_ID,
        "uri": "https://vendor.example/openapi.yaml",
        "sha256": "b" * 64,
        "origin": "fetched",
        "operator_sub": "user:alice",
    }
    await upsert_spec_provenance(sessionmaker, tenant_id=None, **shared)  # type: ignore[arg-type]
    await upsert_spec_provenance(sessionmaker, tenant_id=tenant_id, **shared)  # type: ignore[arg-type]

    async with sessionmaker() as session:
        global_rows = await load_spec_provenance(
            session, tenant_id=None, product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID
        )
        tenant_rows = await load_spec_provenance(
            session, tenant_id=tenant_id, product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID
        )
    assert len(global_rows) == 1
    assert global_rows[0].tenant_id is None
    assert len(tenant_rows) == 1
    assert tenant_rows[0].tenant_id == tenant_id


# ---------------------------------------------------------------------------
# review payload surfacing
# ---------------------------------------------------------------------------


async def _seed_group_and_op(*, tenant_id: uuid.UUID) -> None:
    """Insert one group + one op so get_review_payload resolves the scope."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="group-0",
                name="Group 0",
                when_to_use="Use group 0.",
                review_status="staged",
            )
        )
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id="GET:/api/v1/group-0/0",
                source_kind="ingested",
                method="GET",
                path="/api/v1/group-0/0",
                group_id=group_id,
                summary="Op 0",
                is_enabled=False,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_review_payload_surfaces_provenance_rows() -> None:
    """get_review_payload returns the scope's provenance, ordered by uri."""
    tenant_id = uuid.uuid4()
    await _seed_group_and_op(tenant_id=tenant_id)
    sessionmaker = get_sessionmaker()
    await upsert_spec_provenance(
        sessionmaker,
        tenant_id=tenant_id,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        uri="https://vendor.example/openapi.yaml",
        sha256="c" * 64,
        origin="fetched",
        operator_sub="user:alice",
    )

    service = ReviewService(_make_operator(tenant_id=tenant_id))
    payload = await service.get_review_payload(_CONNECTOR_ID, tenant_id)

    assert len(payload.provenance) == 1
    prov = payload.provenance[0]
    assert prov.uri == "https://vendor.example/openapi.yaml"
    assert prov.sha256 == "c" * 64
    assert prov.origin == "fetched"
    assert prov.operator_sub == "user:alice"


@pytest.mark.asyncio
async def test_review_payload_empty_provenance_for_pre_provenance_connector() -> None:
    """A connector with no provenance rows renders an empty provenance list."""
    tenant_id = uuid.uuid4()
    await _seed_group_and_op(tenant_id=tenant_id)

    service = ReviewService(_make_operator(tenant_id=tenant_id))
    payload = await service.get_review_payload(_CONNECTOR_ID, tenant_id)

    assert payload.provenance == []
