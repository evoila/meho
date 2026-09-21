# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the vSphere Supervisor (WCP) SSO auth mode (#2905).

Covers the three layers the WCP mode adds to the ``k8s-1.x`` connector:

* :mod:`meho_backplane.connectors.kubernetes.wcp` — the ``/wcp/login``
  SSO exchange, JWT-``exp``-aware token expiry, the TLS bootstrap, and
  the self-refreshing :class:`Configuration` (mint -> cache -> refresh).
* :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubernetes_credential`
  — the payload-shape discriminator (kubeconfig vs SSO ``{username,
  password}``).
* :class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
  — routing a WCP credential to the self-refreshing client and dialing
  the reachable alias, not the Supervisor's internal VIP.

``kubernetes_asyncio`` / ``httpx`` are mocked (``respx``) so the gate
runs in every CI lane regardless of a live Supervisor.
"""

from __future__ import annotations

import base64
import datetime
import json
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.kubernetes import wcp
from meho_backplane.connectors.kubernetes.connector import _DEFAULT_K8S_PORT, KubernetesConnector
from meho_backplane.connectors.kubernetes.kubeconfig import (
    KubeconfigCredential,
    KubernetesTargetLike,
    WcpSsoCredential,
    load_kubernetes_credential,
)
from meho_backplane.connectors.kubernetes.wcp import (
    DEFAULT_WCP_TOKEN_TTL_SECONDS,
    WcpLoginError,
    WcpToken,
    build_wcp_api_configuration,
    wcp_login,
)
from meho_backplane.settings import get_settings

_WCP_MODULE = "meho_backplane.connectors.kubernetes.wcp"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires (mirrors the k8s auth suite)."""
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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    """Structural :class:`KubernetesTargetLike` + the TLS knobs the WCP
    path reads via ``getattr``."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    # Tenant-unique cache key components (#1642, security F04).
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_WCP_TARGET = _StubTarget(
    name="wcp-supervisor",
    host="supervisor.alias.test",
    port=6443,
    secret_ref="k8s/wcp-supervisor",
)


def _make_operator(*, raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=__import__("uuid").UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _kube_config(
    *, server: str = "https://10.99.99.99:6443", insecure: bool = True, ca_data: str | None = None
) -> dict[str, Any]:
    """A minimal but valid kubeconfig dict for ``load_kube_config_from_dict``.

    ``server`` defaults to a raw internal-VIP address so the host-override
    assertion (dial the reachable alias, not the VIP) is meaningful.
    """
    cluster: dict[str, Any] = {"server": server}
    if ca_data is not None:
        cluster["certificate-authority-data"] = ca_data
    elif insecure:
        cluster["insecure-skip-tls-verify"] = True
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "sup", "cluster": cluster}],
        "users": [{"name": "u", "user": {"token": "kubeconfig-embedded-token"}}],
        "contexts": [{"name": "ctx", "context": {"cluster": "sup", "user": "u"}}],
        "current-context": "ctx",
    }


def _make_jwt(*, exp: float) -> str:
    """A structurally-valid JWT carrying only an ``exp`` claim (unsigned)."""

    def _seg(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{_seg({'alg': 'none'})}.{_seg({'exp': exp})}.sig"


# ---------------------------------------------------------------------------
# Token expiry — JWT exp vs fallback TTL
# ---------------------------------------------------------------------------


def test_jwt_exp_reads_exp_claim() -> None:
    assert wcp._jwt_exp(_make_jwt(exp=1_900_000_000.0)) == 1_900_000_000.0


@pytest.mark.parametrize(
    "token",
    [
        "opaque-not-a-jwt",
        "only.two",
        "a.b.c.d",
        f"{base64.urlsafe_b64encode(b'{bad json').decode().rstrip('=')}.x.y",
    ],
)
def test_jwt_exp_returns_none_for_non_jwt(token: str) -> None:
    assert wcp._jwt_exp(token) is None


def test_jwt_exp_rejects_bool_exp() -> None:
    # ``True`` is an int subclass — must not be read as a 1-second expiry.
    seg = base64.urlsafe_b64encode(json.dumps({"exp": True}).encode()).decode().rstrip("=")
    assert wcp._jwt_exp(f"h.{seg}.s") is None


def test_token_expiry_prefers_jwt_exp() -> None:
    token = _make_jwt(exp=1000.0)
    got = wcp._token_expiry_monotonic(token, now_wall=400.0, now_monotonic=50.0)
    # remaining wall seconds (600) projected onto the monotonic clock.
    assert got == pytest.approx(650.0)


def test_token_expiry_falls_back_to_default_ttl_for_opaque_token() -> None:
    got = wcp._token_expiry_monotonic("opaque", now_wall=400.0, now_monotonic=50.0)
    assert got == pytest.approx(50.0 + DEFAULT_WCP_TOKEN_TTL_SECONDS)


def test_token_expiry_falls_back_when_jwt_already_expired() -> None:
    # exp in the past -> remaining <= 0 -> fallback rather than an
    # immediately-stale stamp.
    token = _make_jwt(exp=100.0)
    got = wcp._token_expiry_monotonic(token, now_wall=400.0, now_monotonic=50.0)
    assert got == pytest.approx(50.0 + DEFAULT_WCP_TOKEN_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Login-POST TLS bootstrap
# ---------------------------------------------------------------------------


def test_wcp_tls_toggles_on_verify_tls_without_ca() -> None:
    on = wcp._wcp_tls(host="sup.test", verify_tls=True, ca_pem=None, tls_server_name=None)
    assert on.verify is True
    off = wcp._wcp_tls(host="sup.test", verify_tls=False, ca_pem=None, tls_server_name=None)
    assert off.verify is False


def test_wcp_tls_pins_ca_when_present() -> None:
    sentinel = object()
    with patch(f"{_WCP_MODULE}.ssl.create_default_context", return_value=sentinel) as ctx:
        tls = wcp._wcp_tls(
            host="sup.test", verify_tls=True, ca_pem="PEM-DATA", tls_server_name=None
        )
    assert tls.verify is sentinel
    ctx.assert_called_once_with(cadata="PEM-DATA")


def test_wcp_tls_server_hostname_prefers_override_over_dial_host() -> None:
    # tls_server_name set -> the cert-verify / SNI name is the override
    # (the cert's SAN), not the NAT-alias dial host.
    override = wcp._wcp_tls(
        host="alias.test", verify_tls=True, ca_pem=None, tls_server_name="cert-san.test"
    )
    assert override.server_hostname == "cert-san.test"
    # Unset -> falls back to the dial host (byte-identical to the old
    # verify-the-host behaviour).
    fallback = wcp._wcp_tls(host="alias.test", verify_tls=True, ca_pem=None, tls_server_name=None)
    assert fallback.server_hostname == "alias.test"


# ---------------------------------------------------------------------------
# wcp_login — the /wcp/login exchange
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_success_top_level_context() -> None:
    session = _make_jwt(exp=time.time() + 36_000)
    route = respx.route(method="POST").mock(
        return_value=httpx.Response(
            200, json={"session_id": session, "kube_config": _kube_config()}
        )
    )
    token, kube_config = await wcp_login(
        "supervisor.alias.test",
        username="administrator@vsphere.local",
        password="s3cr3t",
        verify_tls=False,
        ca_pem=None,
    )

    assert token.token == session
    assert kube_config["clusters"][0]["cluster"]["server"] == "https://10.99.99.99:6443"

    request = route.calls.last.request
    # Endpoint: the WCP front on 443, path /wcp/login. httpx elides the
    # default https port, so ``port`` reads None (i.e. 443) — never the
    # kube-API 6443 (a non-default login port is exercised separately).
    assert request.url.host == "supervisor.alias.test"
    assert request.url.path == "/wcp/login"
    assert request.url.port in (None, 443)
    # HTTP Basic with the SSO credential.
    scheme, _, encoded = request.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "administrator@vsphere.local:s3cr3t"
    # No body -> the top-level Supervisor context, never a
    # guest_cluster_* per-workload sub-session (internal-VIP redirect).
    assert request.content == b""


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_threads_tls_server_name_as_sni_extension() -> None:
    route = respx.route(method="POST").mock(
        return_value=httpx.Response(200, json={"session_id": "sess", "kube_config": _kube_config()})
    )
    await wcp_login(
        "alias.test",
        username="u",
        password="p",
        verify_tls=True,
        ca_pem=None,
        tls_server_name="cert-san.test",
    )
    # The cert-verify / SNI name is the override, so a NAT-fronted
    # Supervisor whose cert SANs the internal VIP verifies against it.
    assert route.calls.last.request.extensions.get("sni_hostname") == "cert-san.test"


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_sni_extension_defaults_to_dial_host() -> None:
    route = respx.route(method="POST").mock(
        return_value=httpx.Response(200, json={"session_id": "sess", "kube_config": _kube_config()})
    )
    await wcp_login("sup.test", username="u", password="p", verify_tls=True, ca_pem=None)
    assert route.calls.last.request.extensions.get("sni_hostname") == "sup.test"


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_honours_custom_login_port() -> None:
    route = respx.route(method="POST").mock(
        return_value=httpx.Response(200, json={"session_id": "sess", "kube_config": _kube_config()})
    )
    await wcp_login(
        "sup.test",
        username="u",
        password="p",
        verify_tls=False,
        ca_pem=None,
        login_port=8443,
    )
    assert route.calls.last.request.url.port == 8443


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_accepts_yaml_string_kube_config() -> None:
    yaml_cfg = (
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- name: sup\n  cluster:\n    server: https://10.0.0.5:6443\n"
        "    insecure-skip-tls-verify: true\n"
        "users:\n- name: u\n  user:\n    token: t\n"
        "contexts:\n- name: c\n  context:\n    cluster: sup\n    user: u\n"
        "current-context: c\n"
    )
    respx.route(method="POST").mock(
        return_value=httpx.Response(200, json={"session_id": "sess", "kube_config": yaml_cfg})
    )
    _token, kube_config = await wcp_login(
        "sup.test", username="u", password="p", verify_tls=False, ca_pem=None
    )
    assert kube_config["clusters"][0]["cluster"]["server"] == "https://10.0.0.5:6443"


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_raises_on_non_200() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(401, json={"error": "bad creds"}))
    with pytest.raises(WcpLoginError, match="HTTP 401"):
        await wcp_login("sup.test", username="u", password="p", verify_tls=False, ca_pem=None)


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_raises_on_missing_session_id() -> None:
    respx.route(method="POST").mock(
        return_value=httpx.Response(200, json={"kube_config": _kube_config()})
    )
    with pytest.raises(WcpLoginError, match="no session_id"):
        await wcp_login("sup.test", username="u", password="p", verify_tls=False, ca_pem=None)


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_raises_on_unusable_kube_config() -> None:
    respx.route(method="POST").mock(
        return_value=httpx.Response(200, json={"session_id": "sess", "kube_config": 12345})
    )
    with pytest.raises(WcpLoginError, match="kube_config"):
        await wcp_login("sup.test", username="u", password="p", verify_tls=False, ca_pem=None)


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_wraps_transport_error() -> None:
    respx.route(method="POST").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(WcpLoginError, match="failed"):
        await wcp_login("sup.test", username="u", password="p", verify_tls=False, ca_pem=None)


@respx.mock
@pytest.mark.asyncio
async def test_wcp_login_message_never_echoes_credentials() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(WcpLoginError) as exc:
        await wcp_login(
            "sup.test",
            username="administrator@vsphere.local",
            password="TOPSECRET",
            verify_tls=False,
            ca_pem=None,
        )
    assert "TOPSECRET" not in str(exc.value)
    assert "administrator@vsphere.local" not in str(exc.value)


# ---------------------------------------------------------------------------
# Live TLS — the login leg verifies the cert against tls_server_name, not the
# dial host, so a NAT-fronted Supervisor (host != cert SAN) works (#3832)
# ---------------------------------------------------------------------------

_NOW = datetime.datetime.now(datetime.UTC)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(
    subject_cn: str,
    subject_key: rsa.RSAPrivateKey,
    issuer_name: x509.Name | None,
    issuer_key: rsa.RSAPrivateKey,
    *,
    san_dns: list[str] | None = None,
    is_ca: bool = False,
) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name or subject)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - datetime.timedelta(days=1))
        .not_valid_after(_NOW + datetime.timedelta(days=365))
        # Subject/Authority Key Identifiers are required by OpenSSL's
        # VERIFY_X509_STRICT (on by default in ssl.create_default_context on
        # modern Python), which the code-under-test uses to verify the pin.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
        )
    )
    if is_ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
    if san_dns:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in san_dns]), False
        )
    return builder.sign(issuer_key, hashes.SHA256())


class _SilentTLSLoginServer(ThreadingHTTPServer):
    """A loopback HTTPS server answering ``POST /wcp/login`` with a fixed body.

    The listening socket is TLS-wrapped with a leaf whose SAN is a name that
    is **not** the ``127.0.0.1`` dial host, so verifying the presented cert
    against the dial host fails while verifying against that SAN
    (``tls_server_name``) succeeds. ``handle_error`` is silenced: a client
    that aborts the handshake on a hostname mismatch is the expected path in
    one of the two regression tests, not a server fault.
    """

    daemon_threads = True

    def __init__(self, tmp_path: Path, *, san: str, session_id: str) -> None:
        root_key = _new_key()
        root = _sign("WCP Test Root CA", root_key, None, root_key, is_ca=True)
        leaf_key = _new_key()
        leaf = _sign("wcp-leaf", leaf_key, root.subject, root_key, san_dns=[san])
        cert_file = tmp_path / "leaf.pem"
        key_file = tmp_path / "leaf.key"
        cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        self.ca_pem = root.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self.session_id = session_id
        self.kube_config = _kube_config()
        super().__init__(("127.0.0.1", 0), _WcpLoginHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        self.socket = ctx.wrap_socket(self.socket, server_side=True)

    def handle_error(self, request: object, client_address: object) -> None:
        return None


class _WcpLoginHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.dumps(
            {"session_id": self.server.session_id, "kube_config": self.server.kube_config}  # type: ignore[attr-defined]
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


def _serve(server: _SilentTLSLoginServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.mark.asyncio
async def test_wcp_login_over_live_tls_verifies_against_tls_server_name(tmp_path: Path) -> None:
    # host = 127.0.0.1 (the NAT alias), cert SAN = sni.test. Pinned CA on,
    # verification on. Setting tls_server_name to the SAN makes the login
    # handshake verify against it, not the dial host -> success.
    server = _SilentTLSLoginServer(tmp_path, san="sni.test", session_id="live-sess")
    _serve(server)
    try:
        token, _kube = await wcp_login(
            "127.0.0.1",
            username="u",
            password="p",
            verify_tls=True,
            ca_pem=server.ca_pem,
            tls_server_name="sni.test",
            login_port=server.server_address[1],
        )
    finally:
        server.shutdown()
        server.server_close()
    assert token.token == "live-sess"


@pytest.mark.asyncio
async def test_wcp_login_over_live_tls_fails_without_tls_server_name(tmp_path: Path) -> None:
    # Same server, but no tls_server_name -> the login leg verifies the
    # cert against the dial host (127.0.0.1), which the cert does not SAN,
    # so verification fails and surfaces as a TLS-verification WcpLoginError
    # (not a bare ConnectError). This is the exact NAT-fronted-Supervisor
    # failure the fix removes.
    server = _SilentTLSLoginServer(tmp_path, san="sni.test", session_id="live-sess")
    _serve(server)
    try:
        with pytest.raises(WcpLoginError) as exc:
            await wcp_login(
                "127.0.0.1",
                username="u",
                password="p",
                verify_tls=True,
                ca_pem=server.ca_pem,
                tls_server_name=None,
                login_port=server.server_address[1],
            )
    finally:
        server.shutdown()
        server.server_close()
    message = str(exc.value)
    assert "TLS verification failed" in message
    # server_name is the dial-host fallback here, never the cert SAN.
    assert "127.0.0.1" in message
    assert "sni.test" not in message


# ---------------------------------------------------------------------------
# build_wcp_api_configuration — self-refreshing Configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_configuration_dials_alias_not_internal_vip() -> None:
    login = AsyncMock(return_value=(WcpToken("sess-tok", time.monotonic() + 9000), _kube_config()))
    with patch(f"{_WCP_MODULE}.wcp_login", login):
        cfg = await build_wcp_api_configuration(
            host="supervisor.alias.test",
            api_port=6443,
            username="u",
            password="p",
            verify_tls=True,
            ca_pem=None,
        )
    # The reachable alias, never the internal VIP from the kube_config.
    assert cfg.host == "https://supervisor.alias.test:6443"
    assert cfg.api_key["BearerToken"] == "Bearer sess-tok"
    assert cfg.refresh_api_key_hook is not None


@pytest.mark.asyncio
async def test_build_configuration_tls_knobs() -> None:
    async def _fake_lkcfd(*, config_dict: dict[str, Any], client_configuration: Any) -> None:
        del config_dict
        client_configuration.host = "https://10.99.99.99:6443"
        client_configuration.verify_ssl = True
        client_configuration.api_key["BearerToken"] = "Bearer kubeconfig-embedded-token"

    login = AsyncMock(return_value=(WcpToken("sess", time.monotonic() + 9000), _kube_config()))
    with (
        patch(f"{_WCP_MODULE}.wcp_login", login),
        patch(f"{_WCP_MODULE}.load_kube_config_from_dict", _fake_lkcfd),
    ):
        secure = await build_wcp_api_configuration(
            host="alias.test",
            api_port=6443,
            username="u",
            password="p",
            verify_tls=True,
            ca_pem=None,
        )
        insecure = await build_wcp_api_configuration(
            host="alias.test",
            api_port=6443,
            username="u",
            password="p",
            verify_tls=False,
            ca_pem=None,
        )
    # verify_tls on: CA chain stays verified, and the cert is asserted
    # against tls_server_name -> here unset, so the dial host.
    assert secure.verify_ssl is True
    assert secure.tls_server_name == "alias.test"
    # verify_tls off: full insecure override.
    assert insecure.verify_ssl is False


@pytest.mark.asyncio
async def test_build_configuration_honours_tls_server_name_on_both_legs() -> None:
    login = AsyncMock(return_value=(WcpToken("sess", time.monotonic() + 9000), _kube_config()))
    with patch(f"{_WCP_MODULE}.wcp_login", login):
        cfg = await build_wcp_api_configuration(
            host="alias.test",
            api_port=6443,
            username="u",
            password="p",
            verify_tls=True,
            ca_pem=None,
            tls_server_name="cert-san.test",
        )
    # API leg: kubernetes_asyncio verifies the cert against this name
    # (mapped onto the aiohttp server_hostname).
    assert cfg.tls_server_name == "cert-san.test"
    # Login leg (and the refresh re-mint) receives the same override.
    assert login.await_args.kwargs["tls_server_name"] == "cert-san.test"


@pytest.mark.asyncio
async def test_configuration_refreshes_token_past_expiry() -> None:
    kube = _kube_config()
    login = AsyncMock(
        side_effect=[
            (WcpToken("tok1", 100.0), kube),
            (WcpToken("tok2", 100_000.0), kube),
        ]
    )
    mono = MagicMock(return_value=0.0)
    with patch(f"{_WCP_MODULE}.wcp_login", login), patch(f"{_WCP_MODULE}.time.monotonic", mono):
        cfg = await build_wcp_api_configuration(
            host="alias.test",
            api_port=6443,
            username="u",
            password="p",
            verify_tls=False,
            ca_pem=None,
        )
        assert login.await_count == 1

        # Within the refresh margin (100 - 60 = 40): no re-mint.
        assert await cfg.get_api_key_with_prefix("BearerToken") == "Bearer tok1"
        assert login.await_count == 1

        # Past expiry - margin: the hook re-mints transparently.
        mono.return_value = 50.0
        assert await cfg.get_api_key_with_prefix("BearerToken") == "Bearer tok2"
        assert login.await_count == 2

        # tok2 is long-lived: no further re-mint on the next use.
        assert await cfg.get_api_key_with_prefix("BearerToken") == "Bearer tok2"
        assert login.await_count == 2


@pytest.mark.asyncio
async def test_configuration_refresh_is_single_flight() -> None:
    kube = _kube_config()
    login = AsyncMock(
        side_effect=[
            (WcpToken("tok1", 100.0), kube),
            (WcpToken("tok2", 100_000.0), kube),
            (WcpToken("tok3", 100_000.0), kube),
        ]
    )
    mono = MagicMock(return_value=50.0)  # already past the refresh margin
    with patch(f"{_WCP_MODULE}.wcp_login", login), patch(f"{_WCP_MODULE}.time.monotonic", mono):
        cfg = await build_wcp_api_configuration(
            host="alias.test",
            api_port=6443,
            username="u",
            password="p",
            verify_tls=False,
            ca_pem=None,
        )
        import asyncio

        results = await asyncio.gather(
            cfg.get_api_key_with_prefix("BearerToken"),
            cfg.get_api_key_with_prefix("BearerToken"),
            cfg.get_api_key_with_prefix("BearerToken"),
        )
    # Exactly one re-mint (initial login + one refresh), not three.
    assert login.await_count == 2
    assert set(results) == {"Bearer tok2"}


# ---------------------------------------------------------------------------
# load_kubernetes_credential — payload-shape discriminator
# ---------------------------------------------------------------------------


async def _resolve(secret: dict[str, Any]) -> Any:
    with patch(
        "meho_backplane.connectors.kubernetes.kubeconfig.load_vault_secret_data",
        new=AsyncMock(return_value=secret),
    ):
        return await load_kubernetes_credential(_WCP_TARGET, _make_operator())


@pytest.mark.asyncio
async def test_discriminator_picks_kubeconfig() -> None:
    cred = await _resolve({"kubeconfig": "apiVersion: v1\nkind: Config\nclusters: []\n"})
    assert isinstance(cred, KubeconfigCredential)
    assert cred.config["kind"] == "Config"


@pytest.mark.asyncio
async def test_discriminator_picks_wcp_sso() -> None:
    cred = await _resolve({"username": " admin@vsphere.local ", "password": " pw "})
    assert isinstance(cred, WcpSsoCredential)
    # Whitespace-stripped.
    assert cred.username == "admin@vsphere.local"
    assert cred.password == "pw"


@pytest.mark.asyncio
async def test_discriminator_prefers_kubeconfig_when_both_present() -> None:
    cred = await _resolve(
        {"kubeconfig": "apiVersion: v1\nkind: Config\n", "username": "u", "password": "p"}
    )
    assert isinstance(cred, KubeconfigCredential)


@pytest.mark.asyncio
async def test_discriminator_errors_on_neither_shape() -> None:
    with pytest.raises(VaultCredentialsReadError, match="neither"):
        await _resolve({"apitoken": "nope"})


@pytest.mark.asyncio
async def test_discriminator_errors_on_blank_sso_field() -> None:
    with pytest.raises(VaultCredentialsReadError, match="empty or not a string"):
        await _resolve({"username": "  ", "password": "pw"})


# ---------------------------------------------------------------------------
# Connector integration
# ---------------------------------------------------------------------------


def _wcp_loader(credential: WcpSsoCredential) -> Any:
    async def _loader(target: KubernetesTargetLike, operator: Operator) -> Any:
        del target, operator
        return credential

    return _loader


@pytest.mark.asyncio
async def test_connector_builds_wcp_client_from_sso_credential() -> None:
    connector = KubernetesConnector(
        credential_loader=_wcp_loader(
            WcpSsoCredential(username="admin@vsphere.local", password="pw")
        )
    )
    login = AsyncMock(return_value=(WcpToken("sess-tok", time.monotonic() + 9000), _kube_config()))
    with patch(f"{_WCP_MODULE}.wcp_login", login):
        api_client = await connector._get_api_client(_WCP_TARGET, _make_operator())

    cfg = api_client.configuration
    assert cfg.host == "https://supervisor.alias.test:6443"
    assert cfg.api_key["BearerToken"] == "Bearer sess-tok"
    # The login ran against the reachable alias with the SSO credential.
    assert login.await_args.args[0] == "supervisor.alias.test"
    assert login.await_args.kwargs["username"] == "admin@vsphere.local"
    await connector.aclose()


@pytest.mark.asyncio
async def test_connector_defaults_port_and_forwards_tls_knobs() -> None:
    target = _StubTarget(
        name="wcp",
        host="sup.test",
        port=None,  # -> _DEFAULT_K8S_PORT
        secret_ref="k8s/wcp",
        verify_tls=False,
        tls_ca_pin="CA-PEM",
    )
    connector = KubernetesConnector(
        credential_loader=_wcp_loader(WcpSsoCredential(username="u", password="p"))
    )
    login = AsyncMock(return_value=(WcpToken("t", time.monotonic() + 9000), _kube_config()))
    with patch(f"{_WCP_MODULE}.wcp_login", login):
        api_client = await connector._get_api_client(target, _make_operator())

    assert api_client.configuration.host == f"https://sup.test:{_DEFAULT_K8S_PORT}"
    assert login.await_args.kwargs["verify_tls"] is False
    assert login.await_args.kwargs["ca_pem"] == "CA-PEM"
    await connector.aclose()


@pytest.mark.asyncio
async def test_connector_forwards_tls_server_name_from_target() -> None:
    target = _StubTarget(
        name="wcp",
        host="sup-alias.test",  # the NAT alias
        port=6443,
        secret_ref="k8s/wcp",
        verify_tls=True,
        tls_ca_pin="CA-PEM",
        tls_server_name="sup-vip.test",  # the cert SAN
    )
    connector = KubernetesConnector(
        credential_loader=_wcp_loader(WcpSsoCredential(username="u", password="p"))
    )
    login = AsyncMock(return_value=(WcpToken("t", time.monotonic() + 9000), _kube_config()))
    with patch(f"{_WCP_MODULE}.wcp_login", login):
        api_client = await connector._get_api_client(target, _make_operator())
    # Threaded onto the login leg and asserted on the API leg's config.
    assert login.await_args.kwargs["tls_server_name"] == "sup-vip.test"
    assert api_client.configuration.tls_server_name == "sup-vip.test"
    await connector.aclose()


@pytest.mark.asyncio
async def test_connector_fingerprint_over_wcp() -> None:
    connector = KubernetesConnector(
        credential_loader=_wcp_loader(WcpSsoCredential(username="u", password="p"))
    )
    login = AsyncMock(return_value=(WcpToken("t", time.monotonic() + 9000), _kube_config()))
    version = MagicMock()
    version.git_version = "v1.28.5+vmware.wcp.1"
    version.build_date = "2024-01-04T15:00:00Z"
    version.major = "1"
    version.minor = "28"
    version.platform = "linux/amd64"
    version.go_version = "go1.20"
    version.git_commit = "abc"
    version.git_tree_state = "clean"
    with (
        patch(f"{_WCP_MODULE}.wcp_login", login),
        patch("meho_backplane.connectors.kubernetes.connector.client.VersionApi") as version_api,
    ):
        version_api.return_value.get_code = AsyncMock(return_value=version)
        result = await connector.fingerprint(_WCP_TARGET, _make_operator())
    assert result.vendor == "kubernetes"
    assert result.reachable is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_connector_ws_client_over_wcp() -> None:
    connector = KubernetesConnector(
        credential_loader=_wcp_loader(WcpSsoCredential(username="u", password="p"))
    )
    login = AsyncMock(return_value=(WcpToken("sess-tok", time.monotonic() + 9000), _kube_config()))
    with patch(f"{_WCP_MODULE}.wcp_login", login):
        ws_client = await connector._get_ws_api_client(_WCP_TARGET, _make_operator())
    assert ws_client.configuration.host == "https://supervisor.alias.test:6443"
    assert ws_client.configuration.api_key["BearerToken"] == "Bearer sess-tok"
    await connector.aclose()


@pytest.mark.asyncio
async def test_connector_rejects_both_loaders() -> None:
    async def _cred(target: KubernetesTargetLike, operator: Operator) -> Any:
        del target, operator
        return KubeconfigCredential({})

    async def _kube(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del target, operator
        return {}

    with pytest.raises(ValueError, match="at most one"):
        KubernetesConnector(kubeconfig_loader=_kube, credential_loader=_cred)


@pytest.mark.asyncio
async def test_legacy_kubeconfig_loader_still_works() -> None:
    # The legacy kubeconfig_loader= injection is adapted onto the
    # credential contract; the static path stays a KubeconfigCredential.
    async def _kube(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del target, operator
        return _kube_config()

    connector = KubernetesConnector(kubeconfig_loader=_kube)
    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new=AsyncMock(return_value=MagicMock(close=AsyncMock())),
    ) as ctor:
        await connector._get_api_client(_WCP_TARGET, _make_operator())
    ctor.assert_awaited_once()
    assert ctor.await_args.args[0]["kind"] == "Config"
    await connector.aclose()
