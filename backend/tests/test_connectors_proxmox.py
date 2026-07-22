# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""ProxmoxConnector recorded-fixture tests (#2238).

Covers the read/write Proxmox VE connector end to end against a respx-mocked
``/api2/json`` surface (no running Proxmox, no live Vault):

* Registration: dual v2 registry entries + the read/write op set + the write
  op's ``requires_approval=True`` / ``safety_level="dangerous"``.
* Auth: API-token header (``PVEAPIToken=<id>=<secret>``, CSRF-exempt) and the
  ticket fallback (``PVEAuthCookie`` cookie + ``CSRFPreventionToken`` on
  writes).
* Passthrough allowlist: the two-layer path guard (schema pattern + handler).
* Approval routing: a USER dispatch of ``proxmox.api.write`` parks in the
  approval queue rather than hard-denying or reaching the cluster.
* Async writes: a UPID-returning write + ``proxmox.task.status`` polling.
* ``fingerprint()`` against a recorded version/nodes fixture.

Fake credential values only (obviously non-real) so the per-commit secret
scanner has nothing to flag.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx

import meho_backplane.connectors.proxmox  # noqa: F401 -- registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.gsm_creds import GcpSecretManagerReadError
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.proxmox import ProxmoxConnector
from meho_backplane.connectors.proxmox.ops import (
    PROXMOX_READ_OPS,
    ProxmoxMethodError,
    ProxmoxPathError,
    validate_api_path,
    validate_method,
)
from meho_backplane.connectors.proxmox.ops_write import PROXMOX_WRITE_OPS, parse_upid
from meho_backplane.connectors.proxmox.session import (
    ProxmoxCredentials,
    build_ticket_username,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.targets.resolver import resolve_target

# --- Fake, obviously-non-real credential values ---------------------------
_TOKEN_ID = "root@pam!mehotest"
_TOKEN_SECRET = "00000000-0000-0000-0000-000000000000"  # zero UUID, not real
_TICKET_USER = "root@pam"
_TICKET_PASSWORD = "fake-proxmox-password-DO-NOT-LEAK"
_TICKET_VALUE = "PVE:fake-ticket-DO-NOT-LEAK"
_CSRF_TOKEN = "fake:csrf:token"

_CONNECTOR_ID = "proxmox-api-8.x"
_TARGET_NAME = "rdc-proxmox-e2e"
_PVE_HOST = "proxmox-e2e.test.invalid"
_PVE_PORT = 8006
_PVE_BASE_URL = f"https://{_PVE_HOST}:{_PVE_PORT}"

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_OPERATOR = Operator(
    sub="proxmox-e2e-test",
    name="Proxmox E2E Operator",
    email=None,
    raw_jwt="<proxmox-e2e-raw-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)


# --- Lightweight target double for the non-dispatch unit tests ------------
@dataclass(frozen=True)
class _FakeTarget:
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    verify_tls: bool = False
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None


def _fake_target() -> _FakeTarget:
    return _FakeTarget(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name=_TARGET_NAME,
        host=_PVE_HOST,
        port=_PVE_PORT,
        secret_ref="rdc/proxmox/token",
        auth_model="shared_service_account",
    )


def _token_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> ProxmoxCredentials:
        return ProxmoxCredentials(mode="token", token_id=_TOKEN_ID, token_secret=_TOKEN_SECRET)

    return _load()


def _gsm_failing_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> ProxmoxCredentials:
        raise GcpSecretManagerReadError("gsm read failed")

    return _load()


def _ticket_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> ProxmoxCredentials:
        return ProxmoxCredentials(mode="ticket", username=_TICKET_USER, password=_TICKET_PASSWORD)

    return _load()


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


def test_dual_registration_versioned_and_wildcard() -> None:
    registry = all_connectors_v2()
    assert registry[("proxmox", "8.x", "proxmox-api")] is ProxmoxConnector
    assert registry[("proxmox", "", "")] is ProxmoxConnector


def test_op_set_and_write_approval() -> None:
    read_ids = {op.op_id for op in PROXMOX_READ_OPS}
    assert read_ids == {"proxmox.about", "proxmox.api.get", "proxmox.task.status"}
    for op in PROXMOX_READ_OPS:
        assert op.safety_level == "safe"
        assert op.requires_approval is False

    assert {op.op_id for op in PROXMOX_WRITE_OPS} == {"proxmox.api.write"}
    write = PROXMOX_WRITE_OPS[0]
    assert write.safety_level == "dangerous"
    assert write.requires_approval is True
    assert "write" in write.tags


# ---------------------------------------------------------------------------
# Passthrough allowlist (schema pattern + handler)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "/etc/passwd", "a/../b", "nodes//x", "a b", "a?b=1", "http://evil", "x%2e%2e"],
)
def test_path_allowlist_rejects(bad: str) -> None:
    with pytest.raises(ProxmoxPathError):
        validate_api_path(bad)


@pytest.mark.parametrize(
    "good",
    [
        "version",
        "cluster/status",
        "cluster/resources",
        "nodes/pve/qemu/100/status/start",
        "nodes/pve/tasks/UPID:pve:00001:type:root@pam:/status",
        "access/users/root@pam",
    ],
)
def test_path_allowlist_accepts(good: str) -> None:
    assert validate_api_path(good) == good


def test_method_allowlist_enforces_read_write_split() -> None:
    assert validate_method("get", frozenset({"GET", "HEAD"})) == "GET"
    with pytest.raises(ProxmoxMethodError):
        validate_method("POST", frozenset({"GET", "HEAD"}))
    with pytest.raises(ProxmoxMethodError):
        validate_method("GET", frozenset({"POST", "PUT", "DELETE"}))


def test_parse_upid() -> None:
    upid = "UPID:pve:0001A:0002B:64000000:qmclone:100:root@pam:"
    assert parse_upid(upid) == {"upid": upid, "node": "pve"}
    assert parse_upid(None) == {"upid": None, "node": None}
    assert parse_upid({"already": "object"}) == {"upid": None, "node": None}


def test_build_ticket_username() -> None:
    assert build_ticket_username("root", None) == "root@pam"
    assert build_ticket_username("alice", "ldap") == "alice@ldap"
    assert build_ticket_username("bob@pve", "pam") == "bob@pve"


# ---------------------------------------------------------------------------
# Auth: token header (CSRF-exempt) + ticket fallback (cookie + CSRF on writes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_auth_header_is_pveapitoken() -> None:
    connector = ProxmoxConnector(credentials_loader=_token_loader)
    try:
        headers = await connector.auth_headers(_fake_target(), _OPERATOR)
    finally:
        await connector.aclose()
    assert headers == {"Authorization": f"PVEAPIToken={_TOKEN_ID}={_TOKEN_SECRET}"}


@pytest.mark.asyncio
async def test_ticket_auth_mints_cookie_and_writes_carry_csrf() -> None:
    connector = ProxmoxConnector(credentials_loader=_ticket_loader)
    target = _fake_target()
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        ticket_route = mock.post("/api2/json/access/ticket").respond(
            200,
            json={"data": {"ticket": _TICKET_VALUE, "CSRFPreventionToken": _CSRF_TOKEN}},
        )
        write_route = mock.post("/api2/json/nodes/pve/qemu/100/status/stop").respond(
            200, json={"data": "UPID:pve:0001:qmstop:100:root@pam:"}
        )
        try:
            # A GET-style read carries only the cookie, no CSRF header.
            read_headers = await connector.auth_headers(target, _OPERATOR)
            assert read_headers == {"Cookie": f"PVEAuthCookie={_TICKET_VALUE}"}

            # A write attaches the CSRFPreventionToken.
            await connector._write_json(
                target,
                "POST",
                "/api2/json/nodes/pve/qemu/100/status/stop",
                operator=_OPERATOR,
            )
        finally:
            await connector.aclose()

    assert ticket_route.called
    sent = write_route.calls.last.request
    assert sent.headers.get("CSRFPreventionToken") == _CSRF_TOKEN
    assert sent.headers.get("Cookie") == f"PVEAuthCookie={_TICKET_VALUE}"


@pytest.mark.asyncio
async def test_rejects_unsupported_auth_model() -> None:
    connector = ProxmoxConnector(credentials_loader=_token_loader)
    target = _FakeTarget(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name=_TARGET_NAME,
        host=_PVE_HOST,
        port=_PVE_PORT,
        secret_ref="rdc/proxmox/token",
        auth_model="per_user",
    )
    try:
        with pytest.raises(NotImplementedError):
            await connector.auth_headers(target, _OPERATOR)
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Credential discriminator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_discriminates_token_over_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_secret(_target: Any, _operator: Operator) -> dict[str, object]:
        return {
            "token_id": _TOKEN_ID,
            "token_secret": _TOKEN_SECRET,
            "username": _TICKET_USER,
            "password": _TICKET_PASSWORD,
        }

    monkeypatch.setattr(
        "meho_backplane.connectors.proxmox.session.load_vault_secret_data", _fake_secret
    )
    creds = await load_credentials_from_vault(_fake_target(), _OPERATOR)
    assert creds.mode == "token"


@pytest.mark.asyncio
async def test_loader_falls_back_to_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_secret(_target: Any, _operator: Operator) -> dict[str, object]:
        return {"username": "alice", "password": _TICKET_PASSWORD, "realm": "ldap"}

    monkeypatch.setattr(
        "meho_backplane.connectors.proxmox.session.load_vault_secret_data", _fake_secret
    )
    creds = await load_credentials_from_vault(_fake_target(), _OPERATOR)
    assert creds.mode == "ticket"
    assert creds.username == "alice@ldap"


@pytest.mark.asyncio
async def test_loader_fails_closed_on_incomplete_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_secret(_target: Any, _operator: Operator) -> dict[str, object]:
        return {"token_id": _TOKEN_ID}  # secret missing

    monkeypatch.setattr(
        "meho_backplane.connectors.proxmox.session.load_vault_secret_data", _fake_secret
    )
    with pytest.raises(VaultCredentialsReadError):
        await load_credentials_from_vault(_fake_target(), _OPERATOR)


# ---------------------------------------------------------------------------
# Fingerprint against a recorded fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_parses_version_repoid_and_nodes() -> None:
    connector = ProxmoxConnector(credentials_loader=_token_loader)
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api2/json/version").respond(
            200, json={"data": {"version": "8.2.4", "release": "8.2", "repoid": "abc123def456"}}
        )
        mock.get("/api2/json/nodes").respond(
            200,
            json={
                "data": [
                    {"node": "pve1", "status": "online"},
                    {"node": "pve2", "status": "offline"},
                ]
            },
        )
        try:
            fp = await connector.fingerprint(_fake_target(), _OPERATOR)
        finally:
            await connector.aclose()

    assert fp.reachable is True
    assert fp.vendor == "proxmox"
    assert fp.version == "8.2.4"
    assert fp.extras["release"] == "8.2"
    assert fp.extras["repoid"] == "abc123def456"
    assert fp.extras["nodes"] == [
        {"node": "pve1", "status": "online"},
        {"node": "pve2", "status": "offline"},
    ]


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_transport_error() -> None:
    connector = ProxmoxConnector(credentials_loader=_token_loader)
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api2/json/version").mock(side_effect=httpx.ConnectError("boom"))
        try:
            fp = await connector.fingerprint(_fake_target(), _OPERATOR)
        finally:
            await connector.aclose()
    assert fp.reachable is False
    assert "error" in fp.extras


