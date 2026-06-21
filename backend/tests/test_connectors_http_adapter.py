# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for HttpConnector adapter (G0.2-T3).

Coverage matrix (per Task #242 acceptance criteria):

* ``HttpConnector`` class exists and is importable from the adapters package.
* Instantiating a subclass that omits the ABC methods raises :exc:`TypeError`;
  a direct ``HttpConnector()`` call also raises because the ABC methods are
  abstract.
* A subclass that overrides ``auth_headers`` + all three ABC methods can be
  instantiated and run an end-to-end request against a respx-mocked endpoint.
* 5xx response on GET retries exactly 3 times (4 total calls) then re-raises
  :exc:`httpx.HTTPStatusError`.
* 4xx response (e.g. 404) does NOT retry — exactly 1 call, then re-raise.
* :exc:`httpx.ConnectError` retries exactly 3 times (4 total calls).
* Per-target client pool: the same target reuses the same
  :class:`httpx.AsyncClient`; distinct targets get distinct clients.
* Cross-tenant isolation (evoila/meho#1682): two same-named targets in
  different tenants with different hosts get distinct host-bound clients,
  and a dispatch for one tenant never reaches the other tenant's host.
* ``aclose()`` closes all pooled clients and empties the pool dict.

Per-target TLS trust (evoila/meho#1774, #1781):

* ``verify_tls=True`` (the default) builds the client with **no**
  ``verify=`` argument, so httpx keeps verification on against the global
  ``SSL_CERT_FILE`` / chart trust-bundle path — byte-identical to a target
  with no TLS opt-out at all (asserted via construction-kwarg inspection
  and the live transport ``SSLContext``).
* ``verify_tls=False`` builds the client with the module-cached insecure
  ``SSLContext`` (``check_hostname is False`` **and** ``verify_mode ==
  ssl.CERT_NONE``, set in that field order).
* Flipping ``verify_tls`` yields a different pool key and a freshly built
  client — the stale client is never reused.
* The ``(tenant_id, id)`` prefix is intact after the append, so same-named
  targets in different tenants still get distinct clients.
"""

from __future__ import annotations

import datetime as _dt
import socket as _socket
import ssl
import threading as _threading
import types
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.adapters.http import HttpConnector as _HttpConnectorDirect
from meho_backplane.connectors.adapters.http import (
    _build_ca_pinned_ssl_context,
    _ca_pin_digest,
)
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator(raw_jwt: str = "") -> Operator:
    """Return a minimal :class:`Operator` carrying *raw_jwt* for the auth surface."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_target(
    name: str = "test-target",
    host: str = "vcenter.example.com",
    port: int = 443,
    *,
    target_id: str = "11111111-1111-1111-1111-111111111111",
    tenant_id: str = "00000000-0000-0000-0000-000000000000",
    verify_tls: bool = True,
    tls_ca_pin: str | None = None,
    tls_server_name: str | None = None,
) -> Any:
    """Return a minimal duck-typed Target stub.

    Carries ``id`` and ``tenant_id`` because the pooled-client cache is
    keyed on ``target_cache_key`` (``(tenant_id, id)``); a double missing
    either field raises ``AttributeError`` the moment it reaches the pool
    (evoila/meho#1682). ``verify_tls`` defaults to ``True`` — the
    default-secure model value (T1 #1780) — and ``tls_ca_pin`` to ``None``
    (no pin, T5 #1784), so the pool-key suffix and the no-``verify=``
    construction path match a verifying, unpinned target.
    ``tls_server_name`` defaults to ``None`` (#2002) — no SNI / cert-verify
    override, so the dispatch derives the verification name from ``host``.
    """
    return types.SimpleNamespace(
        name=name,
        host=host,
        port=port,
        id=target_id,
        tenant_id=tenant_id,
        auth_model="impersonation",
        verify_tls=verify_tls,
        tls_ca_pin=tls_ca_pin,
        tls_server_name=tls_server_name,
    )


def _client_ssl_context(client: httpx.AsyncClient) -> ssl.SSLContext:
    """Return the live :class:`ssl.SSLContext` httpx built for *client*.

    httpx consumes ``verify`` at construction into the transport's
    ``httpcore`` connection pool; the resolved context is reachable at
    ``client._transport._pool._ssl_context``. Reaching into the private
    attribute is intentional — it is the only way to assert what the
    transport will actually present on the wire (verification on vs off)
    rather than what kwarg was passed in.
    """
    return client._transport._pool._ssl_context  # type: ignore[attr-defined,no-any-return]


class _ConcreteHttpConnector(HttpConnector):
    """Minimal concrete subclass — overrides auth_headers + all ABC methods."""

    product = "test-http"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {"Authorization": f"Bearer {operator.raw_jwt}"}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Import / instantiation
# ---------------------------------------------------------------------------


def test_http_connector_importable_from_adapters_package() -> None:
    assert HttpConnector is _HttpConnectorDirect


def test_http_connector_abstract_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        HttpConnector()  # type: ignore[abstract]


def test_http_connector_subclass_missing_auth_headers_works() -> None:
    """HttpConnector's own methods are NOT abstract — subclass with only ABC
    methods can be instantiated; calling auth_headers raises NotImplementedError."""

    class _MissingAuth(HttpConnector):
        product = "test"

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    conn = _MissingAuth()
    assert conn is not None


def test_concrete_subclass_instantiates() -> None:
    conn = _ConcreteHttpConnector()
    assert conn.product == "test-http"
    assert conn._clients == {}


# ---------------------------------------------------------------------------
# End-to-end with respx mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_json_success() -> None:
    """Subclass end-to-end: GET /api/items returns mocked JSON via respx."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        mock.get("/api/items").respond(200, json={"items": [1, 2, 3]})
        result = await conn._get_json(target, "/api/items", operator=_make_operator("tok-abc"))

    assert result == {"items": [1, 2, 3]}
    await conn.aclose()


@pytest.mark.asyncio
async def test_auth_headers_forwarded() -> None:
    """auth_headers() result is sent as request headers."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/me").respond(200, json={"sub": "u1"})
        await conn._get_json(target, "/api/me", operator=_make_operator("my-jwt"))

    assert route.called
    sent_headers = route.calls[0].request.headers
    assert sent_headers["authorization"] == "Bearer my-jwt"
    await conn.aclose()


# ---------------------------------------------------------------------------
# tls_server_name — SNI / cert-verify host decoupled from Host (#2002)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tls_server_name_threads_sni_extension_on_get() -> None:
    """A target with ``tls_server_name`` set dispatches the SNI override.

    Acceptance criterion (#2002): the dispatched request carries
    ``extensions["sni_hostname"] == tls_server_name`` (the TLS SNI +
    cert-CN/SAN verification name) while ``url.host`` — and therefore the
    connect address and the wire ``Host:`` header — stays the routed
    ``host`` (the IP a cert-CN-pinning appliance accepts). This is the
    decoupling that lets ``verify_tls=true`` survive a deliberate
    ``Host``≠cert-CN mismatch (``httpcore`` resolves
    ``server_hostname = request.extensions["sni_hostname"]``).
    """
    conn = _ConcreteHttpConnector()
    target = _make_target(host="10.0.0.5", tls_server_name="vrli.corp.example")

    async with respx.mock(base_url="https://10.0.0.5") as mock:
        route = mock.get("/api/v2/version").respond(200, json={"version": "9.0"})
        await conn._get_json(target, "/api/v2/version", operator=_make_operator("t"))

    assert route.called
    req = route.calls[0].request
    # SNI / cert-verify name = the override.
    assert req.extensions["sni_hostname"] == "vrli.corp.example"
    # Connect address + Host header = the routed IP, NOT the SNI name.
    assert req.url.host == "10.0.0.5"
    assert req.headers["host"] == "10.0.0.5"
    await conn.aclose()


@pytest.mark.asyncio
async def test_tls_server_name_threads_sni_extension_on_post() -> None:
    """``_post_json`` threads the SNI override on non-idempotent verbs too."""
    conn = _ConcreteHttpConnector()
    target = _make_target(host="10.0.0.5", tls_server_name="vrli.corp.example")

    async with respx.mock(base_url="https://10.0.0.5") as mock:
        route = mock.post("/api/v2/sessions").respond(200, json={"ok": True})
        await conn._post_json(
            target,
            "/api/v2/sessions",
            operator=_make_operator("t"),
            json={"provider": "Local"},
        )

    assert route.called
    req = route.calls[0].request
    assert req.extensions["sni_hostname"] == "vrli.corp.example"
    assert req.url.host == "10.0.0.5"
    assert req.headers["host"] == "10.0.0.5"
    await conn.aclose()


@pytest.mark.asyncio
async def test_no_tls_server_name_omits_sni_extension() -> None:
    """Default ``tls_server_name=None`` dispatches byte-identically to today.

    No ``sni_hostname`` extension is set, so ``httpcore`` derives the SNI /
    verification name from ``base_url`` (``url.host``) exactly as before
    the override existed — the additive-default-secure contract.
    """
    conn = _ConcreteHttpConnector()
    target = _make_target(host="vcenter.example.com")  # tls_server_name=None

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/items").respond(200, json={"items": []})
        await conn._get_json(target, "/api/items", operator=_make_operator("t"))

    assert route.called
    req = route.calls[0].request
    assert "sni_hostname" not in req.extensions
    assert req.url.host == "vcenter.example.com"
    await conn.aclose()


def test_request_extensions_helper_maps_override_and_default() -> None:
    """``_request_extensions`` returns the SNI dict when set, ``{}`` otherwise."""
    conn = _ConcreteHttpConnector()
    assert conn._request_extensions(_make_target(tls_server_name="cn.example")) == {
        "sni_hostname": "cn.example"
    }
    assert conn._request_extensions(_make_target()) == {}
    # Empty string is treated as "no override" (falsy), like the API
    # boundary's nullable-string clear semantics.
    assert conn._request_extensions(_make_target(tls_server_name="")) == {}


# ---------------------------------------------------------------------------
# _post_json — verb honoring, form-encoded body, header merge (#1968)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
async def test_post_json_honors_actual_verb(verb: str) -> None:
    """_post_json sends the request with the *actual* non-idempotent verb.

    Regression for #1968: the seam previously hardcoded ``POST``, so an
    ingested ``PUT``/``PATCH``/``DELETE`` was silently downgraded.
    """
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.request(verb, "/api/widget/1").respond(200, json={"ok": True})
        result = await conn._post_json(
            target,
            "/api/widget/1",
            operator=_make_operator("tok"),
            verb=verb,
            json={"field": "v"},
        )

    assert result == {"ok": True}
    assert route.called
    assert route.calls[0].request.method == verb
    await conn.aclose()


@pytest.mark.asyncio
async def test_post_json_form_encoded_body() -> None:
    """_post_json with data= sends an application/x-www-form-urlencoded body.

    Covers the OAuth2 token-grant + vRLI/nsx session-login shapes (#1968).
    """
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.post("/oauth/token").respond(200, json={"access_token": "t"})
        result = await conn._post_json(
            target,
            "/oauth/token",
            operator=_make_operator("tok"),
            data={"grant_type": "client_credentials", "scope": "read"},
        )

    assert result == {"access_token": "t"}
    sent = route.calls[0].request
    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"
    assert sent.content == b"grant_type=client_credentials&scope=read"
    await conn.aclose()


@pytest.mark.asyncio
async def test_post_json_extra_headers_merged() -> None:
    """_post_json merges extra_headers onto auth_headers; per-call value wins."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.post("/api/widget").respond(201, json={"id": 1})
        await conn._post_json(
            target,
            "/api/widget",
            operator=_make_operator("my-jwt"),
            json={"name": "w"},
            extra_headers={"X-Idempotency-Key": "abc", "Authorization": "Bearer override"},
        )

    sent = route.calls[0].request.headers
    assert sent["x-idempotency-key"] == "abc"
    # extra_headers wins on a key clash with auth_headers.
    assert sent["authorization"] == "Bearer override"
    await conn.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["GET", "HEAD", "OPTIONS", "get"])
async def test_post_json_rejects_idempotent_verb(verb: str) -> None:
    """_post_json refuses idempotent verbs — they belong on the retried path."""
    conn = _ConcreteHttpConnector()
    target = _make_target()
    with pytest.raises(ValueError, match="non-idempotent"):
        await conn._post_json(target, "/api/x", operator=_make_operator("t"), verb=verb)
    await conn.aclose()


@pytest.mark.asyncio
async def test_post_json_rejects_json_and_data_together() -> None:
    """_post_json refuses a json= and data= body in the same call."""
    conn = _ConcreteHttpConnector()
    target = _make_target()
    with pytest.raises(ValueError, match="not both"):
        await conn._post_json(
            target,
            "/api/x",
            operator=_make_operator("t"),
            json={"a": 1},
            data={"b": 2},
        )
    await conn.aclose()


@pytest.mark.asyncio
async def test_request_json_extra_headers_merged() -> None:
    """_request_json forwards extra_headers (header-located params on a GET)."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/items").respond(200, json={"items": []})
        await conn._request_json(
            target,
            "GET",
            "/api/items",
            operator=_make_operator("my-jwt"),
            extra_headers={"X-Tenant": "acme"},
        )

    sent = route.calls[0].request.headers
    assert sent["x-tenant"] == "acme"
    assert sent["authorization"] == "Bearer my-jwt"
    await conn.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour — 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_on_get_retries_three_times_then_reraises() -> None:
    """5xx on GET triggers 3 retries (4 total calls) then re-raises."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        # Tenacity's wait_exponential would sleep for 0.5 / 1.0 / 2.0 s in
        # production. We override the wait to avoid slowing the test suite.
        route = mock.get("/api/vms").respond(503)

        from tenacity import wait_none

        with (
            patch.object(
                conn._request_json.retry,  # type: ignore[attr-defined]
                "wait",
                wait_none(),
            ),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            await conn._request_json(target, "GET", "/api/vms", operator=_make_operator("tok"))

    assert exc_info.value.response.status_code == 503
    assert route.call_count == 4  # 1 initial + 3 retries
    await conn.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour — 4xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_does_not_retry() -> None:
    """4xx response is not retried — exactly one call, then re-raise."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/missing").respond(404)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await conn._request_json(target, "GET", "/api/missing", operator=_make_operator("tok"))

    assert exc_info.value.response.status_code == 404
    assert route.call_count == 1
    await conn.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour — ConnectError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_error_retries_three_times() -> None:
    """ConnectError triggers 3 retries (4 total calls) then re-raises."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/vms").mock(side_effect=httpx.ConnectError("refused"))

        from tenacity import wait_none

        with (
            patch.object(
                conn._request_json.retry,  # type: ignore[attr-defined]
                "wait",
                wait_none(),
            ),
            pytest.raises(httpx.ConnectError),
        ):
            await conn._request_json(target, "GET", "/api/vms", operator=_make_operator("tok"))

    assert route.call_count == 4
    await conn.aclose()


# ---------------------------------------------------------------------------
# Idempotent-method guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_idempotent_method_raises_value_error() -> None:
    """_request_json raises ValueError immediately for non-idempotent verbs."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    with pytest.raises(ValueError, match="idempotent"):
        await conn._request_json(target, "POST", "/api/vms", operator=_make_operator("tok"))

    await conn.aclose()


