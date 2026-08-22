# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Full-dispatch wire + envelope pins for the governed content-library ISO path (#3086).

The recipe (``docs/codebase/vmware-rest-governed-iso-path.md``) rides
**raw ingested ops** — no composite — so the load-bearing questions are
all generic-dispatch questions, each pinned here through the real
``dispatch()`` chain (policy gate → connector resolution → session mint
→ ``dispatch_ingested`` → audit + broadcast) against a respx-mocked
vCenter, mirroring ``test_connectors_vmware_rest_credread.py``:

* **Flat ``/api`` bodies, never the ``/rest`` envelope** (the #2973 /
  #3071 bug class). ``_unwrap_body`` sends the caller's ``body`` param
  verbatim as the JSON request body; the byte-for-byte wire assertions
  here fail the moment anything re-wraps it in ``{"spec": ...}``.
* **``?action=`` discriminators ride the descriptor path key** — the
  ingest parser keeps vCenter's ``?action=<verb>`` suffix in the path,
  and httpx carries it onto the wire under the ``/api`` mount prefix.
* **Response envelopes survive dispatch**: a bare-JSON-string ack (item
  id / session id / cdrom id) wraps to ``{"value": ...}`` per
  ``wrap_ok_result``; a ``204 No Content`` ack returns ``{}`` (#3082);
  dict / list payloads pass through (small sets — no JSONFlux handle).
* **Governance engages**: the ingested writes land ``caution`` +
  ``requires_approval=False`` (auto-execute for a human operator, one
  audit + broadcast per dispatch), and an operator who tightens
  ``requires_approval`` on the mount op parks the dispatch in the
  approval queue *before* any vendor call fires.

Descriptor seeds come from ``tests/_governed_iso_recipe.py`` — the
same table the shelf-gated reconcile lane grounds against the pinned
``vcenter-9.0/vcenter.yaml``, so the ops pinned here are the ops the
pinned spec actually serves.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._governed_iso_recipe import RECIPE_OPS
from ._vault_fakes import install_fake_client

_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"
_CONNECTOR_ID = "vmware-rest-9.0"

#: RFC 6761 ``.test.``/``.invalid`` host — guarantees no real egress.
_VCENTER_HOST = "vcenter-iso-recipe.test.invalid"
_VCENTER_BASE_URL = f"https://{_VCENTER_HOST}"
_SESSION_TOKEN = "iso-recipe-session-token"

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000b1b1")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Fresh dispatcher caches + a clean vmware-rest registration per test."""
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VmwareRestConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canned service-account creds via the in-process Vault fake.

    The resolver builds ``VmwareRestConnector()`` with the *default*
    session loader, so the real operator-context Vault read path runs
    against the fake — no injected stub loader.
    """
    install_fake_client(
        monkeypatch,
        secret={"username": "svc-iso-recipe", "password": "iso-recipe-pass"},
    )


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record broadcast events so the audit/broadcast leg is asserted."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub for the seeded descriptor rows."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _IsoRecipeTarget:
    """Resolver-shaped + vmware-loader-shaped target for the mocked vCenter."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "vcenter-iso-recipe"
        self.host = _VCENTER_HOST
        self.port = 443
        self.secret_ref = "targets/op-iso/vcenter-iso-recipe"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-iso-recipe",
        name="ISO Recipe Operator",
        email=None,
        raw_jwt="op.iso.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_recipe_descriptor(
    session: AsyncSession,
    op_id: str,
    embedding: list[float],
    *,
    requires_approval: bool = False,
) -> None:
    """Insert one enabled ingested descriptor row from the recipe table."""
    op = RECIPE_OPS[op_id]
    method, _, path = op_id.partition(":")
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        handler_ref=None,
        summary=op.stage,
        description=op.stage,
        tags=["content-library-iso"],
        parameter_schema=op.parameter_schema,
        response_schema=None,
        llm_instructions=None,
        safety_level=op.safety_level,
        requires_approval=requires_approval,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()


def _mock_session_routes(mock: respx.MockRouter) -> None:
    mock.post("/api/session").respond(200, json=_SESSION_TOKEN)
    mock.delete("/api/session").respond(204)


@pytest.mark.asyncio
async def test_iso_mount_sends_flat_body_and_wraps_scalar_ack(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Mount rides ``?action=mount`` with the flat two-field body; string ack wraps.

    The wire body must be byte-for-byte the caller's ``body`` param —
    ``{"library_item", "vm"}`` at the TOP level, no ``{"spec": ...}``
    envelope (#2973/#3071 class) — and the bare-JSON-string CD-ROM id
    vCenter acks with must survive dispatch as ``{"value": <id>}``.
    """
    op_id = "POST:/vcenter/iso/image?action=mount"
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)
    body = {"library_item": "item-11", "vm": "vm-77"}

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        mount_route = mock.post("/api/vcenter/iso/image", params={"action": "mount"}).respond(
            200, json="16002"
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"body": body},
        )

    assert result.status == "ok", result.error
    assert mount_route.call_count == 1
    wire_request = mount_route.calls.last.request
    assert wire_request.url.params["action"] == "mount"
    assert json.loads(wire_request.content) == body  # flat — no {"spec": ...} envelope
    # Bare-string ack survives dispatch under the documented scalar envelope.
    assert result.result == {"value": "16002"}
    assert result.handle is None
    # The governed path engaged: exactly one audit/broadcast for the dispatch.
    assert len(captured_events) == 1


@pytest.mark.asyncio
async def test_iso_mount_parks_in_approval_queue_when_tightened(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """``requires_approval=True`` parks the mount BEFORE any vendor call.

    Ingest defaults the recipe's writes to ``requires_approval=False``
    (auto-execute for human operators); an operator who tightens the
    mount op in ingest review gets the approval-queue park — and the
    park must fire ahead of the vCenter round-trip.
    """
    op_id = "POST:/vcenter/iso/image?action=mount"
    await _seed_recipe_descriptor(
        session,
        op_id,
        stub_embedding_service.encode_one.return_value,
        requires_approval=True,
    )

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        mount_route = mock.post("/api/vcenter/iso/image", params={"action": "mount"}).respond(
            200, json="16002"
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"body": {"library_item": "item-11", "vm": "vm-77"}},
        )

    assert result.status == "awaiting_approval"
    assert mount_route.call_count == 0  # parked before the connector fired


@pytest.mark.asyncio
async def test_iso_unmount_sends_flat_body_and_204_ack_is_empty_dict(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Unmount rides ``?action=unmount`` with ``{vm, cdrom}``; 204 acks as ``{}`` (#3082)."""
    op_id = "POST:/vcenter/iso/image?action=unmount"
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)
    body = {"vm": "vm-77", "cdrom": "16002"}

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        unmount_route = mock.post("/api/vcenter/iso/image", params={"action": "unmount"}).respond(
            204
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"body": body},
        )

    assert result.status == "ok", result.error
    assert json.loads(unmount_route.calls.last.request.content) == body
    assert result.result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "wire_path", "body", "vendor_ack"),
    [
        (
            "POST:/content/library/item",
            "/api/content/library/item",
            {"library_id": "lib-1", "name": "esxi-installer", "type": "iso"},
            "item-11",
        ),
        (
            "POST:/content/library/item/update-session",
            "/api/content/library/item/update-session",
            {"library_item_id": "item-11"},
            "us-42",
        ),
    ],
)
async def test_create_ops_send_flat_model_bodies_and_wrap_string_ids(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    op_id: str,
    wire_path: str,
    body: dict[str, Any],
    vendor_ack: str,
) -> None:
    """Item / update-session create send the *Model flat; string ids wrap.

    Both creates take their model at the TOP level of the ``/api`` body
    (the legacy ``/rest`` mount wrapped these in ``create_spec`` — the
    envelope class this lane bans from the wire) and ack with a bare
    JSON string id that must survive as ``{"value": <id>}``.
    """
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        create_route = mock.post(wire_path).respond(201, json=vendor_ack)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"body": body},
        )

    assert result.status == "ok", result.error
    assert json.loads(create_route.calls.last.request.content) == body
    assert result.result == {"value": vendor_ack}