@pytest.mark.asyncio
async def test_fingerprint_degrades_on_gsm_credential_read_error() -> None:
    """A ``gsm:`` credential-read failure degrades, it does not escape (#2642)."""
    connector = ProxmoxConnector(credentials_loader=_gsm_failing_loader)
    try:
        fp = await connector.fingerprint(_fake_target(), _OPERATOR)
    finally:
        await connector.aclose()
    assert fp.reachable is False
    assert "GcpSecretManagerReadError" in fp.extras["error"]


# ---------------------------------------------------------------------------
# Dispatch-level: approval routing + approved write + task poll
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


async def _seed_target() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="proxmox",
                host=_PVE_HOST,
                port=_PVE_PORT,
                fqdn=None,
                secret_ref="rdc/proxmox/token",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "8.x"},
                preferred_impl_id="proxmox-api",
                notes="seeded by test_connectors_proxmox",
            )
        )
        await session.commit()


@pytest.fixture
async def proxmox_dispatch() -> AsyncIterator[ProxmoxConnector]:
    set_default_reducer(PassThroughReducer())
    await ProxmoxConnector.register_operations()
    await _seed_target()
    connector = ProxmoxConnector(credentials_loader=_token_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[ProxmoxConnector] = connector
    yield connector
    await connector.aclose()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool = True) -> dict[str, Any]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


