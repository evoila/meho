# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the doc-collection readiness probe + lifecycle (G4.6-T6 #1555).

Coverage matrix (Task #1555 acceptance criteria):

* **Success-only write-back** — probing a collection writes ``readiness``
  / ``doc_count`` / ``last_ingested_at`` and transitions ``status``; a
  *failed* probe (backend raises ``CorpusUnavailable``) leaves the row
  unchanged, including its previously-cached liveness. Mirrors
  ``probe_target`` / ``Target.fingerprint``.
* **Status transitions** — the lifecycle state machine: a probe promotes
  ``provisioning`` → ``ready`` once the index is built and demotes
  ``ready`` → ``rebuilding`` when it is not; a forbidden move (a probe
  against a ``disabled`` row) raises ``DocCollectionStateError`` (409).
  Enable / disable are idempotent and guarded.
* **Search-time guard** — ``ensure_collection_searchable`` (the T6
  mechanism T3 #1552 wires into the route): ``ready`` passes,
  ``rebuilding`` / ``provisioning`` → 409 (retryable), ``disabled`` →
  403; never an empty pass-through.
* **Per-project rebuild serialization** — two concurrent probes against
  the same backend endpoint serialize inside the adapter (the lock is
  held across the corpus round-trip); probes against different endpoints
  run concurrently.
* **/ready backend reachability** — the coarse probe reports ``ok`` only
  when every registered backend is configured, naming the unconfigured
  ones.
* **REST routes** — probe / enable / disable are tenant_admin-gated
  (operator → 403), 404 on an unknown key, and the probe maps a backend
  failure to 503 with the row untouched.

Runs against ``sqlite+aiosqlite`` via the shared engine the autouse
``_default_database_url`` conftest fixture pre-migrates to ``alembic
upgrade head`` — identical to :mod:`tests.test_doc_collections_registry`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.corpus import CorpusStatusResponse, CorpusUnavailable
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import DocCollection
from meho_backplane.docs_collections import (
    DocCollectionDisabledError,
    DocCollectionNotReadyError,
    DocCollectionStateError,
    ensure_collection_searchable,
    probe_collection,
    set_collection_enabled,
)
from meho_backplane.docs_collections.lifecycle import (
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    STATUS_READY,
    STATUS_REBUILDING,
    apply_operator_transition,
    apply_probe_transition,
    status_for_readiness,
)
from meho_backplane.docs_search.backends import BackendReadiness, CorpusHttpBackend
from meho_backplane.docs_search.backends.base import SearchBackend
from meho_backplane.docs_search.readiness_probe import docs_backends_readiness_probe
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE as _AUDIENCE,
)
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import (
    ISSUER as _ISSUER,
)

_CORPUS_URL = "https://corpus.test/v1/search"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires + a configured corpus."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("CORPUS_URL", _CORPUS_URL)
    monkeypatch.setenv("CORPUS_AUDIENCE", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _make_operator(tenant_id: str = DEFAULT_TENANT_ID) -> Operator:
    """Build a verified operator without a live JWT round-trip."""
    return Operator(
        sub="admin-1",
        tenant_id=uuid.UUID(tenant_id),
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
        capabilities=frozenset({"meho-docs"}),
    )


async def _insert_collection(**kwargs: Any) -> DocCollection:
    """Insert a DocCollection row via the test sessionmaker."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(DEFAULT_TENANT_ID),
        "collection_key": "vmware",
        "vendor": "VMware",
        "products": ["vsphere"],
        "description": None,
        "when_to_use": None,
        "backend": {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}},
        "status": STATUS_PROVISIONING,
        "last_ingested_at": None,
        "doc_count": None,
        "readiness": None,
        "extras": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    c = DocCollection(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(c)
        await session.commit()
    return c


# ---------------------------------------------------------------------------
# Lifecycle state machine (pure)
# ---------------------------------------------------------------------------


def test_status_for_readiness_maps_index_built() -> None:
    assert status_for_readiness(BackendReadiness(reachable=True, index_built=True)) == STATUS_READY
    assert (
        status_for_readiness(BackendReadiness(reachable=True, index_built=False))
        == STATUS_REBUILDING
    )


@pytest.mark.parametrize(
    ("from_status", "to_status", "expected"),
    [
        (STATUS_PROVISIONING, STATUS_READY, STATUS_READY),
        (STATUS_PROVISIONING, STATUS_REBUILDING, STATUS_REBUILDING),
        (STATUS_READY, STATUS_REBUILDING, STATUS_REBUILDING),
        (STATUS_REBUILDING, STATUS_READY, STATUS_READY),
        (STATUS_READY, STATUS_READY, STATUS_READY),  # idempotent no-op
    ],
)
def test_apply_probe_transition_allows_legal_moves(
    from_status: str, to_status: str, expected: str
) -> None:
    assert (
        apply_probe_transition(
            collection_key="vmware", from_status=from_status, to_status=to_status
        )
        == expected
    )


def test_apply_probe_transition_rejects_waking_a_disabled_collection() -> None:
    """A probe never re-enables a disabled collection — operator intent wins."""
    with pytest.raises(DocCollectionStateError) as exc:
        apply_probe_transition(
            collection_key="vmware", from_status=STATUS_DISABLED, to_status=STATUS_READY
        )
    assert exc.value.status_code == 409
    assert exc.value.from_status == STATUS_DISABLED
    assert exc.value.to_status == STATUS_READY


def test_apply_operator_transition_disable_from_any_live_state() -> None:
    for src in (STATUS_PROVISIONING, STATUS_READY, STATUS_REBUILDING):
        assert (
            apply_operator_transition(
                collection_key="vmware", from_status=src, to_status=STATUS_DISABLED
            )
            == STATUS_DISABLED
        )


def test_apply_operator_transition_enable_only_from_disabled() -> None:
    assert (
        apply_operator_transition(
            collection_key="vmware", from_status=STATUS_DISABLED, to_status=STATUS_PROVISIONING
        )
        == STATUS_PROVISIONING
    )
    # disable is reachable from ready, but "enable" (→ provisioning) from a
    # live state is a forbidden move.
    with pytest.raises(DocCollectionStateError):
        apply_operator_transition(
            collection_key="vmware", from_status=STATUS_READY, to_status=STATUS_PROVISIONING
        )


# ---------------------------------------------------------------------------
# Search-time guard (the T6 mechanism T3 #1552 wires in)
# ---------------------------------------------------------------------------


def test_ensure_searchable_passes_for_ready() -> None:
    ensure_collection_searchable(collection_key="vmware", status=STATUS_READY)  # no raise


@pytest.mark.parametrize("status", [STATUS_REBUILDING, STATUS_PROVISIONING])
def test_ensure_searchable_409_for_not_ready(status: str) -> None:
    with pytest.raises(DocCollectionNotReadyError) as exc:
        ensure_collection_searchable(collection_key="vmware", status=status)
    assert exc.value.status_code == 409
    assert exc.value.detail["retryable"] is True
    assert exc.value.detail["status"] == status


def test_ensure_searchable_403_for_disabled() -> None:
    with pytest.raises(DocCollectionDisabledError) as exc:
        ensure_collection_searchable(collection_key="vmware", status=STATUS_DISABLED)
    assert exc.value.status_code == 403
    assert exc.value.detail["retryable"] is False


def test_ensure_searchable_fails_closed_on_unknown_status() -> None:
    """A status outside the enum is treated as not-ready, never waved through."""
    with pytest.raises(DocCollectionNotReadyError):
        ensure_collection_searchable(collection_key="vmware", status="bogus")


# ---------------------------------------------------------------------------
# probe_collection write-back (service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_writes_liveness_and_promotes_to_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful probe writes liveness + transitions provisioning → ready."""
    collection = await _insert_collection(status=STATUS_PROVISIONING)
    ingested = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    async def _fake_probe(
        self: SearchBackend, operator: Operator, *, backend_ref: Any = None
    ) -> BackendReadiness:
        return BackendReadiness(
            reachable=True,
            index_built=True,
            doc_count=17000,
            last_ingested_at=ingested,
            detail={"probe_method": "corpus-status"},
        )

    monkeypatch.setattr(CorpusHttpBackend, "probe", _fake_probe)

    operator = _make_operator()
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollection, collection.id)
        assert row is not None
        readiness = await probe_collection(session, operator, row)

    assert readiness.doc_count == 17000
    async with sm() as session:
        row = await session.get(DocCollection, collection.id)
        assert row is not None
        assert row.status == STATUS_READY
        assert row.doc_count == 17000
        assert row.last_ingested_at.replace(tzinfo=UTC) == ingested
        assert row.readiness == {"probe_method": "corpus-status"}


@pytest.mark.asyncio
async def test_probe_demotes_ready_to_rebuilding_when_index_not_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = await _insert_collection(status=STATUS_READY, doc_count=10)

    async def _fake_probe(
        self: SearchBackend, operator: Operator, *, backend_ref: Any = None
    ) -> BackendReadiness:
        return BackendReadiness(reachable=True, index_built=False, doc_count=10)

    monkeypatch.setattr(CorpusHttpBackend, "probe", _fake_probe)

    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollection, collection.id)
        assert row is not None
        await probe_collection(session, _make_operator(), row)

    async with sm() as session:
        row = await session.get(DocCollection, collection.id)
        assert row is not None
        assert row.status == STATUS_REBUILDING


