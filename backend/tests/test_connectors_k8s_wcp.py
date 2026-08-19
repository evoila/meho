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
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

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


def test_login_tls_toggles_on_verify_tls_without_ca() -> None:
    assert wcp._login_tls(True, None) is True
    assert wcp._login_tls(False, None) is False


def test_login_tls_pins_ca_when_present() -> None:
    sentinel = object()
    with patch(f"{_WCP_MODULE}.ssl.create_default_context", return_value=sentinel) as ctx:
        assert wcp._login_tls(True, "PEM-DATA") is sentinel
    ctx.assert_called_once_with(cadata="PEM-DATA")


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
    # verify_tls on: CA chain stays verified, hostname assertion off
    # (we dial the alias, not the cert's internal-VIP SAN).
    assert secure.verify_ssl is True
    assert secure.assert_hostname is False
    # verify_tls off: full insecure override.
    assert insecure.verify_ssl is False


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
