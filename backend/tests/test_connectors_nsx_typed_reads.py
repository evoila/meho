# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the NSX audited-read typed ops (#2302).

Coverage matrix (per Task #2302 acceptance criteria):

* **Zero-catalog typed dispatch (AC #1).** Each audited read
  (nsx.node.status / nsx.cluster.status / nsx.backup.config /
  nsx.backup.status / nsx.transport_zone.list / nsx.tier1.list /
  nsx.alarm.list) dispatches through :func:`~meho_backplane.operations.dispatch`
  against a respx-mocked NSX manager with **only** the typed registrar
  run -- no ingested descriptor rows -- and returns ``status="ok"``. The
  persisted descriptor carries ``source_kind="typed"``.
* **Backup config is first-class with retention fields surfaced, secrets
  scrubbed (AC #2).** nsx.backup.config surfaces backup_enabled +
  passphrase_configured + the backup_schedule / remote_file_server the
  disk-fill class hinges on, and never returns the passphrase or a nested
  SFTP credential.
* **Session recovery via the #2067 seam (AC #3).** A 401 on the first
  downstream GET is recovered by the dispatcher's auth-class arm calling
  the connector's public ``invalidate_session`` hook and re-dispatching
  once; the op returns ``status="ok"``.
* **Registration-shape invariants.** All seven carry
  ``safety_level="safe"``, ``requires_approval=False``, a ``read-only``
  tag, ``additionalProperties=False`` on the parameter schema, and
  non-empty llm_instructions. No write op is registered.

Mirrors :mod:`tests.test_connectors_argocd_reads` for the dispatch
lifecycle + embedding stub and :mod:`tests.test_connectors_nsx_e2e` for
the NSX session-establish + session-loader stub.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.nsx import (
    NSX_CONNECTOR_ID,
    NSX_IMPL_ID,
    NSX_PRODUCT,
    NSX_TYPED_OPS,
    NSX_VERSION,
    NsxConnector,
    register_nsx_typed_operations,
)
from meho_backplane.connectors.nsx.typed_reads import REDACTED
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_NSX_HOST = "nsx-typed.test.invalid"
_NSX_BASE_URL = f"https://{_NSX_HOST}"

_XSRF_HEADERS = {
    "X-XSRF-TOKEN": "typed-xsrf-token",
    "Set-Cookie": "JSESSIONID=typed-session-id; Path=/; HttpOnly",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=NSX_PRODUCT,
        version=NSX_VERSION,
        impl_id=NSX_IMPL_ID,
        cls=NsxConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration/search don't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _NsxReadTarget:
    """Target satisfying both ``NsxTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = NSX_PRODUCT
        self.fingerprint = type("_FP", (), {"version": NSX_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
        self.name = "nsx-typed"
        self.host = _NSX_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/nsx-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-nsx",
        name="NSX Reads Operator",
        email=None,
        raw_jwt="op.reads.nsx.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000b4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _nsx_session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Static session loader -- bypasses the live operator-context Vault read."""
    return {"username": "nsx-typed-svc", "password": "nsx-typed-pw"}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> NsxConnector:
    """Run the typed registrar (only) and return the session-stubbed instance."""
    await register_nsx_typed_operations()
    instance = get_or_create_connector_instance(NsxConnector)
    instance._session_loader = _nsx_session_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# AC #1 -- zero-catalog typed dispatch
# ---------------------------------------------------------------------------

_NODE_PAYLOAD: dict[str, Any] = {
    "node_version": NSX_VERSION,
    "kernel_version": "9.0.2.0.0",
    "node_uuid": "deadbeef-typed",
    "hostname": "nsxmgr-typed",
    "external_id": "typed-external-id",
}
_CLUSTER_PAYLOAD: dict[str, Any] = {
    "mgmt_cluster_status": {"status": "STABLE"},
    "control_cluster_status": {"status": "STABLE"},
    "detail": [{"member_uuid": "m1", "status": "CONNECTED"}],
}
_BACKUP_STATUS_PAYLOAD: dict[str, Any] = {
    "current_backup_operation_status": {"backup_id": "b-1", "success": True}
}
_TZ_PAYLOAD: dict[str, Any] = {
    "results": [{"id": "tz-overlay", "display_name": "overlay", "tz_type": "OVERLAY"}]
}
_TIER1_PAYLOAD: dict[str, Any] = {
    "results": [{"id": "t1-a", "display_name": "tenant-a", "tier0_path": "/infra/tier-0s/t0"}]
}
_ALARM_PAYLOAD: dict[str, Any] = {
    "results": [
        {
            "id": "alarm-1",
            "status": "OPEN",
            "severity": "CRITICAL",
            "feature_name": "manager_health",
            "event_type": "manager_cpu_usage_high",
        }
    ]
}


@pytest.mark.parametrize(
    ("op_id", "params", "method", "path", "payload"),
    [
        ("nsx.node.status", {}, "GET", "/api/v1/node", _NODE_PAYLOAD),
        ("nsx.cluster.status", {}, "GET", "/api/v1/cluster/status", _CLUSTER_PAYLOAD),
        (
            "nsx.backup.status",
            {},
            "GET",
            "/api/v1/cluster/backups/status",
            _BACKUP_STATUS_PAYLOAD,
        ),
        (
            "nsx.transport_zone.list",
            {},
            "GET",
            "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones",
            _TZ_PAYLOAD,
        ),
        ("nsx.tier1.list", {}, "GET", "/policy/api/v1/infra/tier-1s", _TIER1_PAYLOAD),
        ("nsx.alarm.list", {}, "GET", "/api/v1/alarms", _ALARM_PAYLOAD),
    ],
)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_zero_catalog(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    """AC #1: each audited read dispatches typed with zero ingested catalog state."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_NSX_BASE_URL, assert_all_called=False) as mock:
        mock.post("/api/session/create").respond(200, headers=_XSRF_HEADERS)
        route = mock.request(method, path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=NSX_CONNECTOR_ID,
            op_id=op_id,
            target=_NsxReadTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    assert route.called and route.call_count == 1
    # The XSRF token the session establish primed rides on the read.
    assert route.calls[0].request.headers.get("X-XSRF-TOKEN") == "typed-xsrf-token"


@pytest.mark.asyncio
async def test_registered_ops_are_source_kind_typed(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: the persisted descriptor rows carry ``source_kind='typed'``."""
    await register_nsx_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == NSX_PRODUCT,
                    EndpointDescriptor.impl_id == NSX_IMPL_ID,
                    EndpointDescriptor.op_id.in_([op.op_id for op in NSX_TYPED_OPS]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in NSX_TYPED_OPS}
    assert all(r.source_kind == "typed" for r in rows)
    assert all(r.handler_ref is not None for r in rows)


@pytest.mark.asyncio
async def test_alarm_list_forwards_filters(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """nsx.alarm.list forwards status / feature_name / severity as query params."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_NSX_BASE_URL, assert_all_called=False) as mock:
        mock.post("/api/session/create").respond(200, headers=_XSRF_HEADERS)
        route = mock.get("/api/v1/alarms").respond(200, json=_ALARM_PAYLOAD)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=NSX_CONNECTOR_ID,
            op_id="nsx.alarm.list",
            target=_NsxReadTarget(),
            params={"status": "OPEN", "feature_name": "manager_health", "severity": "CRITICAL"},
        )

    assert result.status == "ok", result.error
    sent = route.calls[0].request.url
    assert sent.params.get("status") == "OPEN"
    assert sent.params.get("feature_name") == "manager_health"
    assert sent.params.get("severity") == "CRITICAL"


# ---------------------------------------------------------------------------
# AC #2 -- backup config first-class + retention surfaced + secrets scrubbed
# ---------------------------------------------------------------------------

_BACKUP_CONFIG_PAYLOAD: dict[str, Any] = {
    "backup_enabled": True,
    "passphrase": "super-secret-passphrase",
    "backup_schedule": {
        "resource_type": "WeeklyBackupSchedule",
        "days_of_week": [0],
        "hour_of_day": 2,
    },
    "remote_file_server": {
        "server": "sftp.backup.invalid",
        "port": 22,
        "directory_path": "/backups/nsx",
        "protocol": {
            "protocol_name": "sftp",
            "authentication_scheme": {"scheme_name": "PASSWORD", "password": "sftp-pw"},
        },
    },
    "inventory_summary_interval": 240,
    "after_inventory_update_interval": 300,
}


@pytest.mark.asyncio
async def test_backup_config_surfaces_retention_and_scrubs_secrets(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #2: retention fields surfaced; passphrase + nested SFTP creds never returned."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_NSX_BASE_URL, assert_all_called=False) as mock:
        mock.post("/api/session/create").respond(200, headers=_XSRF_HEADERS)
        mock.get("/api/v1/cluster/backups/config").respond(200, json=_BACKUP_CONFIG_PAYLOAD)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=NSX_CONNECTOR_ID,
            op_id="nsx.backup.config",
            target=_NsxReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    body = result.result
    assert isinstance(body, dict)
    # Retention-relevant fields are first-class / preserved.
    assert body["backup_enabled"] is True
    assert body["passphrase_configured"] is True
    config = body["config"]
    assert config["backup_schedule"]["resource_type"] == "WeeklyBackupSchedule"
    assert config["remote_file_server"]["directory_path"] == "/backups/nsx"
    assert config["inventory_summary_interval"] == 240
    # Secrets are scrubbed at the boundary -- top-level passphrase + nested
    # SFTP password both masked, and the raw values appear nowhere.
    assert config["passphrase"] == REDACTED
    assert config["remote_file_server"]["protocol"]["authentication_scheme"]["password"] == REDACTED
    serialised = repr(result.result)
    assert "super-secret-passphrase" not in serialised
    assert "sftp-pw" not in serialised


# ---------------------------------------------------------------------------
# AC #3 -- session recovery via the #2067 dispatch-path seam
# ---------------------------------------------------------------------------


def test_connector_advertises_public_invalidate_session() -> None:
    """AC #3: the #2067 duck-typed hook is present (was private-only before #2302)."""
    hook = getattr(NsxConnector, "invalidate_session", None)
    assert callable(hook)


@pytest.mark.asyncio
async def test_typed_dispatch_recovers_from_session_expiry(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #3: a 401 on the first read is recovered via invalidate_session + re-dispatch."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_NSX_BASE_URL, assert_all_called=False) as mock:
        session_route = mock.post("/api/session/create")
        session_route.side_effect = [
            httpx.Response(200, headers=_XSRF_HEADERS),
            httpx.Response(
                200,
                headers={
                    "X-XSRF-TOKEN": "typed-xsrf-token-2",
                    "Set-Cookie": "JSESSIONID=typed-session-id-2; Path=/; HttpOnly",
                },
            ),
        ]
        node_route = mock.get("/api/v1/node")
        node_route.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json=_NODE_PAYLOAD),
        ]
        result = await dispatch(
            operator=_make_operator(),
            connector_id=NSX_CONNECTOR_ID,
            op_id="nsx.node.status",
            target=_NsxReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == _NODE_PAYLOAD
    # One 401 + one recovered 200 = two node calls; two session establishes.
    assert node_route.call_count == 2
    assert session_route.call_count == 2


# ---------------------------------------------------------------------------
# search_operations visibility + registration-shape invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_visible_to_search_operations(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The registered typed ops are retrievable via search_operations."""
    await register_nsx_typed_operations()

    result = await search_operations(
        _make_operator(),
        {"connector_id": NSX_CONNECTOR_ID, "query": "nsx backup cluster alarms", "limit": 25},
    )
    found = {hit["op_id"] for hit in result["hits"]}
    expected = {op.op_id for op in NSX_TYPED_OPS}
    assert expected <= found, f"missing from search: {expected - found}"


_EXPECTED_OP_IDS = {
    "nsx.node.status",
    "nsx.cluster.status",
    "nsx.backup.config",
    "nsx.backup.status",
    "nsx.transport_zone.list",
    "nsx.tier1.list",
    "nsx.alarm.list",
}


def test_typed_ops_table_is_exactly_the_audited_read_set() -> None:
    assert {op.op_id for op in NSX_TYPED_OPS} == _EXPECTED_OP_IDS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in NSX_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in NSX_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_when_to_use_and_output_shape(op_id: str) -> None:
    op = next(o for o in NSX_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    assert op.llm_instructions.get("when_to_use", "").strip() != ""
    assert "output_shape" in op.llm_instructions


def test_no_write_or_mutating_op_is_registered() -> None:
    """Read-only Task: no create/write op ships (tier-1 create is out of scope)."""
    for op in NSX_TYPED_OPS:
        assert op.safety_level == "safe"
        assert not any(token in op.op_id for token in (".create", ".delete", ".update", ".set"))
        assert "write" not in op.tags
