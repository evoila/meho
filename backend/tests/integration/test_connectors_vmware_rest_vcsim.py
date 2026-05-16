# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :class:`VmwareRestConnector` over a respx-mocked vCenter.

Exercises the live ``fingerprint`` / ``probe`` / session-cache /
``aclose`` paths of the connector against a respx-mocked modern
vCenter REST surface.

Why respx and not a real ``vmware/vcsim`` container
===================================================

This module used to boot ``vmware/vcsim`` via testcontainers. That is
**unsatisfiable for these assertions**: govmomi's vcsim does not
implement the vCenter REST *resource/appliance* API. ``GET /api/about``
(what :meth:`VmwareRestConnector.fingerprint` calls) 404s on vcsim —
it only stubs the vAPI session / tagging / content-library subset plus
the SOAP/SDK surface. The previous "``GET /api/about`` returns a
synthesised inventory shape" note was incorrect; the test had been red
on ``main`` for exactly this reason.

Per the decision recorded in evoila/meho#536 (and mirroring the
``tests/acceptance`` migration in #535), the connector is exercised
against a respx-mocked surface that reproduces the exact wire contract
``fingerprint`` / ``probe`` / session establishment / ``aclose`` rely
on. The full connector code path (session POST → cached token → ``GET
/api/about`` → ``FingerprintResult`` mapping → ``DELETE /api/session``
revoke) runs unchanged; only the transport is mocked. No Docker
dependency — respx runs in-process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import respx

from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
)

# ---------------------------------------------------------------------------
# Mocked vCenter surface
# ---------------------------------------------------------------------------

#: Base URL the target points at. Port 443 keeps
#: ``HttpConnector._base_url`` from appending ``:port`` so the respx
#: router's ``base_url`` matches the connector's client URL exactly.
#: ``.test.invalid`` (RFC 6761) guarantees no real egress.
VCENTER_BASE_URL: str = "https://vcsim-integration.test.invalid"

#: ``GET /api/about`` body. Shapes the :class:`FingerprintResult` the
#: connector builds: ``product_line_id="vpx"`` →
#: :func:`product_from_line_id` → ``"vcenter"``; the other keys flow
#: onto ``version`` / ``build`` / ``edition`` / ``extras``.
ABOUT_PAYLOAD: dict[str, Any] = {
    "product_line_id": "vpx",
    "version": "9.0.0.0",
    "build": "24021000",
    "license_product_name": "VMware vCenter Server",
    "instance_uuid": "b3f9f1a0-0000-4000-8000-0000000000ab",
    "full_name": "VMware vCenter Server 9.0.0.0 build-24021000",
    "api_type": "VirtualCenter",
    "os_type": "linux-x64",
}

#: Session token the mocked ``POST /api/session`` returns. vSphere
#: 8.0+/9.0 returns the token as a bare JSON string body; the
#: connector's ``_extract_session_token`` handles that shape.
SESSION_TOKEN: str = "integration-mock-session-token"


def _register_vcenter_routes(mock: respx.MockRouter) -> None:
    """Register the modern vCenter REST surface the connector calls.

    ``POST /api/session`` (200 → token; the modern path succeeds so the
    connector records ``/api/session`` as the established path),
    ``GET /api/about`` (the fingerprint probe), and ``DELETE
    /api/session`` (the ``aclose`` revoke against the established
    path).
    """
    mock.post("/api/session").respond(200, json=SESSION_TOKEN)
    mock.get("/api/about").respond(200, json=ABOUT_PAYLOAD)
    mock.delete("/api/session").respond(204)


# ---------------------------------------------------------------------------
# Target stub
# ---------------------------------------------------------------------------


@dataclass
class _VcsimTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


@pytest.fixture
def vcsim_target() -> _VcsimTarget:
    """Target pointing at the respx-mocked vCenter base URL."""
    return _VcsimTarget(
        name="vcsim-test",
        host=VCENTER_BASE_URL.removeprefix("https://"),
        port=443,
        secret_ref="kv/data/vsphere/vcsim-test",
    )


@pytest.fixture
async def vcsim_connector(
    vcsim_target: _VcsimTarget,
) -> AsyncIterator[tuple[VmwareRestConnector, _VcsimTarget]]:
    """Yield a connector wired against the respx-mocked vCenter surface.

    Only the Vault-backed session loader is replaced (the acceptance
    suite has no Vault); the connector's real ``_http_client`` is left
    intact — respx intercepts httpx at the transport layer, so the
    production pooling + redirect code stays on the exercised path.
    The router stays active across teardown so ``aclose``'s ``DELETE
    /api/session`` is intercepted.
    """

    async def _loader(_target: VsphereTargetLike) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vcenter_routes(mock)
        try:
            yield connector, vcsim_target
        finally:
            await connector.aclose()


# ---------------------------------------------------------------------------
# Tests — assertions unchanged from the vcsim-container era; only the
# transport moved (vcsim → respx) because vcsim cannot serve this API.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_against_vcsim_returns_reachable(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """fingerprint() returns reachable=True with the vmware vendor + mapped product."""
    connector, target = vcsim_connector
    result = await connector.fingerprint(target)
    assert result.vendor == "vmware"
    assert result.reachable is True, f"fingerprint not reachable: extras={dict(result.extras)}"
    assert result.probe_method == "GET /api/about"
    # product_line_id="vpx" maps through product_from_line_id -> "vcenter".
    assert result.product == "vcenter"


@pytest.mark.asyncio
async def test_probe_against_vcsim_returns_ok(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """probe() returns ok=True (delegates to fingerprint)."""
    connector, target = vcsim_connector
    result = await connector.probe(target)
    assert result.ok is True, f"probe failed: reason={result.reason!r}"


@pytest.mark.asyncio
async def test_session_reused_across_consecutive_fingerprint_calls(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """Two consecutive fingerprint calls share the same cached session token."""
    connector, target = vcsim_connector
    await connector.fingerprint(target)
    token_after_first = connector._session_tokens.get(target.name)
    assert token_after_first is not None
    await connector.fingerprint(target)
    token_after_second = connector._session_tokens.get(target.name)
    # Load-bearing: the cached token is byte-identical across calls
    # (no re-establish).
    assert token_after_first == token_after_second


@pytest.mark.asyncio
async def test_aclose_revokes_session_against_vcsim(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """aclose() issues DELETE /api/session and clears the token + client caches."""
    connector, target = vcsim_connector

    await connector.fingerprint(target)
    assert target.name in connector._session_tokens

    await connector.aclose()
    # Post-aclose: token cache + client pool both emptied. (The fixture
    # teardown calls aclose() again — idempotent no-op on empty state.)
    assert connector._session_tokens == {}
    assert connector._clients == {}
