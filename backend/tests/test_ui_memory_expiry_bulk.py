# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Memory UI expiry visualisation + bulk actions.

Initiative #341 (G10.4 Memory UI), Task #879 (G10.4-T3). The acceptance
criteria on issue #879 are:

* memories with ``expires_at`` show a server-rendered countdown badge
  ("expires in 3d 4h"); the HTMX 60s refresh updates it,
* expired-but-not-yet-swept memories appear greyed in a separate
  "Recently expired (cleanup pending)" section (consistent with the
  G5.2 sweeper window #623),
* bulk select -> bulk delete / bulk extend-expiry acts only on writable
  memories (RBAC); non-writable selections rejected server-side,
* CSRF enforced; cross-user/cross-tenant isolation holds.

Suite shape mirrors :mod:`backend.tests.test_ui_memory_list` (T1) so
the fixtures, app-build, and seed helpers stay coherent across the
memory UI test surface.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Document, Tenant
from meho_backplane.memory._internal import (
    MEMORY_SOURCE,
    build_metadata,
    encode_source_id,
)
from meho_backplane.memory.schemas import MemoryScope, kind_for_scope
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.memory.bulk import (
    BULK_MAX_IDS,
    apply_bulk_action,
    format_countdown,
    parse_bulk_ids,
    parse_extend_duration,
    partition_expired,
)
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

# ---------------------------------------------------------------------------
# Fixtures (mirror test_ui_memory_list.py)
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_A = "op-alice"
_OP_B = "op-bob"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars (mirrors :mod:`test_ui_memory_list`)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_memory(
    *,
    tenant_id: uuid.UUID,
    scope: MemoryScope,
    slug: str,
    body: str,
    user_sub: str | None = None,
    target_name: str | None = None,
    tags: list[str] | None = None,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """Persist one memory row directly via the documents table.

    Mirrors :func:`test_ui_memory_list._seed_memory` -- bypasses the
    service so the test doesn't pull in the embedding model wheel.
    """
    metadata = build_metadata(
        caller_metadata={"tags": list(tags)} if tags else None,
        scope=scope,
        user_sub=user_sub or "",
        target_name=target_name,
        expires_at=expires_at,
    )
    source_id = encode_source_id(
        scope=scope,
        user_sub=user_sub or "",
        target_name=target_name,
        slug=slug,
    )
    doc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Document(
                    id=doc_id,
                    tenant_id=tenant_id,
                    source=MEMORY_SOURCE,
                    source_id=source_id,
                    kind=kind_for_scope(scope),
                    body=body,
                    body_hash=f"sha256:test:{doc_id}",
                    embedding=[0.0] * 384,
                    doc_metadata=metadata,
                    tokens=len(body.split()),
                ),
            )

    asyncio.run(_do())
    return doc_id


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-memory-expiry-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
    with_csrf: bool = False,
) -> tuple[TestClient, respx.MockRouter, str]:
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    if with_csrf:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token, "HX-Request": "true"}


def _read_doc(doc_id: uuid.UUID) -> Document | None:
    async def _do() -> Document | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            return await session.get(Document, doc_id)

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# Unit tests -- countdown formatting
# ---------------------------------------------------------------------------


def test_format_countdown_days_and_hours() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    expires_at = now + timedelta(days=3, hours=4, minutes=30)
    assert format_countdown(expires_at, now=now) == "expires in 3d 4h"


def test_format_countdown_hours_and_minutes() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    expires_at = now + timedelta(hours=5, minutes=42)
    assert format_countdown(expires_at, now=now) == "expires in 5h 42m"


def test_format_countdown_minutes_only() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    expires_at = now + timedelta(minutes=18)
    assert format_countdown(expires_at, now=now) == "expires in 18m"


def test_format_countdown_clamps_to_one_minute_on_sub_minute_delta() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    expires_at = now + timedelta(seconds=42)
    assert format_countdown(expires_at, now=now) == "expires in 1m"


def test_format_countdown_returns_expired_on_past_value() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    expires_at = now - timedelta(seconds=1)
    assert format_countdown(expires_at, now=now) == "expired"


