# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VmwareRestConnector` session-auth + lifecycle (G3.1-T1 #498).

The integration shape (real vcsim) lives in
:mod:`tests.integration.test_connectors_vmware_rest_vcsim`. This module
exercises the same contract with respx-mocked HTTP so the gate runs in
every CI lane regardless of Docker availability.

Coverage matrix (per #498 acceptance criteria):

* Session establishment success — ``POST /api/session`` returns a
  JSON-string token (vSphere 7.0+ shape); the token is cached under
  ``target.name`` and returned in the
  ``vmware-api-session-id`` header on subsequent calls.
* Session reuse — a second auth call against the same target does NOT
  re-issue ``POST /api/session``.
* Session-creation failure — 401 from ``/api/session`` surfaces as a
  ``RuntimeError`` whose message names the target.
* Per-target isolation — two distinct ``target.name`` values get two
  distinct cached tokens; cross-target token leakage would be a tenant-
  isolation bug.
* :meth:`aclose` revokes every cached session via ``DELETE /api/session``
  before invoking :meth:`HttpConnector.aclose`.
* :meth:`auth_headers` raises :exc:`NotImplementedError` when
  ``target.auth_model`` is ``"per_user"`` or ``"impersonation"``.
* Loader-returning-incomplete-dict surfaces a clear error.
* Legacy ``{"value": "<token>"}`` response shape is accepted defensively
  for cross-vcsim-version compatibility.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
import respx

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
)


@pytest.fixture(autouse=True)
def _clean_vmware_rest_registry() -> Iterator[None]:
    """Re-register VmwareRestConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` (and other earlier-alphabetised
    test modules) install autouse fixtures that call
    :func:`clear_registry` between tests. The connector class
    self-registered at import time, but the post-clear empty state
    breaks the registration-acceptance test below. Re-register before
    every test in this module and clear after — same pattern
    :mod:`tests.test_connectors_vault` established.
    """
    clear_registry()
    register_connector_v2(
        product=VmwareRestConnector.product,
        version=VmwareRestConnector.version,
        impl_id=VmwareRestConnector.impl_id,
        cls=VmwareRestConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies VsphereTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


_TARGET_A = _StubTarget(
    name="vcenter-a",
    host="vcenter-a.test.invalid",
    port=443,
    secret_ref="kv/data/vsphere/vcenter-a",
)
_TARGET_B = _StubTarget(
    name="vcenter-b",
    host="vcenter-b.test.invalid",
    port=443,
    secret_ref="kv/data/vsphere/vcenter-b",
)


async def _stub_loader(_target: VsphereTargetLike) -> dict[str, str]:
    """Return canned credentials regardless of the target."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    """Build a connector wired with the stub loader."""
    return VmwareRestConnector(session_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vmware_rest_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector."""
    assert issubclass(VmwareRestConnector, HttpConnector)
    assert VmwareRestConnector.product == "vmware"
    assert VmwareRestConnector.version == "9.0"
    assert VmwareRestConnector.impl_id == "vmware-rest"
    assert VmwareRestConnector.supported_version_range == ">=8.5,<10.0"
    # Outranks GenericRestConnector auto-shim (priority=0) defensively
    # if both somehow register for the same triple.
    assert VmwareRestConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vmware", "9.0", "vmware-rest")
    assert key in registry
    assert registry[key] is VmwareRestConnector


def test_default_session_loader_raises_until_g03_lands() -> None:
    """The default Vault loader stays unimplemented until G0.3."""
    import asyncio

    from meho_backplane.connectors.vmware_rest.session import (
        load_session_credentials_from_vault,
    )

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"G0\.3"):
            await load_session_credentials_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Session establishment — happy path
# ---------------------------------------------------------------------------