@pytest.mark.asyncio
async def test_non_idempotent_method_lowercase_also_raises() -> None:
    """Method normalised to uppercase before guard — 'post' is rejected."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    with pytest.raises(ValueError):
        await conn._request_json(target, "post", "/api/vms", operator=_make_operator("tok"))

    await conn.aclose()


# ---------------------------------------------------------------------------
# Per-target client pooling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_target_reuses_client() -> None:
    """Two calls to the same target return the same httpx.AsyncClient instance."""
    conn = _ConcreteHttpConnector()
    target = _make_target(name="vc-01")

    client_a = await conn._http_client(target)
    client_b = await conn._http_client(target)

    assert client_a is client_b
    await conn.aclose()


@pytest.mark.asyncio
async def test_different_targets_get_different_clients() -> None:
    """Each distinct target gets its own httpx.AsyncClient."""
    conn = _ConcreteHttpConnector()
    target_a = _make_target(name="vc-01", host="vc01.example.com", target_id="aaaa")
    target_b = _make_target(name="vc-02", host="vc02.example.com", target_id="bbbb")

    client_a = await conn._http_client(target_a)
    client_b = await conn._http_client(target_b)

    assert client_a is not client_b
    # Pool is keyed on the tenant-unique ``(tenant_id, id)`` tuple plus the
    # ``verify_tls`` dimension (stubs default to verify_tls=True).
    assert conn._client_cache_key(target_a) in conn._clients
    assert conn._client_cache_key(target_b) in conn._clients
    await conn.aclose()


@pytest.mark.asyncio
async def test_same_name_different_tenant_get_distinct_host_bound_clients() -> None:
    """Cross-tenant misrouting regression (evoila/meho#1682).

    Two targets both named ``prod-vcenter`` but owned by different
    tenants and pointing at different hosts must get *distinct* pooled
    clients, and a dispatch issued for tenant B must reach B's host —
    never tenant A's host-bound client. We assert on the resolved
    ``base_url`` of each pooled client (the host the request would be
    routed to), not merely dict identity, because the bug is a wrong
    *route*, not a shared object per se.
    """
    conn = _ConcreteHttpConnector()
    target_a = _make_target(
        name="prod-vcenter",
        host="vc-tenant-a.example.com",
        target_id="a-id",
        tenant_id="tenant-a",
    )
    target_b = _make_target(
        name="prod-vcenter",
        host="vc-tenant-b.example.com",
        target_id="b-id",
        tenant_id="tenant-b",
    )

    # Tenant A dispatches first — first-writer-wins would have bound the
    # name-keyed pool to A's host.
    client_a = await conn._http_client(target_a)
    # Tenant B's dispatch against its *own* same-named target.
    client_b = await conn._http_client(target_b)

    assert client_a is not client_b
    # The load-bearing assertion: B's client routes to B's host, A's to
    # A's host. Under the old name-keyed pool, client_b would be
    # client_a and this would resolve to vc-tenant-a.example.com.
    assert str(client_a.base_url) == "https://vc-tenant-a.example.com"
    assert str(client_b.base_url) == "https://vc-tenant-b.example.com"
    # Re-fetching B's client returns B's, not A's (no first-writer bleed).
    assert await conn._http_client(target_b) is client_b
    await conn.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_all_clients_and_empties_pool() -> None:
    """aclose() calls .aclose() on every pooled client and clears the dict."""
    conn = _ConcreteHttpConnector()

    target_a = _make_target(name="t-a", host="a.example.com", target_id="id-a")
    target_b = _make_target(name="t-b", host="b.example.com", target_id="id-b")

    client_a = await conn._http_client(target_a)
    client_b = await conn._http_client(target_b)

    # Spy on aclose
    original_close_a = client_a.aclose
    original_close_b = client_b.aclose
    closed: list[str] = []

    async def _close_a() -> None:
        closed.append("t-a")
        await original_close_a()

    async def _close_b() -> None:
        closed.append("t-b")
        await original_close_b()

    client_a.aclose = _close_a  # type: ignore[method-assign]
    client_b.aclose = _close_b  # type: ignore[method-assign]

    await conn.aclose()

    assert set(closed) == {"t-a", "t-b"}
    assert conn._clients == {}


@pytest.mark.asyncio
async def test_aclose_idempotent_on_empty_pool() -> None:
    """aclose() on a fresh connector with no pooled clients is a no-op."""
    conn = _ConcreteHttpConnector()
    await conn.aclose()
    assert conn._clients == {}


# ---------------------------------------------------------------------------
# Per-target TLS trust — verify_tls wiring (evoila/meho#1774, #1781)
# ---------------------------------------------------------------------------


def test_insecure_ssl_context_field_ordering() -> None:
    """The module insecure context disables check_hostname before CERT_NONE.

    The acceptance criterion is the *field state*: ``check_hostname is
    False`` AND ``verify_mode == ssl.CERT_NONE``. That state is only
    reachable by setting ``check_hostname`` False first — assigning
    ``CERT_NONE`` while ``check_hostname`` is still enabled raises
    ``ValueError`` on Python 3.12, so a context that exhibits both is proof
    the ordering held.
    """
    from meho_backplane.connectors.adapters.http import _insecure_ssl_context

    ctx = _insecure_ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_insecure_ssl_context_is_cached_at_module_scope() -> None:
    """Repeated calls return the same shared context (built once)."""
    from meho_backplane.connectors.adapters.http import (
        _INSECURE_SSL_CONTEXT,
        _insecure_ssl_context,
    )

    assert _insecure_ssl_context() is _insecure_ssl_context()
    assert _insecure_ssl_context() is _INSECURE_SSL_CONTEXT


@pytest.mark.asyncio
async def test_verify_tls_false_client_uses_insecure_context() -> None:
    """A verify_tls=False target's client presents the insecure context.

    Asserts on the live transport ``SSLContext`` (what goes on the wire):
    ``check_hostname is False``, ``verify_mode == CERT_NONE``, and that it
    is the module-cached instance — not a per-client rebuild.
    """
    from meho_backplane.connectors.adapters.http import _INSECURE_SSL_CONTEXT

    conn = _ConcreteHttpConnector()
    target = _make_target(name="self-signed-appliance", verify_tls=False)

    client = await conn._http_client(target)
    ctx = _client_ssl_context(client)

    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx is _INSECURE_SSL_CONTEXT
    await conn.aclose()


@pytest.mark.asyncio
async def test_verify_tls_true_client_passes_no_verify_arg() -> None:
    """A verify_tls=True target builds the client with NO verify= kwarg.

    The byte-identical-to-today guarantee (evoila/meho#209): when
    verification is on we must not pass ``verify=`` at all, so httpx keeps
    its default ``verify=True`` and honours ``SSL_CERT_FILE``. We assert on
    the construction kwargs via a patched ``httpx.AsyncClient`` so the
    proof is the *absence* of the argument, not merely an equivalent
    context.
    """
    conn = _ConcreteHttpConnector()
    target = _make_target(name="verifying-target", verify_tls=True)

    with patch(
        "meho_backplane.connectors.adapters.http.httpx.AsyncClient",
        wraps=httpx.AsyncClient,
    ) as mock_client:
        await conn._http_client(target)

    assert mock_client.call_count == 1
    _, kwargs = mock_client.call_args
    assert "verify" not in kwargs
    await conn.aclose()


@pytest.mark.asyncio
async def test_verify_tls_true_client_keeps_default_verification() -> None:
    """The verify_tls=True transport context verifies (CERT_REQUIRED + SNI).

    Complements the kwarg-absence assertion with the resulting wire state:
    a default httpx client verifies (``check_hostname`` on, ``verify_mode
    == CERT_REQUIRED``) — exactly the pre-#1781 behaviour.
    """
    conn = _ConcreteHttpConnector()
    target = _make_target(name="verifying-target", verify_tls=True)

    client = await conn._http_client(target)
    ctx = _client_ssl_context(client)

    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    await conn.aclose()


@pytest.mark.asyncio
async def test_flipping_verify_tls_yields_distinct_client_not_served_stale() -> None:
    """Toggling verify_tls produces a different pool key and a fresh client.

    A PATCH that flips verify_tls must not be served the stale pooled
    client built under the previous flag (a client's ``verify`` is fixed at
    construction). Same target identity (tenant_id, id), different flag →
    distinct keys, distinct clients.
    """
    conn = _ConcreteHttpConnector()
    secure = _make_target(name="t", target_id="same-id", tenant_id="same-tenant", verify_tls=True)
    insecure = _make_target(
        name="t", target_id="same-id", tenant_id="same-tenant", verify_tls=False
    )

    client_secure = await conn._http_client(secure)
    client_insecure = await conn._http_client(insecure)

    assert client_secure is not client_insecure
    # Distinct pool keys, sharing the (tenant_id, id) prefix.
    key_secure = conn._client_cache_key(secure)
    key_insecure = conn._client_cache_key(insecure)
    assert key_secure != key_insecure
    assert key_secure[:2] == key_insecure[:2] == ("same-tenant", "same-id")
    # The insecure client really is insecure; the secure one really verifies.
    assert _client_ssl_context(client_insecure).verify_mode == ssl.CERT_NONE
    assert _client_ssl_context(client_secure).verify_mode == ssl.CERT_REQUIRED
    # Re-fetching the secure target returns the original secure client,
    # never the insecure one minted in between.
    assert await conn._http_client(secure) is client_secure
    await conn.aclose()


@pytest.mark.asyncio
async def test_same_id_different_tenant_distinct_clients_with_verify_tls() -> None:
    """The (tenant_id, id) prefix still isolates tenants after the append.

    Two targets sharing an ``id`` but owned by different tenants (and
    pointing at different hosts) get distinct host-bound clients even when
    both carry the same verify_tls value — the appended dimension never
    collapses the tenant prefix (evoila/meho#1682/#1642).
    """
    conn = _ConcreteHttpConnector()
    target_a = _make_target(
        name="prod", host="a.example.com", target_id="shared-id", tenant_id="tenant-a"
    )
    target_b = _make_target(
        name="prod", host="b.example.com", target_id="shared-id", tenant_id="tenant-b"
    )

    client_a = await conn._http_client(target_a)
    client_b = await conn._http_client(target_b)

    assert client_a is not client_b
    assert str(client_a.base_url) == "https://a.example.com"
    assert str(client_b.base_url) == "https://b.example.com"
    assert conn._client_cache_key(target_a) != conn._client_cache_key(target_b)
    await conn.aclose()


def test_extra_cache_dimensions_defaults_to_verify_tls_true_unpinned() -> None:
    """A target missing verify_tls/tls_ca_pin defaults to ``(True, "")``.

    Guards the ``getattr`` fallbacks so a duck-typed double or a
    pre-migration row defaults to verification on and no pin — never
    silently insecure, never a spurious pin. The pin slot is the empty
    string when unpinned, so the key matches the pre-#1784 shape in its
    first two extra slots plus a trailing ``""``.
    """
    conn = _ConcreteHttpConnector()
    without_attr = types.SimpleNamespace(
        name="legacy", host="h", port=443, id="i", tenant_id="t", auth_model="impersonation"
    )
    assert conn.extra_cache_dimensions(without_attr) == (True, "")
    assert conn._client_cache_key(without_attr) == ("t", "i", "True", "")


# ---------------------------------------------------------------------------
# Per-target CA-pin — tls_ca_pin secure supersession (evoila/meho#1784)
# ---------------------------------------------------------------------------
#
# The CA-pin is the *secure* path: it must trust the pinned CA while
# KEEPING CERT_REQUIRED + check_hostname (vs verify_tls=false, which drops
# both). These tests prove that property three ways: (1) the built context's
# field state, (2) a real in-memory TLS handshake that succeeds against a
# cert chaining to the pin and FAILS CLOSED against one that does not / a
# hostname mismatch, and (3) precedence + cache-key wiring.


def _gen_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Generate a self-signed CA keypair + certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "meho-test-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _gen_leaf(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    hostname: str,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Generate a leaf cert for *hostname* signed by the given CA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _tls_handshake_error(
    client_ctx: ssl.SSLContext,
    server_cert: x509.Certificate,
    server_key: rsa.RSAPrivateKey,
    server_hostname: str,
) -> ssl.SSLError | None:
    """Run one in-memory TLS handshake; return the client-side error or None.

    Drives a real client/server TLS handshake over a connected socketpair:
    the server presents *server_cert*, the client verifies using
    *client_ctx* with ``server_hostname`` as the SNI / hostname-check name.
    Returns the :exc:`ssl.SSLError` the client raised, or ``None`` when the
    handshake completed (i.e. the cert was trusted and the hostname
    matched). Files the server cert/key into a tmp via in-memory PEM so the
    stdlib server context can load them.
    """
    import tempfile

    csock, ssock = _socket.socketpair()
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False) as cf:
        cf.write(server_cert.public_bytes(serialization.Encoding.PEM))
        cf.write(_key_pem(server_key))
        chain_path = cf.name
    server_ctx.load_cert_chain(chain_path)

    client_err: list[ssl.SSLError] = []

    def _serve() -> None:
        try:
            with server_ctx.wrap_socket(ssock, server_side=True) as tls:
                tls.recv(16)
        except (ssl.SSLError, OSError):
            pass

    t = _threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        with client_ctx.wrap_socket(csock, server_hostname=server_hostname) as tls:
            tls.send(b"ok")
    except ssl.SSLError as exc:
        client_err.append(exc)
    finally:
        t.join(timeout=5)
        csock.close()
    return client_err[0] if client_err else None


def test_ca_pinned_context_keeps_cert_required_and_check_hostname() -> None:
    """The CA-pin context trusts the pin but KEEPS CERT_REQUIRED + hostname.

    This is the whole point vs verify_tls=false: load_verify_locations
    does not touch check_hostname or verify_mode, so the secure defaults
    of create_default_context survive — verification stays ON, now also
    trusting the pinned CA.
    """
    _ca_key, ca_cert = _gen_ca()
    ctx = _build_ca_pinned_ssl_context(_pem(ca_cert))

    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    # The pinned CA is actually in the context's trust store.
    loaded_subjects = {tuple(sorted(d["subject"][0])) for d in ctx.get_ca_certs()}
    assert ("commonName", "meho-test-ca") in {s for subj in loaded_subjects for s in subj}


def test_ca_pinned_context_trusts_chaining_cert() -> None:
    """A cert chaining to the pinned CA + matching hostname handshakes OK."""
    ca_key, ca_cert = _gen_ca()
    leaf_key, leaf_cert = _gen_leaf(ca_key, ca_cert, "appliance.lab.internal")
    ctx = _build_ca_pinned_ssl_context(_pem(ca_cert))

    err = _tls_handshake_error(ctx, leaf_cert, leaf_key, "appliance.lab.internal")
    assert err is None, f"expected trust, got {err!r}"


def test_ca_pinned_context_fails_closed_on_non_chaining_cert() -> None:
    """A cert NOT chaining to the pinned CA fails verification (fail closed).

    Acceptance criterion: the pin trusts exactly its CA — an unrelated
    self-signed cert (a MITM presenting any cert) is rejected, because
    CERT_REQUIRED is kept.
    """
    _ca_key, ca_cert = _gen_ca()
    ctx = _build_ca_pinned_ssl_context(_pem(ca_cert))
    # A different CA's leaf — does not chain to the pinned CA.
    other_ca_key, other_ca_cert = _gen_ca()
    rogue_key, rogue_cert = _gen_leaf(other_ca_key, other_ca_cert, "appliance.lab.internal")

    err = _tls_handshake_error(ctx, rogue_cert, rogue_key, "appliance.lab.internal")
    assert err is not None
    assert "CERTIFICATE_VERIFY_FAILED" in str(err)


def test_ca_pinned_context_fails_closed_on_hostname_mismatch() -> None:
    """A cert that chains to the pin but for the WRONG hostname fails closed.

    Acceptance criterion: check_hostname stays ON, so a cert legitimately
    signed by the pinned CA but issued for a different name is still
    rejected — the pin does not turn hostname checking off.
    """
    ca_key, ca_cert = _gen_ca()
    # Leaf is for "real.lab.internal" but we connect expecting "evil.lab.internal".
    leaf_key, leaf_cert = _gen_leaf(ca_key, ca_cert, "real.lab.internal")
    ctx = _build_ca_pinned_ssl_context(_pem(ca_cert))

    err = _tls_handshake_error(ctx, leaf_cert, leaf_key, "evil.lab.internal")
    assert err is not None
    # Hostname-mismatch surfaces as a CertificateError (a subclass of SSLError).
    assert "hostname" in str(err).lower() or "CERTIFICATE_VERIFY_FAILED" in str(err)


@pytest.mark.asyncio
async def test_ca_pin_takes_precedence_over_verify_tls_false() -> None:
    """CA-pin wins over verify_tls=false: the client verifies (secure path).

    The connector should never silently honour verify_tls=false when a pin
    is set (the API rejects that combo, but the connector defends in depth):
    a target carrying both builds the CA-pinned, verifying context — NOT
    the insecure one.
    """
    from meho_backplane.connectors.adapters.http import _INSECURE_SSL_CONTEXT

    _ca_key, ca_cert = _gen_ca()
    conn = _ConcreteHttpConnector()
    target = _make_target(name="pinned-and-insecure", verify_tls=False, tls_ca_pin=_pem(ca_cert))

    client = await conn._http_client(target)
    ctx = _client_ssl_context(client)

    assert ctx is not _INSECURE_SSL_CONTEXT
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    await conn.aclose()


@pytest.mark.asyncio
async def test_ca_pin_client_uses_verifying_context() -> None:
    """A pinned target's pooled client presents a verifying context."""
    _ca_key, ca_cert = _gen_ca()
    conn = _ConcreteHttpConnector()
    target = _make_target(name="pinned", tls_ca_pin=_pem(ca_cert))

    client = await conn._http_client(target)
    ctx = _client_ssl_context(client)

    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    await conn.aclose()


def test_ca_pin_digest_is_stable_and_distinguishes_pins() -> None:
    """The pin digest is deterministic, empty for no-pin, and pin-specific."""
    _ca_key_a, ca_a = _gen_ca()
    _ca_key_b, ca_b = _gen_ca()
    pem_a, pem_b = _pem(ca_a), _pem(ca_b)

    assert _ca_pin_digest(None) == ""
    assert _ca_pin_digest("") == ""
    assert _ca_pin_digest(pem_a) == _ca_pin_digest(pem_a)
    assert _ca_pin_digest(pem_a) != _ca_pin_digest(pem_b)


@pytest.mark.asyncio
async def test_changing_ca_pin_yields_distinct_client_not_served_stale() -> None:
    """Rotating the pin produces a different pool key and a fresh client.

    A PATCH that rotates tls_ca_pin must not be served the stale pooled
    client built against the previous pin (a client's verify context is
    fixed at construction). Same target identity, different pin → distinct
    keys, distinct clients, with the (tenant_id, id) prefix preserved.
    """
    _ka, ca_a = _gen_ca()
    _kb, ca_b = _gen_ca()
    conn = _ConcreteHttpConnector()
    pin_a = _make_target(name="t", target_id="id", tenant_id="ten", tls_ca_pin=_pem(ca_a))
    pin_b = _make_target(name="t", target_id="id", tenant_id="ten", tls_ca_pin=_pem(ca_b))

    client_a = await conn._http_client(pin_a)
    client_b = await conn._http_client(pin_b)

    assert client_a is not client_b
    key_a = conn._client_cache_key(pin_a)
    key_b = conn._client_cache_key(pin_b)
    assert key_a != key_b
    assert key_a[:2] == key_b[:2] == ("ten", "id")
    # Re-fetching the first pin returns the original client, not the second.
    assert await conn._http_client(pin_a) is client_a
    await conn.aclose()


@pytest.mark.asyncio
async def test_same_pin_different_tenant_distinct_clients() -> None:
    """The (tenant_id, id) prefix still isolates tenants with the same pin.

    Two targets sharing an id and the SAME pinned CA but owned by different
    tenants (pointing at different hosts) get distinct host-bound clients —
    the pin dimension never collapses the tenant prefix (#1682/#1642).
    """
    _ka, ca = _gen_ca()
    pin = _pem(ca)
    conn = _ConcreteHttpConnector()
    target_a = _make_target(
        name="prod", host="a.example.com", target_id="shared", tenant_id="ten-a", tls_ca_pin=pin
    )
    target_b = _make_target(
        name="prod", host="b.example.com", target_id="shared", tenant_id="ten-b", tls_ca_pin=pin
    )

    client_a = await conn._http_client(target_a)
    client_b = await conn._http_client(target_b)

    assert client_a is not client_b
    assert str(client_a.base_url) == "https://a.example.com"
    assert str(client_b.base_url) == "https://b.example.com"
    assert conn._client_cache_key(target_a) != conn._client_cache_key(target_b)
    await conn.aclose()


@pytest.mark.asyncio
async def test_unpinned_verifying_target_still_passes_no_verify_arg() -> None:
    """An unpinned verify_tls=True target still builds with NO verify= kwarg.

    The #1784 pin dimension must not regress the #209 byte-identical
    guarantee: with no pin and verification on, no verify= argument is
    passed, so httpx keeps its default and honours SSL_CERT_FILE.
    """
    conn = _ConcreteHttpConnector()
    target = _make_target(name="plain", verify_tls=True, tls_ca_pin=None)

    with patch(
        "meho_backplane.connectors.adapters.http.httpx.AsyncClient",
        wraps=httpx.AsyncClient,
    ) as mock_client:
        await conn._http_client(target)

    assert mock_client.call_count == 1
    _, kwargs = mock_client.call_args
    assert "verify" not in kwargs
    await conn.aclose()