@pytest.mark.asyncio
async def test_file_add_substitutes_session_path_and_sends_pull_source_verbatim(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """The PULL file-add: ``{updateSessionId}`` → URL; nested AddSpec rides flat.

    ``source_type="PULL"`` + ``source_endpoint.uri`` is the governed
    import-from-URL core — vCenter itself fetches the ISO from the HTTP
    depot, so no file bytes ever transit MEHO. The nested
    ``source_endpoint`` object must reach the wire verbatim inside the
    flat AddSpec body.
    """
    op_id = "POST:/content/library/item/update-session/{updateSessionId}/file"
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)
    body = {
        "name": "esxi.iso",
        "source_type": "PULL",
        "source_endpoint": {"uri": "http://depot.lab.invalid/isos/esxi.iso"},
    }

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        file_route = mock.post("/api/content/library/item/update-session/us-42/file").respond(
            200,
            json={"name": "esxi.iso", "source_type": "PULL", "status": "WAITING_FOR_TRANSFER"},
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"updateSessionId": "us-42", "body": body},
        )

    assert result.status == "ok", result.error
    assert file_route.call_count == 1
    assert json.loads(file_route.calls.last.request.content) == body
    assert isinstance(result.result, dict)
    assert result.result["status"] == "WAITING_FOR_TRANSFER"


