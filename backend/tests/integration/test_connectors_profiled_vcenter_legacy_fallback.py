# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test: profiled vCenter modern→legacy session fallback (#2031).

Exercises the full ``ProfiledRestConnector`` ``session_login_basic`` path
against a respx-mocked **legacy-only** vCenter REST surface — the shape the
upstream ``vmware/vcsim`` simulator registers (it serves only
``POST /rest/com/vmware/cis/session``, not the modern ``POST /api/session``).

Why respx and not a real ``vmware/vcsim`` container
===================================================

The typed-connector sibling test
(``tests/integration/test_connectors_vmware_rest_vcsim.py``) records the
decision (evoila/meho#536): vcsim does **not** implement the vCenter REST
resource/appliance API — ``GET /api/about`` 404s on it — so the connector
is exercised against a respx surface reproducing the exact wire contract
session establishment + op dispatch rely on. The same posture is mirrored
here: respx serves a legacy-only surface (modern ``/api/session`` 404s,
legacy ``/rest/com/vmware/cis/session`` mints a token), and the real
``_http_client`` / session harness / ``mount_op_path`` code paths run
unchanged — only the transport is mocked. No Docker dependency.

The assertions pin the three #2031 acceptance criteria end-to-end:

* the modern ``/api/session`` 404 triggers exactly one legacy retry, and the
  legacy token is applied verbatim in the ``vmware-api-session-id`` header;
* the winning legacy endpoint drives op-path mount (``/rest`` not ``/api``)
  and is recorded for teardown;
* a 401 on modern is NOT retried on legacy (auth failure, not "endpoint
  absent").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.schemas import AuthModel

#: Base URL the target points at. Port 443 keeps ``HttpConnector._base_url``
#: from appending ``:port`` so the respx router's ``base_url`` matches the
#: connector's client URL exactly. ``.test.invalid`` (RFC 6761) guarantees
#: no real egress.
VCSIM_BASE_URL = "https://profiled-vcsim.test.invalid"

#: The legacy ``/rest`` session-establish path vcsim registers.
LEGACY_SESSION_PATH = "/rest/com/vmware/cis/session"
MODERN_SESSION_PATH = "/api/session"

#: Session token the mocked legacy ``POST`` returns (raw JSON-string body,
#: the modern + legacy vCenter shape the connector coerces to the token).
SESSION_TOKEN = "profiled-legacy-session-token"


@dataclass
class _VcsimTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    # Tenant-unique cache key components (#1642/#1672); without them
    # ``target_cache_key`` raises AttributeError at runtime.
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator(raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _vmware_profile() -> ExecutionProfile:
    """The shipped vmware profile shape — session_login_basic, /api/about."""
    return ExecutionProfile(
        product="vmware",
        version="9.0",
        auth=AuthSpec(scheme="session_login_basic", secret_fields=("username", "password")),
        fingerprint=FingerprintSpec(
            path="/api/about", version_key="version", version_splitter="none"
        ),
        probe="delegate",
        pagination=PaginationSpec(strategy="none", items_key="value"),
    )


def _connector() -> ProfiledRestConnector:
    async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
        return {"username": "svc", "password": "pw"}

    return ProfiledRestConnector(profile=_vmware_profile(), credentials_loader=_loader)


@pytest.fixture
def vcsim_target() -> _VcsimTarget:
    return _VcsimTarget(
        name="profiled-vcsim",
        host=VCSIM_BASE_URL.removeprefix("https://"),
        port=443,
        secret_ref="vsphere/profiled-vcsim",
    )


@pytest.mark.asyncio
async def test_profiled_vcenter_falls_back_to_legacy_session_end_to_end(
    vcsim_target: _VcsimTarget,
) -> None:
    """A legacy-only vCenter authenticates via the legacy path + raw token header."""
    connector = _connector()

    async with respx.mock(base_url=VCSIM_BASE_URL, assert_all_called=False) as mock:
        modern = mock.post(MODERN_SESSION_PATH).respond(404)
        legacy = mock.post(LEGACY_SESSION_PATH).respond(200, json=SESSION_TOKEN)

        headers = await connector.auth_headers(vcsim_target, operator=_operator())

    # Modern attempted once, legacy minted the token.
    assert modern.call_count == 1
    assert legacy.call_count == 1
    # Raw token (no Bearer wrap) in vCenter's bespoke session header.
    assert headers == {"vmware-api-session-id": SESSION_TOKEN}
    # The legacy login carried HTTP Basic creds + empty body.
    assert legacy.calls[0].request.read() == b""
    assert legacy.calls[0].request.headers.get("authorization", "").startswith("Basic ")
    # The winning legacy endpoint is recorded for mount + teardown.
    assert connector._session_login_paths[target_cache_key(vcsim_target)] == LEGACY_SESSION_PATH
    await connector.aclose()


@pytest.mark.asyncio
async def test_profiled_vcenter_legacy_op_path_mounts_under_rest(
    vcsim_target: _VcsimTarget,
) -> None:
    """An ingested op against the legacy-only target mounts under ``/rest``."""
    connector = _connector()

    async with respx.mock(base_url=VCSIM_BASE_URL, assert_all_called=False) as mock:
        mock.post(MODERN_SESSION_PATH).respond(404)
        mock.post(LEGACY_SESSION_PATH).respond(200, json=SESSION_TOKEN)

        # The spec-relative descriptor path the G0.7 pipeline stores.
        mounted = await connector.mount_op_path(vcsim_target, "/vcenter/vm", operator=_operator())

    # Legacy won → /rest mount, not the /api default that would 404 every op.
    assert mounted == "/rest/vcenter/vm"
    await connector.aclose()


@pytest.mark.asyncio
async def test_profiled_vcenter_session_reused_across_calls(
    vcsim_target: _VcsimTarget,
) -> None:
    """Two consecutive auth calls share one legacy login (single-flight cache)."""
    connector = _connector()

    async with respx.mock(base_url=VCSIM_BASE_URL, assert_all_called=False) as mock:
        mock.post(MODERN_SESSION_PATH).respond(404)
        legacy = mock.post(LEGACY_SESSION_PATH).respond(200, json=SESSION_TOKEN)

        h1 = await connector.auth_headers(vcsim_target, operator=_operator())
        h2 = await connector.auth_headers(vcsim_target, operator=_operator())

    assert h1 == h2 == {"vmware-api-session-id": SESSION_TOKEN}
    # The cached session means exactly one legacy login round-trip.
    assert legacy.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_profiled_vcenter_401_on_modern_not_retried_on_legacy(
    vcsim_target: _VcsimTarget,
) -> None:
    """A 401 on modern is an auth failure — the legacy path is NOT tried.

    #2414: a login-POST auth-class rejection surfaces as the structured
    :class:`ConnectorAuthError` (establish stage) rather than the raw
    ``httpx.HTTPStatusError``, so the dispatcher stamps ``session_establish_401``
    (restage) instead of the retry arm's ``after_relogin``. The chained
    ``__cause__`` preserves the underlying transport error.
    """
    connector = _connector()

    async with respx.mock(base_url=VCSIM_BASE_URL, assert_all_called=False) as mock:
        modern = mock.post(MODERN_SESSION_PATH).respond(401)
        legacy = mock.post(LEGACY_SESSION_PATH).respond(200, json=SESSION_TOKEN)

        with pytest.raises(ConnectorAuthError) as exc_info:
            await connector.auth_headers(vcsim_target, operator=_operator())
        assert exc_info.value.cause == "session_establish_401"
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)

    assert modern.call_count == 1
    assert legacy.call_count == 0
    await connector.aclose()