@pytest.mark.asyncio
async def test_write_parks_in_approval_queue_not_hard_deny(
    proxmox_dispatch: ProxmoxConnector,
) -> None:
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        write_route = mock.post("/api2/json/nodes/pve/qemu/100/status/stop").respond(
            200, json={"data": "UPID:pve:0001:qmstop:100:root@pam:"}
        )
        result = await _dispatch(
            "proxmox.api.write",
            {"path": "nodes/pve/qemu/100/status/stop", "method": "POST"},
            approved=False,
        )
    assert result["status"] == "awaiting_approval", result
    assert not write_route.called, "a parked write must not reach the cluster"


@pytest.mark.asyncio
async def test_approved_write_returns_upid(proxmox_dispatch: ProxmoxConnector) -> None:
    upid = "UPID:pve:0001:0002:64000000:qmstop:100:root@pam:"
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/api2/json/nodes/pve/qemu/100/status/stop").respond(
            200, json={"data": upid}
        )
        result = await _dispatch(
            "proxmox.api.write",
            {"path": "nodes/pve/qemu/100/status/stop", "method": "POST"},
            approved=True,
        )
    assert route.called
    assert result["status"] == "ok", result
    assert result["result"]["upid"] == upid
    assert result["result"]["node"] == "pve"


@pytest.mark.asyncio
async def test_api_get_returns_data_payload(proxmox_dispatch: ProxmoxConnector) -> None:
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api2/json/cluster/resources").respond(
            200, json={"data": [{"type": "node", "node": "pve"}]}
        )
        result = await _dispatch("proxmox.api.get", {"path": "cluster/resources"})
    assert result["status"] == "ok", result
    assert result["result"]["data"] == [{"type": "node", "node": "pve"}]