@pytest.mark.asyncio
async def test_session_complete_sends_no_body_and_204_ack_is_empty_dict(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """``?action=complete`` is a bodyless verb: empty wire body, 204 → ``{}``."""
    op_id = "POST:/content/library/item/update-session/{updateSessionId}?action=complete"
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        complete_route = mock.post(
            "/api/content/library/item/update-session/us-42",
            params={"action": "complete"},
        ).respond(204)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"updateSessionId": "us-42"},
        )

    assert result.status == "ok", result.error
    wire_request = complete_route.calls.last.request
    assert wire_request.url.params["action"] == "complete"
    assert wire_request.content == b""  # no body param declared → nothing on the wire
    assert result.result == {}


@pytest.mark.asyncio
async def test_session_poll_returns_state_object_passthrough(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """The poll GET substitutes the session id and passes the state object through."""
    op_id = "GET:/content/library/item/update-session/{updateSessionId}"
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)
    state = {"id": "us-42", "state": "DONE", "client_progress": 100}

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        poll_route = mock.get("/api/content/library/item/update-session/us-42").respond(
            200, json=state
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"updateSessionId": "us-42"},
        )

    assert result.status == "ok", result.error
    assert poll_route.call_count == 1
    assert result.result == state


@pytest.mark.asyncio
async def test_item_list_merges_query_param_into_marker_path(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """The ``?library_id`` marker path merges the query param to a single value.

    The parser keys this op ``GET:/content/library/item?library_id``
    (required-query marker in the path). httpx merges the query bucket
    into the marker, so the wire carries exactly one
    ``library_id=<value>`` pair — pinned here because a doubled or
    empty param would 400 at vCenter.
    """
    op_id = "GET:/content/library/item?library_id"
    await _seed_recipe_descriptor(session, op_id, stub_embedding_service.encode_one.return_value)

    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        _mock_session_routes(mock)
        list_route = mock.get("/api/content/library/item", params={"library_id": "lib-1"}).respond(
            200, json=["item-11"]
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_IsoRecipeTarget(),
            params={"library_id": "lib-1"},
        )

    assert result.status == "ok", result.error
    wire_url = list_route.calls.last.request.url
    assert wire_url.params.get_list("library_id") == ["lib-1"]
    assert result.result == ["item-11"]
