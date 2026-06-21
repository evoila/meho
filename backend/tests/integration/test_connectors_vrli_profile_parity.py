# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-method dispatch parity: typed vRLI connector vs the ExecutionProfile.

G0.28-T8 (#1974) — the **capstone** integration proof for Initiative #1965.
It exercises the whole profiled-connector chain (T1-T7) against the first
real, shipped, bespoke connector and asserts that a profiled vRLI connector
driven solely by the reviewed
:data:`~meho_backplane.connectors.vcf_logs.profile.VRLI_EXECUTION_PROFILE`
reaches **per-method parity** with the hand-coded
:class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector` it
was migrated from:

* ``auth_headers`` — both produce ``Authorization: Bearer <sessionId>`` from
  the same ``POST /api/v2/sessions`` login round-trip (the ``session_login``
  named scheme, #1970), and both reuse the cached token across calls.
* ``fingerprint`` — both read the unauthenticated ``GET /api/v2/version``
  and render the 5-part vRLI version string identically into
  ``(version, build)`` via the ``vrli_five_part`` named splitter (#1972).
* ``probe`` — both delegate to ``fingerprint`` and report the same ``ok``.
* session-expiry recovery — both treat ``{401, 440}`` (the profile's
  ``expiry_statuses``, #1973) as "re-login once and retry"; the parity test
  drives a 440 through the typed connector's retry seam and asserts the
  profiled connector classifies the same status set.

Lane placement
==============

This lives in ``tests/integration/`` so it runs in the **required**
``Python (integration testcontainers)`` merge gate (#698) — the lane the
issue's acceptance criterion names. vRLI is a proprietary VMware appliance
with no public container image (the existing ``test_connectors_vcf_logs_e2e``
acceptance suite already mocks it rather than booting a container), so the
"appliance" here is a single ``respx`` mock router that **both** connectors
talk to over identical routes — the cleanest way to assert two
implementations agree on the wire interactions without a real vRLI.

tenant_id-double trap (memory: integration doubles)
===================================================

The session-token cache is keyed on the tenant-unique ``(tenant_id, id)``
tuple (#1642), so the stub target carries **both** ``id`` and ``tenant_id``
attributes; a double missing either would collapse two targets onto one
cache entry (or crash :func:`target_cache_key`). The stub is shared by both
connectors so the parity comparison is apples-to-apples.

What is deliberately NOT asserted equal
=======================================

The typed connector keeps two enrichments the declarative profile cannot
model — the per-target ``provider`` (the ``session_login`` scheme hardcodes
``"Local"``) and the fingerprint ``extras`` (``release_name`` /
``version_full`` / ``patch``). Those are tested as typed-only in
``tests/test_connectors_vcf_logs_auth.py``; here the parity contract is the
**dispatch-relevant** surface (the Bearer header, the version/build, the
reachability, the expiry set), which is exactly what a profiled connector
must reproduce to be a drop-in dispatch sibling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_logs import VRLI_EXECUTION_PROFILE, VcfLogsConnector

_BASE_URL = "https://vrli-parity.test.invalid"
_HOST = "vrli-parity.test.invalid"
_SESSION_ID = "parity-session-token-abc"
_SESSION_REFRESH_ID = "parity-session-token-refreshed"
_VERSION_FULL = "9.0.0.0.21761695"
_EXPECTED_VERSION = "9.0.0"
_EXPECTED_BUILD = "21761695"


@dataclass
class _StubTarget:
    """A vRLI target double satisfying both connectors' target contract.

    Carries the tenant-unique ``(tenant_id, id)`` pair the session-token
    cache keys on (#1642) — the tenant_id-double trap. ``provider`` is left
    unset so the typed connector uses its ``"Local"`` default, matching the
    profile's hardcoded provider for an apples-to-apples auth comparison.
    """

    name: str = "vrli-parity"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vrli/parity"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    provider: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator(raw_jwt: str = "op.parity.jwt") -> Operator:
    return Operator(
        sub="parity-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "parity-svc", "password": "parity-pw"}


def _typed_connector() -> VcfLogsConnector:
    return VcfLogsConnector(credentials_loader=_stub_loader)


class _ProfiledVrli(ProfiledRestConnector):
    """A stamped profiled vRLI connector — the shape the T5 stamping path registers.

    Carries the ingested triple as class attributes (``product`` /
    ``version`` / ``impl_id``) exactly as a profile-stamped subclass would
    in production, so the inherited ``fingerprint`` (which reads
    ``self.product``) resolves. The profile is attached per-instance below
    to match the test-construction pattern in ``test_connectors_profiled_auth``.
    """

    product = "vrli"
    version = "9.0"
    impl_id = "vrli-rest"
    supported_version_range = ">=9.0,<10.0"


def _profiled_connector() -> ProfiledRestConnector:
    """A profiled vRLI connector driven solely by the reviewed profile."""
    return _ProfiledVrli(
        profile=VRLI_EXECUTION_PROFILE,
        credentials_loader=_stub_loader,
    )


# ---------------------------------------------------------------------------
# The profile is the migrated declaration — pin its shape
# ---------------------------------------------------------------------------


def test_profile_declares_the_migrated_vrli_surface() -> None:
    """The vrli_session profile carries exactly what was retired from the class."""
    profile = VRLI_EXECUTION_PROFILE
    assert profile.product == "vrli"
    assert profile.auth.scheme == "session_login"
    assert profile.auth.secret_fields == ("username", "password")
    # The unauthenticated version endpoint + the 5-part splitter (#1972).
    assert profile.fingerprint.path == "/api/v2/version"
    assert profile.fingerprint.authenticated is False
    assert profile.fingerprint.version_key == "version"
    assert profile.fingerprint.version_splitter == "vrli_five_part"
    # vRLI's session-expiry status set (#1973): 401 floor + vRLI's 440.
    assert profile.expiry_statuses == frozenset({401, 440})


def test_typed_connector_derives_its_literals_from_the_profile() -> None:
    """The typed class no longer hand-codes the session path / version path / expiry set."""
    from meho_backplane.connectors.vcf_logs import connector as vrli_connector

    assert vrli_connector._SESSION_CREATE_PATH == "/api/v2/sessions"
    assert VRLI_EXECUTION_PROFILE.fingerprint.path == vrli_connector._VERSION_PATH
    assert vrli_connector._SESSION_EXPIRED_STATUSES is VRLI_EXECUTION_PROFILE.expiry_statuses


# ---------------------------------------------------------------------------
# auth_headers parity
# ---------------------------------------------------------------------------


@respx.mock
async def test_auth_headers_parity_bearer_from_same_login() -> None:
    """Typed + profiled connectors both POST login creds and return the same Bearer."""
    route = respx.post(f"{_BASE_URL}/api/v2/sessions").mock(
        return_value=httpx.Response(200, json={"sessionId": _SESSION_ID, "ttl": 1800})
    )

    typed = _typed_connector()
    profiled = _profiled_connector()
    target = _StubTarget()

    typed_headers = await typed.auth_headers(target, operator=_operator())
    profiled_headers = await profiled.auth_headers(target, operator=_operator())

    expected = {"Authorization": f"Bearer {_SESSION_ID}"}
    assert typed_headers == expected
    assert profiled_headers == expected
    assert typed_headers == profiled_headers

    # Each connector logged in exactly once (two logins total across both).
    assert route.call_count == 2
    # Both sent a JSON login body with the session_login scheme's fields.
    for call in route.calls:
        assert call.request.headers.get("content-type", "").startswith("application/json")
        assert "authorization" not in {k.lower() for k in call.request.headers}

    await typed.aclose()
    await profiled.aclose()


@respx.mock
async def test_auth_headers_parity_caches_token_across_calls() -> None:
    """Both connectors reuse the cached session token — one login per connector."""
    route = respx.post(f"{_BASE_URL}/api/v2/sessions").mock(
        return_value=httpx.Response(200, json={"sessionId": _SESSION_ID, "ttl": 1800})
    )
    target = _StubTarget()

    for connector in (_typed_connector(), _profiled_connector()):
        h1 = await connector.auth_headers(target, operator=_operator())
        h2 = await connector.auth_headers(target, operator=_operator())
        assert h1 == h2 == {"Authorization": f"Bearer {_SESSION_ID}"}
        await connector.aclose()

    # Two connectors, one login each (cache hit on the second call).
    assert route.call_count == 2


@respx.mock
async def test_auth_headers_parity_empty_jwt_fails_closed_on_both() -> None:
    """The empty-raw_jwt fail-closed invariant holds identically on both connectors."""
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

    respx.post(f"{_BASE_URL}/api/v2/sessions").mock(
        return_value=httpx.Response(200, json={"sessionId": _SESSION_ID, "ttl": 1800})
    )
    target = _StubTarget()

    typed = _typed_connector()
    profiled = _profiled_connector()
    with pytest.raises(VaultCredentialsReadError):
        await typed.auth_headers(target, operator=_operator(raw_jwt=""))
    with pytest.raises(VaultCredentialsReadError):
        await profiled.auth_headers(target, operator=_operator(raw_jwt=""))

    await typed.aclose()
    await profiled.aclose()


# ---------------------------------------------------------------------------
# fingerprint / probe parity
# ---------------------------------------------------------------------------


@respx.mock
async def test_fingerprint_parity_version_build_split() -> None:
    """Typed + profiled fingerprints render the same (version, build) from one response."""
    respx.get(f"{_BASE_URL}/api/v2/version").mock(
        return_value=httpx.Response(
            200,
            json={"version": _VERSION_FULL, "releaseName": "VMware Aria Operations for Logs 9.0"},
        )
    )

    typed = _typed_connector()
    profiled = _profiled_connector()
    target = _StubTarget()

    typed_fp = await typed.fingerprint(target)
    profiled_fp = await profiled.fingerprint(target)

    # The dispatch-relevant fields match exactly.
    assert typed_fp.reachable is True and profiled_fp.reachable is True
    assert typed_fp.version == _EXPECTED_VERSION == profiled_fp.version
    assert typed_fp.build == _EXPECTED_BUILD == profiled_fp.build
    assert typed_fp.product == "vrli" == profiled_fp.product
    assert typed_fp.probe_method == "GET /api/v2/version" == profiled_fp.probe_method

    await typed.aclose()
    await profiled.aclose()


@respx.mock
async def test_fingerprint_parity_unreachable_on_status_error() -> None:
    """A 500 on the version endpoint yields reachable=False on both connectors."""
    respx.get(f"{_BASE_URL}/api/v2/version").mock(return_value=httpx.Response(500))

    typed = _typed_connector()
    profiled = _profiled_connector()
    target = _StubTarget()

    typed_fp = await typed.fingerprint(target)
    profiled_fp = await profiled.fingerprint(target)

    assert typed_fp.reachable is False and profiled_fp.reachable is False
    assert "error" in typed_fp.extras and "error" in profiled_fp.extras

    await typed.aclose()
    await profiled.aclose()


@respx.mock
async def test_probe_parity_reachable() -> None:
    """probe() delegates to fingerprint on both connectors and reports the same ok."""
    respx.get(f"{_BASE_URL}/api/v2/version").mock(
        return_value=httpx.Response(200, json={"version": _VERSION_FULL, "releaseName": "Logs"})
    )

    typed = _typed_connector()
    profiled = _profiled_connector()
    target = _StubTarget()

    assert (await typed.probe(target)).ok is True
    assert (await profiled.probe(target)).ok is True

    await typed.aclose()
    await profiled.aclose()


# ---------------------------------------------------------------------------
# session-expiry ({401, 440}) parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expiry_status", [401, 440])
@respx.mock
async def test_session_expiry_recovery_parity(expiry_status: int) -> None:
    """Both 401 and 440 drive the typed connector's invalidate -> re-login -> retry-once.

    The status set under test IS the profile's ``expiry_statuses`` — the
    single declaration the typed connector's retry seam now narrows. The
    typed connector owns the downstream-retry seam
    (``_get_json_with_session_retry``); the profiled connector classifies
    the same set, asserted below.
    """
    session_route = respx.post(f"{_BASE_URL}/api/v2/sessions")
    session_route.side_effect = [
        httpx.Response(200, json={"sessionId": _SESSION_ID, "ttl": 1800}),
        httpx.Response(200, json={"sessionId": _SESSION_REFRESH_ID, "ttl": 1800}),
    ]
    events_route = respx.get(f"{_BASE_URL}/api/v2/events")
    events_route.side_effect = [
        httpx.Response(expiry_status),
        httpx.Response(200, json={"events": [{"id": "ev-1"}]}),
    ]

    typed = _typed_connector()
    target = _StubTarget()
    result = await typed._get_json_with_session_retry(
        target, "/api/v2/events", operator=_operator()
    )

    assert result == {"events": [{"id": "ev-1"}]}
    assert session_route.call_count == 2  # initial + post-expiry re-login
    assert events_route.call_count == 2  # expiry + retry
    assert typed._session_tokens[target_cache_key(target)] == _SESSION_REFRESH_ID

    await typed.aclose()


def test_expiry_status_set_is_the_single_profile_source() -> None:
    """The retry-seam status set and the profile's expiry_statuses are one object."""
    from meho_backplane.connectors.vcf_logs import connector as vrli_connector

    # Same frozenset instance — no second hand-coded copy that could drift.
    assert vrli_connector._SESSION_EXPIRED_STATUSES is VRLI_EXECUTION_PROFILE.expiry_statuses
    assert 401 in VRLI_EXECUTION_PROFILE.expiry_statuses
    assert 440 in VRLI_EXECUTION_PROFILE.expiry_statuses