@pytest.mark.asyncio
async def test_api_get_rejects_bad_path_at_validator(proxmox_dispatch: ProxmoxConnector) -> None:
    result = await _dispatch("proxmox.api.get", {"path": "../../etc/passwd"})
    assert result["status"] != "ok"


@pytest.mark.asyncio
async def test_task_status_polls_to_terminal(
    proxmox_dispatch: ProxmoxConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the inter-poll sleep so the two-read poll returns near-instantly.
    monkeypatch.setattr("meho_backplane.connectors.proxmox.connector._TASK_POLL_INTERVAL", 0.01)
    upid = "UPID:pve:0001:qmstop:100:root@pam:"
    path = f"/api2/json/nodes/pve/tasks/{upid}/status"
    with respx.mock(base_url=_PVE_BASE_URL, assert_all_called=False) as mock:
        mock.get(path).mock(
            side_effect=[
                httpx.Response(200, json={"data": {"upid": upid, "status": "running"}}),
                httpx.Response(
                    200,
                    json={"data": {"upid": upid, "status": "stopped", "exitstatus": "OK"}},
                ),
            ]
        )
        result = await _dispatch(
            "proxmox.task.status",
            {"node": "pve", "upid": upid, "wait": True, "poll_timeout_seconds": 30},
        )
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "stopped"
    assert result["result"]["exitstatus"] == "OK"
    assert result["result"]["timed_out"] is False
