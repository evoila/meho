# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ESXi-native VI-JSON session branch (#3363).

#3332 shipped standalone-ESXi host resolution (``ha-host``), the VI-JSON
seam, and ``product=esxi`` fingerprint stamping, but left session
establishment on the two vSphere-Automation vAPI endpoints
(``POST /api/session`` + the ``/rest/com/vmware/cis/session`` 404-fallback)
that exist on vCenter only. On a standalone ESXi host ``/api/*`` is a
JSON-RPC 2.0 handler: a bodyless ``POST /api/session`` answers HTTP 400
(not 404), so the modern→legacy fallback dead-ends and no
``vmware-api-session-id`` token is ever obtained. #3363 adds an ESXi-native
branch that mints the session over VI-JSON ``SessionManager.Login`` on the
``ha-sessionmgr`` singleton.

Coverage matrix (per #3363 acceptance criteria):

* First probe, before any fingerprint exists — the JSON-RPC-400 signature
  on ``POST /api/session`` selects the ESXi branch; ``SessionManager.Login``
  is POSTed at ``/sdk/vim25/{release}/SessionManager/ha-sessionmgr/Login``
  with the ``{"userName","password","locale"}`` body (no HTTP Basic), and
  the ``vmware-api-session-id`` **response header** propagates into
  ``auth_headers()``.
* A target already fingerprinted ``product=esxi`` goes straight to the
  VI-JSON branch — no ``POST /api/session`` at all.
* Session reuse — one Login for two ``auth_headers`` calls.
* ``fingerprint`` against a standalone ESXi target → ``reachable=True``,
  ``product="esxi"``, a version from the vim25 service-versions document.
* ``_post_vmomi_json`` (the seam every host read/write rides) resolves the
  VI-JSON ``/sdk/vim25/{release}`` mount + attaches the session header
  against an ESXi target.
* ``Logout`` on ``ha-sessionmgr`` on ``invalidate_session`` and ``aclose``.
* 401 recovery — ``invalidate_session`` → cold VI-JSON re-login.
* vCenter regression — a genuine (non-JSON-RPC) 400 never takes the ESXi
  branch; a real vCenter still mints via ``POST /api/session``.
* Credentials never appear in the Login error message.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.settings import get_settings

_ESXI_HOST = "esxi-standalone.test.invalid"
_ESXI_BASE = f"https://{_ESXI_HOST}"
_RELEASE = "9.1.0.0"
_LOGIN_PATH = f"/sdk/vim25/{_RELEASE}/SessionManager/ha-sessionmgr/Login"
_LOGOUT_PATH = f"/sdk/vim25/{_RELEASE}/SessionManager/ha-sessionmgr/Logout"

#: The version-discovery document both vCenter and ESXi serve
#: unauthenticated at /sdk/vimServiceVersions.xml (urn:vim25 9.1.0.0 — the
#: standalone-ESXi 9.1 shape from the field diagnosis).
_SERVICE_VERSIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<namespaces version="1.0">
 <namespace>
  <name>urn:vim25</name>
  <version>9.1.0.0</version>
  <priorVersions>
   <version>8.0.3.0</version>
  </priorVersions>
 </namespace>
</namespaces>
"""

#: ESXi's JSON-RPC 2.0 answer to a bodyless POST /api/session (Basic auth),
#: verbatim from the field diagnosis — HTTP 400, not 404.
_JSONRPC_400_BODY: dict[str, Any] = {
    "jsonrpc": "2.0",
    "error": {"code": 400, "message": "Unsupported content type: "},
}


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``Settings`` reads at construction time.

    The injected stub loader never reaches Vault, but keep the env pinned
    the same way the sibling auth-test module does so nothing incidental
    trips on an unset ``KEYCLOAK_*`` / ``VAULT_*``.
    """
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


@dataclass
class _StubTarget:
    """Satisfies ``VsphereTargetLike`` structurally; carries an optional fingerprint.

    ``fingerprint=None`` (unprobed) classifies as vCenter — the first-probe
    case; ``{"product": "esxi", "reachable": True, ...}`` classifies as a
    standalone ESXi target (``classify_host_target``, #3332).
    """

    name: str = "esxi-standalone"
    host: str = _ESXI_HOST
    port: int | None = 443
    secret_ref: str = "vsphere/esxi-standalone"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _esxi_fingerprinted() -> _StubTarget:
    return _StubTarget(fingerprint={"product": "esxi", "reachable": True, "version": "9.1"})


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Replace ``aclose`` with a revoke-free pool tear-down.

    Tests that don't exercise the revoke leg would otherwise trip respx's
    assert-all-mocked on the shutdown Logout/DELETE. Clears the #3363
    ``_session_flavors`` map alongside the pre-existing caches.
    """

    async def _aclose() -> None:
        connector._session_tokens.clear()
        connector._session_paths.clear()
        connector._session_flavors.clear()
        connector._session_extensions.clear()
        connector._about_versions.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# First probe — JSON-RPC-400 signature selects the ESXi branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_probe_jsonrpc_400_selects_esxi_login_branch() -> None:
    """A bodyless POST /api/session 400 (JSON-RPC) → VI-JSON Login; header propagates."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    token = "esxi-session-token-abc"

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        session_route = mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        sv_route = mock.get("/sdk/vimServiceVersions.xml").respond(
            200, text=_SERVICE_VERSIONS_XML, headers={"content-type": "text/xml"}
        )
        login_route = mock.post(_LOGIN_PATH).respond(
            200, json={"key": "session-key"}, headers={"vmware-api-session-id": token}
        )
        headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers == {"vmware-api-session-id": token}
    # The vAPI probe fired once (its 400 is what selected the branch),
    # the version doc was read for {release}, and Login minted the token.
    assert session_route.call_count == 1
    assert sv_route.called
    assert login_route.call_count == 1


@pytest.mark.asyncio
async def test_esxi_login_request_shape_body_and_no_basic_auth() -> None:
    """Login POSTs {"userName","password","locale"} on ha-sessionmgr, no HTTP Basic."""
    import json as _json

    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        login_route = mock.post(_LOGIN_PATH).respond(
            200, json={}, headers={"vmware-api-session-id": "t"}
        )
        await connector.auth_headers(_StubTarget(), _make_operator())

    req = login_route.calls[0].request
    body = _json.loads(req.content)
    assert body == {"userName": "svc-meho", "password": "stub-password", "locale": "en_US"}
    # Login carries credentials in the JSON body — never as HTTP Basic.
    assert req.headers.get("authorization") is None


@pytest.mark.asyncio
async def test_fingerprinted_esxi_skips_api_session_entirely() -> None:
    """A target already fingerprinted product=esxi goes straight to VI-JSON Login."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE, assert_all_called=False) as mock:
        session_route = mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        login_route = mock.post(_LOGIN_PATH).respond(
            200, json={}, headers={"vmware-api-session-id": "t"}
        )
        headers = await connector.auth_headers(_esxi_fingerprinted(), _make_operator())

    assert headers == {"vmware-api-session-id": "t"}
    assert login_route.call_count == 1
    # The fingerprint already said ESXi, so the vAPI session path is never hit.
    assert session_route.call_count == 0


@pytest.mark.asyncio
async def test_esxi_session_reused_across_auth_calls() -> None:
    """Two auth_headers calls against one ESXi target → exactly one Login."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        login_route = mock.post(_LOGIN_PATH).respond(
            200, json={}, headers={"vmware-api-session-id": "reused"}
        )
        h1 = await connector.auth_headers(target, _make_operator())
        h2 = await connector.auth_headers(target, _make_operator())

    assert h1 == h2 == {"vmware-api-session-id": "reused"}
    assert login_route.call_count == 1


# ---------------------------------------------------------------------------
# Failure shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esxi_login_missing_session_header_raises() -> None:
    """A 200 Login with no vmware-api-session-id header is an establish failure."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={"key": "no-header"})
        with pytest.raises(RuntimeError, match="without a vmware-api-session-id"):
            await connector.auth_headers(_esxi_fingerprinted(), _make_operator())


@pytest.mark.asyncio
async def test_esxi_login_401_error_message_hides_credentials() -> None:
    """A 401 at Login names only the target + status — never the password."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(401, json={})
        # 401 at Login → the structured ConnectorAuthError (auth-class),
        # the same shape the vAPI path raises; assert the message, not type.
        with pytest.raises((RuntimeError, ConnectorAuthError)) as excinfo:
            await connector.auth_headers(_esxi_fingerprinted(), _make_operator())

    message = str(excinfo.value)
    assert "stub-password" not in message
    assert "svc-meho" not in message


@pytest.mark.asyncio
async def test_esxi_login_unresolvable_release_raises() -> None:
    """No usable vim25 release from the discovery doc → clean establish failure."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        # 200 but no parsable urn:vim25 version → service_versions returns None.
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text="<namespaces/>")
        with pytest.raises(RuntimeError, match="could not read the vim25 release"):
            await connector.auth_headers(_esxi_fingerprinted(), _make_operator())


# ---------------------------------------------------------------------------
# vCenter regression — a genuine 400 must NOT take the ESXi branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_jsonrpc_400_does_not_select_esxi_branch() -> None:
    """A vCenter 400 without the JSON-RPC signature stays on the vAPI path (RuntimeError)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(400, text="<html>Bad Request</html>")
        login_route = mock.post(_LOGIN_PATH).respond(
            200, json={}, headers={"vmware-api-session-id": "t"}
        )
        with pytest.raises(RuntimeError, match="POST /api/session returned HTTP 400"):
            await connector.auth_headers(_StubTarget(), _make_operator())

    # The ESXi login branch was never taken for a non-JSON-RPC 400.
    assert login_route.call_count == 0


# ---------------------------------------------------------------------------
# fingerprint() over the ESXi session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_standalone_esxi_reachable_product_esxi() -> None:
    """probe/fingerprint against a standalone ESXi 9.1 host → reachable=True, product=esxi."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={}, headers={"vmware-api-session-id": "t"})
        # ESXi's JSON-RPC handler answers GET /api/about with HTTP 400 (empty).
        mock.get("/api/about").respond(400)
        result = await connector.fingerprint(_StubTarget(), _make_operator())

    assert result.reachable is True
    assert result.vendor == "vmware"
    assert result.product == "esxi"
    assert result.version == _RELEASE
    assert result.extras["session_flavor"] == "esxi"


@pytest.mark.asyncio
async def test_probe_standalone_esxi_ok_true() -> None:
    """probe() folds the reachable ESXi fingerprint into ok=True."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={}, headers={"vmware-api-session-id": "t"})
        mock.get("/api/about").respond(400)
        probe = await connector.probe(_esxi_fingerprinted())

    assert probe.ok is True


# ---------------------------------------------------------------------------
# _post_vmomi_json rides the VI-JSON /sdk/vim25/{release} mount on ESXi
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_vmomi_json_uses_vijson_mount_and_session_header_on_esxi() -> None:
    """A host read on an ESXi target POSTs /sdk/vim25/{release}/... with the session header."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()
    vmomi_path = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    vijson_url = f"/sdk/vim25/{_RELEASE}{vmomi_path}"

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={}, headers={"vmware-api-session-id": "vtok"})
        read_route = mock.post(vijson_url).respond(200, json={"returnval": {"objects": []}})
        result = await connector._post_vmomi_json(
            target, vmomi_path, operator=_make_operator(), json={"specSet": []}
        )

    assert result == {"returnval": {"objects": []}}
    assert read_route.call_count == 1
    assert read_route.calls[0].request.headers.get("vmware-api-session-id") == "vtok"


# ---------------------------------------------------------------------------
# Teardown + recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_session_logs_out_esxi_and_drops_cache() -> None:
    """invalidate_session issues Logout on ha-sessionmgr for an ESXi session."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={}, headers={"vmware-api-session-id": "tok"})
        logout_route = mock.post(_LOGOUT_PATH).respond(204)
        await connector.auth_headers(target, _make_operator())
        await connector.invalidate_session(target)

    assert logout_route.call_count == 1
    assert logout_route.calls[0].request.headers.get("vmware-api-session-id") == "tok"
    # The cache slot is cleared so the next auth re-establishes.
    assert target_cache_key(target) not in connector._session_tokens
    assert target_cache_key(target) not in connector._session_flavors


@pytest.mark.asyncio
async def test_401_recovery_cold_re_logs_in() -> None:
    """invalidate_session → the next auth_headers cold-re-runs VI-JSON Login."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        login_route = mock.post(_LOGIN_PATH).respond(
            200, json={}, headers={"vmware-api-session-id": "tok"}
        )
        mock.post(_LOGOUT_PATH).respond(204)
        await connector.auth_headers(target, _make_operator())  # login #1
        await connector.invalidate_session(target)  # 401-recovery hook
        await connector.auth_headers(target, _make_operator())  # login #2 (cold)

    assert login_route.call_count == 2


@pytest.mark.asyncio
async def test_invalidate_logout_failure_is_swallowed() -> None:
    """A failing Logout on invalidate never blocks the cold re-establish."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={}, headers={"vmware-api-session-id": "tok"})
        mock.post(_LOGOUT_PATH).mock(side_effect=httpx.ConnectError("down"))
        await connector.auth_headers(target, _make_operator())
        # Must not raise even though Logout errors.
        await connector.invalidate_session(target)

    assert target_cache_key(target) not in connector._session_tokens


@pytest.mark.asyncio
async def test_aclose_logs_out_esxi_session() -> None:
    """aclose tears an ESXi session down with Logout, not DELETE /api/session."""
    connector = _make_connector()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE, assert_all_called=False) as mock:
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.post(_LOGIN_PATH).respond(200, json={}, headers={"vmware-api-session-id": "tok"})
        api_delete = mock.delete("/api/session").respond(204)
        logout_route = mock.post(_LOGOUT_PATH).respond(204)
        await connector.auth_headers(target, _make_operator())
        await connector.aclose()

    assert logout_route.call_count == 1
    assert logout_route.calls[0].request.headers.get("vmware-api-session-id") == "tok"
    # The vAPI DELETE is never used for an ESXi-flavored session.
    assert api_delete.call_count == 0