# The connector's aclose() issues DELETE /api/session for every cached
# target; tests that don't mock that endpoint would tip respx into an
# AssertionError on the unhandled request. ``_patch_no_revoke_aclose``
# replaces the cleanup with a direct httpx pool tear-down for tests
# that don't care about the revoke leg — kept here at module scope so
# every test that follows can call it after building its connector.
def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Replace connector.aclose with a revoke-free pool tear-down."""

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_auth_headers_establishes_session_and_returns_header() -> None:
    """First auth_headers call POSTs to /api/session and caches the token."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    token = "abc-session-token-123"

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        session_route = mock.post("/api/session").respond(200, json=token)
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert session_route.called
    assert session_route.call_count == 1
    assert headers == {"vmware-api-session-id": token}
    # Basic auth from the stub loader: svc-meho / stub-password.
    sent_auth = session_route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None
    assert sent_auth.startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_session_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-POST /api/session."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    token = "cached-token-456"

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        session_route = mock.post("/api/session").respond(200, json=token)
        h1 = await connector.auth_headers(_TARGET_A, raw_jwt="")
        h2 = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert h1 == h2 == {"vmware-api-session-id": token}
    # The load-bearing assertion — exactly one POST /api/session for two
    # auth header calls.
    assert session_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_session_tokens_separate() -> None:
    """Two targets get two distinct cached tokens; no cross-target leakage."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock() as mock:
        route_a = mock.post("https://vcenter-a.test.invalid/api/session").respond(
            200, json="token-for-a"
        )
        route_b = mock.post("https://vcenter-b.test.invalid/api/session").respond(
            200, json="token-for-b"
        )

        h_a = await connector.auth_headers(_TARGET_A, raw_jwt="")
        h_b = await connector.auth_headers(_TARGET_B, raw_jwt="")

    assert route_a.called and route_b.called
    assert h_a == {"vmware-api-session-id": "token-for-a"}
    assert h_b == {"vmware-api-session-id": "token-for-b"}
    # Both tokens cached.
    assert connector._session_tokens == {
        "vcenter-a": "token-for-a",
        "vcenter-b": "token-for-b",
    }
    await connector.aclose()


# ---------------------------------------------------------------------------
# Session establishment — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_create_401_surfaces_runtime_error_with_target_name() -> None:
    """401 from POST /api/session raises RuntimeError naming the target."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(401, json={"error": "invalid_credentials"})
        with pytest.raises(RuntimeError, match=r"vcenter-a") as exc_info:
            await connector.auth_headers(_TARGET_A, raw_jwt="")

    # The wrapped HTTPStatusError carries the 401 details.
    assert "401" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_response_unexpected_shape_raises() -> None:
    """A response that isn't a JSON string or {"value": str} raises."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        # POST /api/session returns something the connector can't parse —
        # an empty object is the most common vcsim misbehaviour shape.
        mock.post("/api/session").respond(200, json={"unrelated": "field"})
        with pytest.raises(RuntimeError, match=r"vcenter-a"):
            await connector.auth_headers(_TARGET_A, raw_jwt="")

    await connector.aclose()


@pytest.mark.asyncio
async def test_session_legacy_object_shape_is_accepted_defensively() -> None:
    """Pre-7.0 ``{"value": "<token>"}`` response is still parsed correctly.

    Some vcsim builds straddle the modern-string and legacy-object
    shapes; the connector accepts both so the integration test stays
    green across simulator versions.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(200, json={"value": "legacy-shape-token"})
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert headers == {"vmware-api-session-id": "legacy-shape-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_clear_error() -> None:
    """A loader that returns a dict without 'password' surfaces a clear message."""

    async def _bad_loader(_target: VsphereTargetLike) -> dict[str, str]:
        # Intentionally missing 'password'; a real production loader bug.
        return {"username": "svc-meho"}  # type: ignore[return-value]

    connector = VmwareRestConnector(session_loader=_bad_loader)
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"password"):
            await connector.auth_headers(_TARGET_A, raw_jwt="")
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="vcenter-per-user",
        host="vc.test.invalid",
        port=443,
        secret_ref="kv/data/vsphere/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, raw_jwt="")

    assert "vcenter-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted as SHARED_SERVICE_ACCOUNT."""
    target = _StubTarget(
        name="vcenter-pre-g03",
        host="vc.test.invalid",
        port=443,
        secret_ref="kv/data/vsphere/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vc.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="pre-g03-token")
        headers = await connector.auth_headers(target, raw_jwt="")

    assert headers == {"vmware-api-session-id": "pre-g03-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_value_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="vcenter-enum",
        host="vc.test.invalid",
        port=443,
        secret_ref="kv/data/vsphere/enum",
    )
    # Use the enum member directly rather than its .value
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vc.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="enum-token")
        headers = await connector.auth_headers(target, raw_jwt="")

    assert headers == {"vmware-api-session-id": "enum-token"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose — session revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_revokes_every_cached_session() -> None:
    """aclose() issues DELETE /api/session for each cached token before pool tear-down."""
    connector = _make_connector()

    async with respx.mock() as mock:
        mock.post("https://vcenter-a.test.invalid/api/session").respond(200, json="token-for-a")
        mock.post("https://vcenter-b.test.invalid/api/session").respond(200, json="token-for-b")
        delete_a = mock.delete("https://vcenter-a.test.invalid/api/session").respond(204)
        delete_b = mock.delete("https://vcenter-b.test.invalid/api/session").respond(204)

        await connector.auth_headers(_TARGET_A, raw_jwt="")
        await connector.auth_headers(_TARGET_B, raw_jwt="")
        await connector.aclose()

    assert delete_a.called and delete_b.called
    # Both DELETE requests carried the session token in the header.
    deleted_headers_a = delete_a.calls[0].request.headers
    assert deleted_headers_a.get("vmware-api-session-id") == "token-for-a"
    deleted_headers_b = delete_b.calls[0].request.headers
    assert deleted_headers_b.get("vmware-api-session-id") == "token-for-b"
    # Pool cleared.
    assert connector._clients == {}
    assert connector._session_tokens == {}


@pytest.mark.asyncio
async def test_aclose_continues_on_revoke_failure() -> None:
    """A 5xx or transport error during DELETE /api/session is logged, not raised."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="token-for-a")
        # The revoke leg fails with a transport error — connector swallows
        # and proceeds (lifespan shutdown must not block).
        mock.delete("/api/session").mock(side_effect=httpx.ConnectError("revoke failed"))
        await connector.auth_headers(_TARGET_A, raw_jwt="")
        # The load-bearing invariant: aclose() returns cleanly.
        await connector.aclose()

    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_continues_on_revoke_4xx() -> None:
    """A 401/403 from DELETE /api/session is logged but doesn't block tear-down."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="token-for-a")
        # vCenter's DELETE /api/session may 401 if the session expired
        # between the last call and shutdown; tear-down still proceeds.
        mock.delete("/api/session").respond(401)
        await connector.auth_headers(_TARGET_A, raw_jwt="")
        await connector.aclose()

    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_sessions_is_a_noop() -> None:
    """A fresh connector with no sessions established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._session_tokens == {}