# ---------------------------------------------------------------------------
# Unit tests -- partition_expired
# ---------------------------------------------------------------------------


def test_partition_expired_splits_by_now_boundary() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    # Build minimal MemoryEntry-shaped objects via the constructor;
    # exercising the partition function does not require a service.
    from meho_backplane.memory.schemas import MemoryEntry

    def _make(expires_at: datetime | None, slug: str) -> MemoryEntry:
        return MemoryEntry(
            id=uuid.uuid4(),
            tenant_id=_TENANT_A,
            scope=MemoryScope.USER,
            slug=slug,
            body="x",
            metadata={},
            expires_at=expires_at,
            user_sub=_OP_A,
            target_name=None,
            created_at=now,
            updated_at=now,
        )

    persistent = _make(None, "persistent")
    future = _make(now + timedelta(hours=1), "future")
    just_now = _make(now, "just-now")
    past = _make(now - timedelta(hours=1), "past")
    active, recently_expired = partition_expired([persistent, future, just_now, past], now=now)
    active_slugs = {e.slug for e in active}
    expired_slugs = {e.slug for e in recently_expired}
    assert active_slugs == {"persistent", "future"}
    assert expired_slugs == {"just-now", "past"}


# ---------------------------------------------------------------------------
# Unit tests -- bulk-form parsers
# ---------------------------------------------------------------------------


def test_parse_bulk_ids_rejects_empty_selection() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_bulk_ids([])
    assert exc.value.status_code == 422
    assert "bulk_no_ids_selected" in str(exc.value.detail)


def test_parse_bulk_ids_rejects_overflow() -> None:
    from fastapi import HTTPException

    raw = [str(uuid.uuid4()) for _ in range(BULK_MAX_IDS + 1)]
    with pytest.raises(HTTPException) as exc:
        parse_bulk_ids(raw)
    assert exc.value.status_code == 422
    assert "bulk_too_many_ids" in str(exc.value.detail)


def test_parse_bulk_ids_rejects_non_uuid() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_bulk_ids(["not-a-uuid"])
    assert exc.value.status_code == 422
    assert "bulk_invalid_id" in str(exc.value.detail)


def test_parse_bulk_ids_deduplicates_silently() -> None:
    same = str(uuid.uuid4())
    parsed = parse_bulk_ids([same, same, same])
    assert len(parsed) == 1


def test_parse_extend_duration_rejects_missing() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_extend_duration(None)
    assert exc.value.status_code == 422


def test_parse_extend_duration_rejects_unknown_value() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_extend_duration("100y")
    assert exc.value.status_code == 422
    assert "bulk_invalid_extend_duration" in str(exc.value.detail)


def test_parse_extend_duration_maps_known_values() -> None:
    assert parse_extend_duration("1d") == timedelta(days=1)
    assert parse_extend_duration("7d") == timedelta(days=7)
    assert parse_extend_duration("30d") == timedelta(days=30)


# ---------------------------------------------------------------------------
# Integration -- countdown badge in the list HTML
# ---------------------------------------------------------------------------


def test_list_renders_countdown_badge_for_active_memory() -> None:
    """An ``expires_at`` future-dated memory shows a countdown badge."""
    _seed_tenant(_TENANT_A, "tenant-a")
    future_expiry = datetime.now(UTC) + timedelta(days=3, hours=4)
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="will-expire",
        body="hot tip",
        user_sub=_OP_A,
        expires_at=future_expiry,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "expires in" in body
    # The badge carries a ``data-countdown`` attribute so external
    # observers (selenium tests, browser devtools) can read the cue
    # without scraping inner text.
    assert "data-countdown=" in body
    assert "will-expire" in body


def test_list_renders_recently_expired_section_for_past_expires_at() -> None:
    """Memory with ``expires_at`` in the past lands in the recently-expired bucket."""
    _seed_tenant(_TENANT_A, "tenant-a")
    past = datetime.now(UTC) - timedelta(hours=1)
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="stale-note",
        body="should be greyed",
        user_sub=_OP_A,
        expires_at=past,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Recently expired" in body
    assert 'id="memory-recently-expired"' in body
    assert "stale-note" in body
    # The expired card carries ``data-expired="true"`` so observers
    # can target the greyed bucket without inferring from siblings.
    assert 'data-expired="true"' in body