@pytest.mark.asyncio
async def test_failed_probe_leaves_row_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Success-only write-back: a raising probe persists nothing."""
    before_ingest = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    collection = await _insert_collection(
        status=STATUS_READY,
        doc_count=42,
        last_ingested_at=before_ingest,
        readiness={"probe_method": "corpus-status"},
    )

    async def _raising_probe(
        self: SearchBackend, operator: Operator, *, backend_ref: Any = None
    ) -> BackendReadiness:
        raise CorpusUnavailable("corpus unreachable: ConnectError")

    monkeypatch.setattr(CorpusHttpBackend, "probe", _raising_probe)

    sm = get_sessionmaker()
    # The route's transaction (session.begin()) is what rolls back; emulate
    # it here so the assertion proves the row survives a failed probe.
    with pytest.raises(CorpusUnavailable):
        async with sm() as session, session.begin():
            row = await session.get(DocCollection, collection.id)
            assert row is not None
            await probe_collection(session, _make_operator(), row)

    async with sm() as session:
        row = await session.get(DocCollection, collection.id)
        assert row is not None
        assert row.status == STATUS_READY
        assert row.doc_count == 42
        assert row.last_ingested_at.replace(tzinfo=UTC) == before_ingest


@pytest.mark.asyncio
async def test_set_collection_enabled_is_idempotent() -> None:
    collection = await _insert_collection(status=STATUS_READY)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollection, collection.id)
        assert row is not None
        # Disable → change; disable again → no-op.
        assert await set_collection_enabled(session, row, enabled=False) is True
        assert await set_collection_enabled(session, row, enabled=False) is False
        assert row.status == STATUS_DISABLED


# ---------------------------------------------------------------------------
# Per-project rebuild serialization (in-adapter lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_probes_same_project_serialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two probes against the same endpoint do not overlap inside the adapter."""
    backend = CorpusHttpBackend()
    overlap = {"max_in_flight": 0, "in_flight": 0}
    gate = asyncio.Event()

    async def _slow_status(
        operator: Operator, *, corpus_url: str | None = None, audience: str | None = None
    ) -> CorpusStatusResponse:
        overlap["in_flight"] += 1
        overlap["max_in_flight"] = max(overlap["max_in_flight"], overlap["in_flight"])
        # Yield so a second coroutine gets a chance to run before this one
        # releases — if the lock were absent, both would be in-flight here.
        await gate.wait()
        overlap["in_flight"] -= 1
        return CorpusStatusResponse(index_built=True, doc_count=1)

    monkeypatch.setattr(
        "meho_backplane.docs_search.backends.corpus_http.corpus_status", _slow_status
    )

    ref = {"endpoint": _CORPUS_URL}
    operator = _make_operator()
    task_a = asyncio.create_task(backend.probe(operator, backend_ref=ref))
    task_b = asyncio.create_task(backend.probe(operator, backend_ref=ref))
    await asyncio.sleep(0.05)  # let both tasks reach the lock / status call
    gate.set()
    await asyncio.gather(task_a, task_b)

    # Same-project lock means at most one probe is ever inside the corpus
    # round-trip — never two concurrently.
    assert overlap["max_in_flight"] == 1


