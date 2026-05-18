# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`NsxConnector` session-auth + fingerprint/probe (G3.5-T1 #613).

Exercises the form-encoded session-create flow, the X-XSRF-TOKEN +
JSESSIONID-cookie pairing, the 401 -> re-login + retry-once recovery,
per-target isolation, and the auth_model boundary gate. The
contract mirrors :mod:`tests.test_connectors_vmware_rest_auth` with NSX
auth divergence: form body (not HTTP Basic), Set-Cookie + X-XSRF-TOKEN
response (not JSON-string token), and the connector-level 401 retry
(vSphere defers 401 recovery; NSX does it once per call).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.nsx import (
    NsxConnector,
    NsxTargetLike,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel


@pytest.fixture(autouse=True)
def _clean_nsx_registry() -> Iterator[None]:
    """Re-register NsxConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. The connector class
    self-registered at import time, but the post-clear empty state
    breaks the registration-acceptance test below. Re-register before
    every test in this module and clear after -- same pattern
    :mod:`tests.test_connectors_vmware_rest_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=NsxConnector.product,
        version=NsxConnector.version,
        impl_id=NsxConnector.impl_id,
        cls=NsxConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub -- satisfies NsxTargetLike Protocol structurally.
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
    name="nsx-a",
    host="nsx-a.test.invalid",
    port=443,
    secret_ref="kv/data/nsx/nsx-a",
)
_TARGET_B = _StubTarget(
    name="nsx-b",
    host="nsx-b.test.invalid",
    port=443,
    secret_ref="kv/data/nsx/nsx-b",
)


async def _stub_loader(_target: NsxTargetLike) -> dict[str, str]:
    """Return canned credentials regardless of the target."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> NsxConnector:
    """Build a connector wired with the stub loader."""
    return NsxConnector(session_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_nsx_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(NsxConnector, HttpConnector)
    assert NsxConnector.product == "nsx"
    assert NsxConnector.version == "4.2"
    assert NsxConnector.impl_id == "nsx-rest"
    assert NsxConnector.supported_version_range == ">=4.0,<5.0"
    # Outranks a future GenericRestConnector auto-shim defensively.
    assert NsxConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("nsx", "4.2", "nsx-rest")
    assert key in registry
    assert registry[key] is NsxConnector


def test_default_session_loader_raises_until_g03_lands() -> None:
    """The default Vault loader stays unimplemented until G0.3."""
    import asyncio

    from meho_backplane.connectors.nsx.session import (
        load_session_credentials_from_vault,
    )

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"G0\.3"):
            await load_session_credentials_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Session establishment -- happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_establishes_session_with_form_encoded_body() -> None:
    """First auth_headers call POSTs form-encoded creds to /api/session/create.

    Asserts the load-bearing auth divergence from vSphere:
    ``Content-Type: application/x-www-form-urlencoded`` body with
    ``j_username`` + ``j_password`` keys (NOT JSON, NOT HTTP Basic).
    """
    connector = _make_connector()
    xsrf_token = "xsrf-abc-123"

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        session_route = mock.post("/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": xsrf_token, "Set-Cookie": "JSESSIONID=jsess-1; Path=/"},
        )
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert session_route.called and session_route.call_count == 1
    assert headers == {"X-XSRF-TOKEN": xsrf_token}

    # Form-encoded body assertion -- decode the URL-encoded bytes back
    # to a dict so a key-order swap in form encoding doesn't flake the
    # test. The connector must send j_username + j_password verbatim.
    request = session_route.calls[0].request
    assert request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
    sent = dict(parse_qsl(request.content.decode()))
    assert sent == {"j_username": "svc-meho", "j_password": "stub-password"}
    # NSX rejects HTTP Basic on the canonical FQDN -- the form-encoded
    # flow must NOT smuggle an Authorization: Basic header alongside.
    assert "authorization" not in {k.lower() for k in request.headers}

    # The JSESSIONID cookie landed in the per-target client jar
    # automatically (httpx.AsyncClient.cookies.extract_cookies).
    client = await connector._http_client(_TARGET_A)
    assert client.cookies.get("JSESSIONID") == "jsess-1"

    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_session_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-POST /api/session/create."""
    connector = _make_connector()
    xsrf_token = "xsrf-cached-456"

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        session_route = mock.post("/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": xsrf_token, "Set-Cookie": "JSESSIONID=jsess-1; Path=/"},
        )
        h1 = await connector.auth_headers(_TARGET_A, raw_jwt="")
        h2 = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert h1 == h2 == {"X-XSRF-TOKEN": xsrf_token}
    # The load-bearing assertion -- exactly one POST /api/session/create
    # for two auth header calls.
    assert session_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_session_tokens_separate() -> None:
    """Two targets get two distinct cached tokens; no cross-target leakage."""
    connector = _make_connector()

    async with respx.mock() as mock:
        route_a = mock.post("https://nsx-a.test.invalid/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": "xsrf-a", "Set-Cookie": "JSESSIONID=jsess-a"},
        )
        route_b = mock.post("https://nsx-b.test.invalid/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": "xsrf-b", "Set-Cookie": "JSESSIONID=jsess-b"},
        )

        h_a = await connector.auth_headers(_TARGET_A, raw_jwt="")
        h_b = await connector.auth_headers(_TARGET_B, raw_jwt="")

    assert route_a.called and route_b.called
    assert h_a == {"X-XSRF-TOKEN": "xsrf-a"}
    assert h_b == {"X-XSRF-TOKEN": "xsrf-b"}
    assert connector._session_tokens == {
        "nsx-a": "xsrf-a",
        "nsx-b": "xsrf-b",
    }
    # Cookie jars are also isolated -- one client per target.
    client_a = await connector._http_client(_TARGET_A)
    client_b = await connector._http_client(_TARGET_B)
    assert client_a.cookies.get("JSESSIONID") == "jsess-a"
    assert client_b.cookies.get("JSESSIONID") == "jsess-b"

    await connector.aclose()


# ---------------------------------------------------------------------------
# Session establishment -- failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_create_401_surfaces_runtime_error_naming_target() -> None:
    """401 from POST /api/session/create raises RuntimeError naming the target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        mock.post("/api/session/create").respond(401, json={"error": "invalid_credentials"})
        with pytest.raises(RuntimeError, match=r"nsx-a") as exc_info:
            await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert "401" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_create_missing_xsrf_header_raises() -> None:
    """A 2xx response without X-XSRF-TOKEN raises naming the target.

    A misbehaving proxy or a wrong endpoint can 200 with no XSRF header;
    the connector fails loudly rather than caching an empty token that
    would silently 401 every subsequent call.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        # Set-Cookie present but no X-XSRF-TOKEN -- failure case.
        mock.post("/api/session/create").respond(200, headers={"Set-Cookie": "JSESSIONID=jsess-1"})
        with pytest.raises(RuntimeError, match=r"nsx-a"):
            await connector.auth_headers(_TARGET_A, raw_jwt="")

    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_clear_error() -> None:
    """A loader returning a dict without 'password' surfaces a clear message."""

    async def _bad_loader(_target: NsxTargetLike) -> dict[str, str]:
        return {"username": "svc-meho"}  # type: ignore[return-value]

    connector = NsxConnector(session_loader=_bad_loader)

    async with respx.mock(base_url="https://nsx-a.test.invalid"):
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
        name="nsx-per-user",
        host="nsx.test.invalid",
        port=443,
        secret_ref="kv/data/nsx/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, raw_jwt="")

    assert "nsx-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="nsx-pre-g03",
        host="nsx.test.invalid",
        port=443,
        secret_ref="kv/data/nsx/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx.test.invalid") as mock:
        mock.post("/api/session/create").respond(200, headers={"X-XSRF-TOKEN": "pre-g03-token"})
        headers = await connector.auth_headers(target, raw_jwt="")

    assert headers == {"X-XSRF-TOKEN": "pre-g03-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_value_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="nsx-enum",
        host="nsx.test.invalid",
        port=443,
        secret_ref="kv/data/nsx/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx.test.invalid") as mock:
        mock.post("/api/session/create").respond(200, headers={"X-XSRF-TOKEN": "enum-token"})
        headers = await connector.auth_headers(target, raw_jwt="")

    assert headers == {"X-XSRF-TOKEN": "enum-token"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# 401 -> re-login -> retry-once recovery (downstream call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downstream_401_triggers_relogin_and_retry_once() -> None:
    """A 401 on a downstream GET triggers session invalidation + re-login + a single retry.

    Exercises the issue's "on HTTP 401 from a downstream call, re-login
    once then retry once" contract. The session-create route is called
    twice (initial + post-401 re-login); the downstream route is
    called twice (401 then 200).
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        session_route = mock.post("/api/session/create")
        session_route.side_effect = [
            httpx.Response(
                200, headers={"X-XSRF-TOKEN": "xsrf-first", "Set-Cookie": "JSESSIONID=js1"}
            ),
            httpx.Response(
                200,
                headers={"X-XSRF-TOKEN": "xsrf-second", "Set-Cookie": "JSESSIONID=js2"},
            ),
        ]
        node_route = mock.get("/api/v1/node")
        node_route.side_effect = [
            httpx.Response(401),
            httpx.Response(
                200,
                json={
                    "node_version": "4.2.0.0.0",
                    "kernel_version": "5.10.0-nsx",
                    "node_uuid": "uuid-1",
                    "hostname": "nsx-a.test.invalid",
                },
            ),
        ]

        result = await connector._get_json_with_session_retry(_TARGET_A, "/api/v1/node", raw_jwt="")

    assert result["node_version"] == "4.2.0.0.0"
    # Re-login fired exactly once -- two POSTs total.
    assert session_route.call_count == 2
    # Downstream GET fired twice -- the original 401 + the post-relogin retry.
    assert node_route.call_count == 2
    # The post-relogin XSRF replaced the stale one.
    assert connector._session_tokens == {"nsx-a": "xsrf-second"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_downstream_401_then_still_401_after_relogin_raises_runtime_error() -> None:
    """If the post-relogin retry also 401s, RuntimeError naming the target is raised.

    Single retry, not a loop -- a configured credential pair that
    consistently 401s should fail fast rather than hammering NSX's
    audit log.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        session_route = mock.post("/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": "xsrf-any", "Set-Cookie": "JSESSIONID=js"},
        )
        node_route = mock.get("/api/v1/node").respond(401)

        with pytest.raises(RuntimeError, match=r"nsx-a") as exc_info:
            await connector._get_json_with_session_retry(_TARGET_A, "/api/v1/node", raw_jwt="")

    assert "401" in str(exc_info.value)
    assert "after refresh" in str(exc_info.value)
    # Exactly one re-login attempt + two GETs (no further retries).
    assert session_route.call_count == 2
    assert node_route.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_downstream_non_401_status_error_propagates_untouched() -> None:
    """A 500 (or any non-401 status) on a downstream call does NOT trigger relogin.

    Re-establishing the session on a 5xx would mask transient backend
    failures behind a credential-rotation that solves nothing; the
    relogin branch is keyed strictly on 401.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        session_route = mock.post("/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": "xsrf-any", "Set-Cookie": "JSESSIONID=js"},
        )
        # 502 is the gateway-error shape behind the VCF 9 envoy proxy;
        # tenacity's @retry on _request_json will retry it up to its
        # budget before raising. Asserting "exactly one POST" guarantees
        # we never triggered a relogin.
        mock.get("/api/v1/node").respond(502)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector._get_json_with_session_retry(_TARGET_A, "/api/v1/node", raw_jwt="")

    assert exc_info.value.response.status_code == 502
    # One session-create call (initial); no re-login fired.
    assert session_route.call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() + probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against a respx-mocked GET /api/v1/node returns the canonical shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        mock.post("/api/session/create").respond(
            200,
            headers={"X-XSRF-TOKEN": "xsrf-1", "Set-Cookie": "JSESSIONID=js"},
        )
        mock.get("/api/v1/node").respond(
            200,
            json={
                "node_version": "4.2.0.0.0.21761695",
                "kernel_version": "5.10.0-nsx",
                "node_uuid": "abc-uuid-1",
                "hostname": "nsx-a.test.invalid",
                "external_id": "ext-1",
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "nsx"
    assert fp.version == "4.2.0.0.0.21761695"
    assert fp.build == "5.10.0-nsx"
    assert fp.reachable is True
    assert fp.probe_method == "GET /api/v1/node"
    assert fp.extras["node_uuid"] == "abc-uuid-1"
    assert fp.extras["hostname"] == "nsx-a.test.invalid"
    assert fp.extras["external_id"] == "ext-1"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_error() -> None:
    """Transport/status/session failure returns reachable=False with structured extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        # Session-create itself fails 401 -- the RuntimeError from
        # ``_session_token`` must surface as a clean reachable=False
        # rather than propagating up.
        mock.post("/api/session/create").respond(401, json={"error": "invalid_credentials"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "nsx"
    assert fp.reachable is False
    assert fp.probe_method == "GET /api/v1/node"
    # Structured error: ``"<ExcType>: <message>"``.
    error = fp.extras["error"]
    assert "RuntimeError" in error
    assert "nsx-a" in error
    assert "401" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_reachable() -> None:
    """probe() returns ok=True on a reachable target (delegates to fingerprint)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        mock.post("/api/session/create").respond(
            200, headers={"X-XSRF-TOKEN": "xsrf", "Set-Cookie": "JSESSIONID=js"}
        )
        mock.get("/api/v1/node").respond(
            200,
            json={
                "node_version": "4.2.0",
                "kernel_version": "5.10.0",
                "node_uuid": "u",
                "hostname": "nsx-a.test.invalid",
            },
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_when_unreachable() -> None:
    """probe() returns ok=False + reason on an unreachable target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        mock.post("/api/session/create").respond(401, json={"error": "bad_creds"})
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "401" in result.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose -- token cache clear + pool tear-down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_session_token_cache_and_pool() -> None:
    """aclose() clears in-memory session caches and tears down the httpx pool."""
    connector = _make_connector()

    async with respx.mock(base_url="https://nsx-a.test.invalid") as mock:
        mock.post("/api/session/create").respond(
            200, headers={"X-XSRF-TOKEN": "xsrf", "Set-Cookie": "JSESSIONID=js"}
        )
        await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert connector._session_tokens == {"nsx-a": "xsrf"}
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_sessions_is_a_noop() -> None:
    """A fresh connector with no sessions established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._session_tokens == {}
