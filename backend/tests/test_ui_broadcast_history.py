# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast Last-24h history pane (#2549).

The history pane used to hard-drop agent-authored announcements
(``_is_audit_event``) so it was write-only for humans. #2549 turns that
drop into a user-facing ``?kind=`` filter and renders both event kinds:

* ``GET /ui/broadcast/history`` (no kind) renders both operations AND
  announcements into the seed data island.
* ``?kind=agent_announcement`` / ``?kind=operation`` each return only
  their kind (route test).
* Announcement free text is emitted through Jinja ``| tojson`` (which
  escapes ``<`` / ``>`` / ``&`` for the script-element text context) and
  bound via Alpine ``x-text`` on render, so an ``activity`` carrying
  ``<script>`` never reaches the DOM as live markup.
* A mixed pre/post-T1 replay (a pre-#2544 announcement carrying only
  ``event_kind``, a pre-migration operation carrying no ``kind``) renders
  200 without error (back-compat).

The read helper (:func:`list_recent_events_fail_soft`) is patched so the
route tests need no live Valkey; its dict shape is reproduced verbatim
(the fail-soft path serialises via ``_dump_event_plain`` — no
untrusted-text envelope — because this HTML sink escapes separately).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import (
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.broadcast import history as history_module
from meho_backplane.ui.routes.broadcast.history import (
    _event_kind,
    _matches_kind_filter,
    _normalise_kind_filter,
)
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + broadcast env vars; reset caches around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
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


def _seed_session_sync(*, tenant_id: uuid.UUID = _TENANT_A) -> uuid.UUID:
    from meho_backplane.db.engine import get_sessionmaker

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-42",
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _operation_dict(
    *,
    op_id: str = "vsphere.vm.list",
    kind: str | None = "operation",
) -> dict[str, Any]:
    """One audit-driven event dict as ``list_recent_events_fail_soft`` returns it.

    ``kind=None`` reproduces a pre-migration entry that carries no
    discriminator on the wire (inferred as an operation).
    """
    event: dict[str, Any] = {
        "id": "1715600000000-0",
        "cursor": "1715600000000-0",
        "event_id": str(uuid.uuid4()),
        "ts": "2026-05-13T00:00:00Z",
        "tenant_id": str(_TENANT_A),
        "principal_sub": "op-test",
        "target_name": "rdc-vcenter",
        "op_id": op_id,
        "op_class": "read",
        "result_status": "ok",
        "audit_id": "33333333-3333-3333-3333-333333333333",
        "payload": {"op_class": "read", "params": {}, "result_status": "ok"},
    }
    if kind is not None:
        event["kind"] = kind
    return event


def _announcement_dict(
    *,
    activity: str = "rotating tokens on cluster X",
    use_event_kind_only: bool = False,
) -> dict[str, Any]:
    """One announcement dict as the fail-soft (unwrapped) path returns it.

    ``use_event_kind_only`` reproduces a pre-#2544/T1 announcement that
    carries only the historical ``event_kind`` alias, no top-level
    ``kind`` (back-compat replay).
    """
    event: dict[str, Any] = {
        "id": "1715600000001-0",
        "cursor": "1715600000001-0",
        "event_kind": "agent_announcement",
        "ts": "2026-05-13T00:01:00Z",
        "tenant_id": str(_TENANT_A),
        "principal_sub": "agent-bot",
        "activity": activity,
        "target": "cluster-x",
        "targets": ["kube-a"],
        "planned_op_class": "credential_write",
        "ttl_minutes": 30,
        "work_ref": "gh:evoila/meho#123",
        "phase": "start",
    }
    if not use_event_kind_only:
        event["kind"] = "agent_announcement"
    return event


def _patch_history(events: list[dict[str, Any]]) -> AsyncMock:
    """Patch the route's read helper to return *events* (newest-last order).

    The route reverses to newest-first, so the returned list is the
    XRANGE ascending order; the mock ignores its arguments.
    """
    mock = AsyncMock(return_value={"events": events, "next_cursor": None})
    return mock


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------


def test_event_kind_normalises_discriminator() -> None:
    assert _event_kind({"kind": "agent_announcement"}) == "agent_announcement"
    assert _event_kind({"event_kind": "agent_announcement"}) == "agent_announcement"
    assert _event_kind({"kind": "operation"}) == "operation"
    # Pre-migration entry with neither field → operation default.
    assert _event_kind({"op_id": "x"}) == "operation"


def test_normalise_kind_filter_clamps_unknown_to_none() -> None:
    assert _normalise_kind_filter("operation") == "operation"
    assert _normalise_kind_filter("agent_announcement") == "agent_announcement"
    assert _normalise_kind_filter("") is None
    assert _normalise_kind_filter(None) is None
    assert _normalise_kind_filter("garbage") is None


def test_matches_kind_filter_none_keeps_both() -> None:
    assert _matches_kind_filter({"kind": "operation"}, None) is True
    assert _matches_kind_filter({"kind": "agent_announcement"}, None) is True


def test_matches_kind_filter_selects_single_kind() -> None:
    op = {"kind": "operation"}
    ann = {"kind": "agent_announcement"}
    assert _matches_kind_filter(op, "operation") is True
    assert _matches_kind_filter(ann, "operation") is False
    assert _matches_kind_filter(ann, "agent_announcement") is True
    assert _matches_kind_filter(op, "agent_announcement") is False


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def test_history_renders_both_kinds_by_default() -> None:
    """No kind filter → both operations and announcements seed the island."""
    session_id = _seed_session_sync()
    events = [_operation_dict(op_id="vsphere.vm.list"), _announcement_dict()]
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    assert "vsphere.vm.list" in body
    assert "agent_announcement" in body
    assert "rotating tokens on cluster X" in body


def test_history_announcement_row_markup_is_agent_authored() -> None:
    """The row partial carries the announcement branch + escaped activity binding."""
    session_id = _seed_session_sync()
    events = [_announcement_dict()]
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    # Announcement branch + its structured-field bindings render server-side
    # (Alpine markup; evaluated client-side but authored here).
    assert "isAnnouncement(ev)" in body
    assert 'x-text="ev.activity"' in body
    assert "announcementTargets(ev)" in body
    assert "announcementMeta(ev)" in body
    assert "phaseBadgeClass(ev.phase)" in body


def test_history_activity_is_html_escaped_in_island() -> None:
    """A ``<script>``-bearing activity is escaped by ``| tojson`` in the island."""
    session_id = _seed_session_sync()
    events = [_announcement_dict(activity="<script>alert(1)</script>")]
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    # The raw tag never appears as live markup; tojson escapes ``<`` / ``>``.
    assert "<script>alert(1)</script>" not in body
    assert "\\u003cscript\\u003e" in body


def test_history_kind_filter_announcement_only() -> None:
    """``?kind=agent_announcement`` returns only announcements."""
    session_id = _seed_session_sync()
    events = [_operation_dict(op_id="vsphere.vm.list"), _announcement_dict()]
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history", params={"kind": "agent_announcement"})
    assert response.status_code == 200
    body = response.text
    assert "rotating tokens on cluster X" in body
    assert "vsphere.vm.list" not in body


def test_history_kind_filter_operation_only() -> None:
    """``?kind=operation`` returns only audit-driven operations."""
    session_id = _seed_session_sync()
    events = [_operation_dict(op_id="vsphere.vm.list"), _announcement_dict()]
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history", params={"kind": "operation"})
    assert response.status_code == 200
    body = response.text
    assert "vsphere.vm.list" in body
    assert "rotating tokens on cluster X" not in body


def test_history_mixed_pre_post_t1_replay_renders_without_error() -> None:
    """A pre-migration op (no kind) + pre-T1 announcement (event_kind only) render 200."""
    session_id = _seed_session_sync()
    events = [
        _operation_dict(op_id="legacy.op", kind=None),
        _announcement_dict(activity="legacy announce", use_event_kind_only=True),
    ]
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    assert "legacy.op" in body
    assert "legacy announce" in body
    # The pre-migration op still counts as an operation under the filter.
    with patch.object(history_module, "list_recent_events_fail_soft", _patch_history(events)):
        client = _authenticated_client(session_id)
        op_only = client.get("/ui/broadcast/history", params={"kind": "operation"})
    assert op_only.status_code == 200
    assert "legacy.op" in op_only.text
    assert "legacy announce" not in op_only.text