def test_list_60s_refresh_attribute_present() -> None:
    """The cards wrapper carries ``hx-trigger="every 60s"`` per the AC."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'hx-trigger="every 60s"' in body
    assert 'id="memory-cards"' in body


def test_list_htmx_60s_refresh_returns_partial() -> None:
    """An HTMX poll against ``/ui/memory`` returns just the cards partial."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="alive",
        body="b",
        user_sub=_OP_A,
        expires_at=datetime.now(UTC) + timedelta(days=2),
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "<title>" not in body
    # The wrapper retains the refresh trigger so subsequent polls fire.
    assert 'hx-trigger="every 60s"' in body
    assert "expires in" in body


# ---------------------------------------------------------------------------
# Integration -- bulk delete / bulk extend via POST /ui/memory/bulk
# ---------------------------------------------------------------------------


def test_bulk_delete_removes_writable_rows_and_returns_partial() -> None:
    """POST /ui/memory/bulk action=delete deletes the writable rows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keep_id = _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="keep",
        body="k",
        user_sub=_OP_A,
    )
    drop_id = _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="drop",
        body="d",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "delete", "ids": [str(drop_id)], "scope": "all"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # Flash banner reports the outcome.
    assert "Bulk: 1 deleted" in body
    # The kept row is still in the rendered list; the dropped row is not.
    assert "keep" in body
    assert "drop" not in body
    # Row physically gone from the DB.
    assert _read_doc(drop_id) is None
    assert _read_doc(keep_id) is not None


def test_bulk_extend_pushes_expires_at_into_the_future() -> None:
    """POST /ui/memory/bulk action=extend updates expires_at to now + duration."""
    _seed_tenant(_TENANT_A, "tenant-a")
    soon = datetime.now(UTC) + timedelta(hours=2)
    doc_id = _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="bump-me",
        body="b",
        user_sub=_OP_A,
        expires_at=soon,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    # ``MemoryService.remember`` re-runs the indexer's re-embed branch
    # when the body hash matches; stub the embedding service so the
    # test doesn't pull the wheel.
    from unittest.mock import AsyncMock, patch

    try:
        with patch("meho_backplane.retrieval.indexer.get_embedding_service") as mock_embed_factory:
            mock_embed_factory.return_value = type(
                "_Stub", (), {"encode_one": AsyncMock(return_value=[0.0] * 384)}
            )()
            response = client.post(
                "/ui/memory/bulk",
                data={
                    "action": "extend",
                    "ids": [str(doc_id)],
                    "extend_duration": "7d",
                    "scope": "all",
                },
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Bulk: 1 extended" in body
    # The row's expires_at is now ~7 days out (well beyond the original 2h).
    doc = _read_doc(doc_id)
    assert doc is not None
    new_iso = doc.doc_metadata["expires_at"]
    new_expires = datetime.fromisoformat(new_iso)
    assert new_expires > soon + timedelta(days=6)


def test_bulk_delete_denies_tenant_scoped_for_non_admin_operator() -> None:
    """An operator (not tenant_admin) cannot bulk-delete a tenant-scoped memory."""
    _seed_tenant(_TENANT_A, "tenant-a")
    doc_id = _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="shared-rule",
        body="shared",
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "delete", "ids": [str(doc_id)], "scope": "all"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    # The handler accepts the request and reports the RBAC denial in
    # the flash banner; the row remains untouched.
    assert response.status_code == 200, response.text
    assert "denied" in response.text
    assert _read_doc(doc_id) is not None


def test_bulk_delete_cross_tenant_id_falls_into_missing_bucket() -> None:
    """A doc id from another tenant is silently missing, never acted on."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    # Owned by tenant B; operator authenticates against tenant A.
    other_tenant_doc = _seed_memory(
        tenant_id=_TENANT_B,
        scope=MemoryScope.USER,
        slug="not-yours",
        body="z",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "delete", "ids": [str(other_tenant_doc)], "scope": "all"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "not found" in response.text
    # Tenant B's row is untouched.
    assert _read_doc(other_tenant_doc) is not None


def test_bulk_post_without_csrf_token_rejected() -> None:
    """POST /ui/memory/bulk without the CSRF header is 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    doc_id = _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="x",
        body="x",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    # ``with_csrf=False`` means the cookie isn't seeded; the middleware
    # rejects on the missing token half.
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=False)
    try:
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "delete", "ids": [str(doc_id)], "scope": "all"},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_bulk_post_with_empty_ids_returns_422() -> None:
    """Submitting the form with no selection returns 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "delete", "scope": "all"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422


def test_bulk_post_with_invalid_action_returns_422() -> None:
    """Submitting an unknown action value returns 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    doc_id = _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="x",
        body="x",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "incinerate", "ids": [str(doc_id)], "scope": "all"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422


def test_bulk_delete_unauthenticated_redirects_to_login() -> None:
    """POST /ui/memory/bulk without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.post(
            "/ui/memory/bulk",
            data={"action": "delete", "ids": [str(uuid.uuid4())]},
        )
    # The session middleware short-circuits on the unauthenticated request
    # before the CSRF check sees it -- main.py installs UISessionMiddleware
    # outermost and CSRFMiddleware inside, so the response is deterministically
    # a 302 to /ui/auth/login?return_to=... (not a 403 from the CSRF layer).
    # Asserting the exact status + Location shape pins this ordering so a
    # future middleware swap would surface as a loud regression instead of
    # silently flipping the auth boundary.
    assert response.status_code == 302, response.text
    assert response.headers["Location"].startswith("/ui/auth/login"), response.headers["Location"]
    assert "return_to=" in response.headers["Location"], response.headers["Location"]


def test_bulk_post_renders_checkboxes_only_for_writable_rows() -> None:
    """Tenant-scoped memory shows no checkbox for an ``operator`` role."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="own-row",
        body="x",
        user_sub=_OP_A,
    )
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="tenant-row",
        body="y",
    )
    # Sign in as a plain operator so tenant-scoped rows are read-only.
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    # Both cards rendered.
    assert "own-row" in body
    assert "tenant-row" in body
    # Only the own (user-scoped) row carries the checkbox label.
    assert 'aria-label="Select memory user/own-row"' in body
    assert "Select memory tenant/tenant-row" not in body


# ---------------------------------------------------------------------------
# Service-level -- apply_bulk_action via direct unit call
# ---------------------------------------------------------------------------


def test_apply_bulk_action_unit_delete_counts_succeeded_denied_missing() -> None:
    """Unit test: the result tally is honest across the three buckets."""
    _seed_tenant(_TENANT_A, "tenant-a")
    own = _seed_memory(
        tenant_id=_TENANT_A, scope=MemoryScope.USER, slug="own", body="o", user_sub=_OP_A
    )
    tenant_row = _seed_memory(
        tenant_id=_TENANT_A, scope=MemoryScope.TENANT, slug="tenant", body="t"
    )
    missing_id = uuid.uuid4()
    from meho_backplane.auth.operator import Operator

    operator = Operator(
        sub=_OP_A,
        raw_jwt="test",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )

    async def _run() -> Any:
        return await apply_bulk_action(
            operator,
            action="delete",
            ids=[own, tenant_row, missing_id],
        )

    result = asyncio.run(_run())
    assert result.requested == 3
    assert result.succeeded == 1  # own
    assert result.denied == 1  # tenant_row (operator role can't write tenant scope)
    assert result.missing == 1  # missing_id
    assert "1 deleted" in result.flash_message
    assert "1 denied" in result.flash_message
    assert "1 not found" in result.flash_message
    # Physical state: own gone, tenant_row intact, missing never existed.
    assert _read_doc(own) is None
    assert _read_doc(tenant_row) is not None