@pytest.mark.asyncio
async def test_concurrent_probes_different_projects_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probes against distinct endpoints are not serialized against each other."""
    backend = CorpusHttpBackend()
    overlap = {"max_in_flight": 0, "in_flight": 0}
    gate = asyncio.Event()

    async def _slow_status(
        operator: Operator, *, corpus_url: str | None = None, audience: str | None = None
    ) -> CorpusStatusResponse:
        overlap["in_flight"] += 1
        overlap["max_in_flight"] = max(overlap["max_in_flight"], overlap["in_flight"])
        await gate.wait()
        overlap["in_flight"] -= 1
        return CorpusStatusResponse(index_built=True)

    monkeypatch.setattr(
        "meho_backplane.docs_search.backends.corpus_http.corpus_status", _slow_status
    )

    operator = _make_operator()
    task_a = asyncio.create_task(
        backend.probe(operator, backend_ref={"endpoint": "https://a.test/v1/search"})
    )
    task_b = asyncio.create_task(
        backend.probe(operator, backend_ref={"endpoint": "https://b.test/v1/search"})
    )
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(task_a, task_b)

    # Different-project locks → both probes in-flight at once.
    assert overlap["max_in_flight"] == 2


# ---------------------------------------------------------------------------
# /ready backend reachability probe
# ---------------------------------------------------------------------------


def test_ready_probe_ok_when_corpus_configured() -> None:
    result = docs_backends_readiness_probe()
    assert result.name == "docs_backends"
    assert result.ok is True


def test_ready_probe_fails_when_backend_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPUS_URL", "")
    get_settings.cache_clear()
    result = docs_backends_readiness_probe()
    assert result.ok is False
    assert "corpus-http" in (result.detail or "")


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    from meho_backplane.api.v1.doc_collections import router as doc_collections_router

    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(doc_collections_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_engine_for_testing()
    yield TestClient(_build_app())


def _admin_token(key: Any) -> str:
    return mint_token(key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value)


def _operator_token(key: Any) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)


@pytest.mark.asyncio
async def test_probe_route_writes_back_and_returns_readiness(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_collection(collection_key="vmware", status=STATUS_PROVISIONING)

    async def _fake_probe(
        self: SearchBackend, operator: Operator, *, backend_ref: Any = None
    ) -> BackendReadiness:
        return BackendReadiness(reachable=True, index_built=True, doc_count=5)

    monkeypatch.setattr(CorpusHttpBackend, "probe", _fake_probe)

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.post(
            "/api/v1/doc_collections/vmware/probe",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["index_built"] is True
    assert body["doc_count"] == 5

    sm = get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(DocCollection).where(DocCollection.collection_key == "vmware")
            )
        ).scalar_one()
        assert row.status == STATUS_READY


def test_probe_route_requires_tenant_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.post(
            "/api/v1/doc_collections/vmware/probe",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert resp.status_code == 403


def test_probe_route_unknown_key_404(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.post(
            "/api/v1/doc_collections/nope/probe",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_probe_route_backend_unavailable_503_row_untouched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_collection(collection_key="vmware", status=STATUS_PROVISIONING)

    async def _raising_probe(
        self: SearchBackend, operator: Operator, *, backend_ref: Any = None
    ) -> BackendReadiness:
        raise CorpusUnavailable("corpus unreachable: ConnectError")

    monkeypatch.setattr(CorpusHttpBackend, "probe", _raising_probe)

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.post(
            "/api/v1/doc_collections/vmware/probe",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 503

    sm = get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(DocCollection).where(DocCollection.collection_key == "vmware")
            )
        ).scalar_one()
        # Unchanged: a failed probe is rolled back by the route transaction.
        assert row.status == STATUS_PROVISIONING
        assert row.doc_count is None


@pytest.mark.asyncio
async def test_disable_then_enable_route_roundtrip(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware", status=STATUS_READY)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        disable = client.post(
            "/api/v1/doc_collections/vmware/disable",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        enable = client.post(
            "/api/v1/doc_collections/vmware/enable",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert disable.status_code == 204
    assert enable.status_code == 204

    sm = get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(DocCollection).where(DocCollection.collection_key == "vmware")
            )
        ).scalar_one()
        # enable returns a disabled collection to provisioning (a probe
        # then promotes it).
        assert row.status == STATUS_PROVISIONING
