# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Passive-only kubeconfig schema enforcement (security review F03, #265).

Covers :func:`enforce_passive_kubeconfig` and its wiring into the two
production credential loaders. The threat model: a tenant-controlled
kubeconfig secret must not be able to reach ``kubernetes_asyncio``'s
loader with an ``exec`` provider (subprocess), a legacy ``auth-provider``
block (network token fetch) or a local-file credential/CA reference
(disk read) inside the shared backplane process.

The pure-function tests assert the schema rejects those sinks and
rebuilds a fresh config from only vetted fields; the loader/connector
tests assert a synthetic exec config never reaches the (mocked) library
client-build boundary.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.kubeconfig import (
    KubeconfigCredential,
    WcpSsoCredential,
    load_kubernetes_credential,
)
from meho_backplane.connectors.kubernetes.kubeconfig_schema import (
    UnsupportedKubeconfigError,
    enforce_passive_kubeconfig,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client


def _passive_config() -> dict[str, Any]:
    """A minimal valid inline-token kubeconfig mapping (as parsed YAML)."""
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
        "users": [{"name": "u1", "user": {"token": "inline-bearer"}}],
    }


# ---------------------------------------------------------------------------
# Happy path — accepted inline auth is preserved; a fresh config is built.
# (Acceptance criteria 3 + 4.)
# ---------------------------------------------------------------------------


def test_accepts_and_rebuilds_inline_token_config() -> None:
    config = _passive_config()
    result = enforce_passive_kubeconfig(config)
    assert result == {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "clusters": [{"cluster": {"server": "https://k8s.test:6443"}, "name": "c1"}],
        "users": [{"user": {"token": "inline-bearer"}, "name": "u1"}],
        "contexts": [{"context": {"cluster": "c1", "user": "u1"}, "name": "default"}],
    }


def test_accepts_inline_client_certificate_and_ca_data() -> None:
    config = {
        "apiVersion": "v1",
        "clusters": [
            {
                "name": "c1",
                "cluster": {
                    "server": "https://k8s.test:6443",
                    "certificate-authority-data": "Y2EtcGVt",
                    "tls-server-name": "k8s.internal",
                    "insecure-skip-tls-verify": False,
                },
            }
        ],
        "users": [
            {
                "name": "u1",
                "user": {
                    "client-certificate-data": "Y2VydC1wZW0=",
                    "client-key-data": "a2V5LXBlbQ==",
                },
            }
        ],
    }
    result = enforce_passive_kubeconfig(config)
    assert result["clusters"][0]["cluster"] == {
        "server": "https://k8s.test:6443",
        "certificate-authority-data": "Y2EtcGVt",
        "tls-server-name": "k8s.internal",
        "insecure-skip-tls-verify": False,
    }
    assert result["users"][0]["user"] == {
        "client-certificate-data": "Y2VydC1wZW0=",
        "client-key-data": "a2V5LXBlbQ==",
    }


def test_accepts_basic_auth_config() -> None:
    config = _passive_config()
    config["users"] = [{"name": "u1", "user": {"username": "admin", "password": "s3cr3t"}}]
    result = enforce_passive_kubeconfig(config)
    assert result["users"][0]["user"] == {"username": "admin", "password": "s3cr3t"}


def test_returns_fresh_mapping_not_the_input() -> None:
    config = _passive_config()
    result = enforce_passive_kubeconfig(config)
    assert result is not config
    assert result["clusters"] is not config["clusters"]
    assert result["clusters"][0] is not config["clusters"][0]
    assert result["clusters"][0]["cluster"] is not config["clusters"][0]["cluster"]


def test_drops_unrecognised_keys() -> None:
    config = _passive_config()
    config["extensions"] = [{"name": "x", "extension": {"do": "stuff"}}]
    config["clusters"][0]["cluster"]["disable-compression"] = True
    config["users"][0]["user"]["as"] = "impersonated"
    result = enforce_passive_kubeconfig(config)
    assert "extensions" not in result
    assert "disable-compression" not in result["clusters"][0]["cluster"]
    assert "as" not in result["users"][0]["user"]


def test_accepts_http_and_https_and_socks5_endpoints() -> None:
    for server in ("https://k8s.test:6443", "http://127.0.0.1:8080"):
        config = _passive_config()
        config["clusters"][0]["cluster"]["server"] = server
        assert enforce_passive_kubeconfig(config)["clusters"][0]["cluster"]["server"] == server
    config = _passive_config()
    config["clusters"][0]["cluster"]["proxy-url"] = "socks5://proxy.test:1080"
    result = enforce_passive_kubeconfig(config)
    assert result["clusters"][0]["cluster"]["proxy-url"] == "socks5://proxy.test:1080"


# ---------------------------------------------------------------------------
# Reject active / out-of-band credential mechanisms.
# (Acceptance criteria 1 + 5.)
# ---------------------------------------------------------------------------


def test_rejects_exec_provider() -> None:
    config = _passive_config()
    config["users"][0]["user"] = {
        "exec": {
            "apiVersion": "client.authentication.k8s.io/v1",
            "command": "/bin/evil-plugin",
            "args": ["--steal"],
        }
    }
    with pytest.raises(UnsupportedKubeconfigError, match="exec"):
        enforce_passive_kubeconfig(config)


@pytest.mark.parametrize("provider_name", ["gcp", "oidc", "azure"])
def test_rejects_legacy_auth_provider(provider_name: str) -> None:
    config = _passive_config()
    config["users"][0]["user"] = {"auth-provider": {"name": provider_name, "config": {}}}
    with pytest.raises(UnsupportedKubeconfigError, match="auth-provider"):
        enforce_passive_kubeconfig(config)


def test_rejects_token_file_reference() -> None:
    config = _passive_config()
    config["users"][0]["user"] = {"tokenFile": "/var/run/secrets/token"}
    with pytest.raises(UnsupportedKubeconfigError, match="tokenFile"):
        enforce_passive_kubeconfig(config)


@pytest.mark.parametrize("file_key", ["client-certificate", "client-key"])
def test_rejects_user_local_file_reference(file_key: str) -> None:
    config = _passive_config()
    config["users"][0]["user"] = {file_key: "/etc/k8s/creds.pem"}
    with pytest.raises(UnsupportedKubeconfigError, match=file_key):
        enforce_passive_kubeconfig(config)


def test_rejects_certificate_authority_file_reference() -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"]["certificate-authority"] = "/etc/k8s/ca.crt"
    with pytest.raises(UnsupportedKubeconfigError, match="certificate-authority"):
        enforce_passive_kubeconfig(config)


def test_rejects_certificate_authority_file_even_beside_inline_data() -> None:
    # The library would prefer the inline data and ignore the file, but a
    # passive config must not carry a local-file reference at all.
    config = _passive_config()
    config["clusters"][0]["cluster"]["certificate-authority"] = "/etc/k8s/ca.crt"
    config["clusters"][0]["cluster"]["certificate-authority-data"] = "Y2EtcGVt"
    with pytest.raises(UnsupportedKubeconfigError, match="certificate-authority"):
        enforce_passive_kubeconfig(config)


def test_no_parameter_re_enables_exec() -> None:
    # Acceptance criterion 5: no retained executable-provider facility —
    # enforcement takes only the config, with no allow/escalate switch.
    import inspect

    params = list(inspect.signature(enforce_passive_kubeconfig).parameters)
    assert params == ["config"]


# ---------------------------------------------------------------------------
# Endpoint / proxy / TLS validation. (Acceptance criterion 3.)
# ---------------------------------------------------------------------------


def test_rejects_cluster_without_server() -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"] = {"certificate-authority-data": "Y2EtcGVt"}
    with pytest.raises(UnsupportedKubeconfigError, match="server"):
        enforce_passive_kubeconfig(config)


@pytest.mark.parametrize("server", ["file:///etc/passwd", "ssh://host", "unix:///run/k.sock"])
def test_rejects_non_http_server_scheme(server: str) -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"]["server"] = server
    with pytest.raises(UnsupportedKubeconfigError, match="http"):
        enforce_passive_kubeconfig(config)


def test_rejects_server_without_host() -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"]["server"] = "https://"
    with pytest.raises(UnsupportedKubeconfigError, match="host"):
        enforce_passive_kubeconfig(config)


def test_rejects_invalid_proxy_scheme() -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"]["proxy-url"] = "ftp://proxy.test"
    with pytest.raises(UnsupportedKubeconfigError, match="proxy-url"):
        enforce_passive_kubeconfig(config)


def test_rejects_non_bool_insecure_skip_tls_verify() -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = "true"
    with pytest.raises(UnsupportedKubeconfigError, match="insecure-skip-tls-verify"):
        enforce_passive_kubeconfig(config)


def test_rejects_non_string_token() -> None:
    config = _passive_config()
    config["users"][0]["user"]["token"] = 12345
    with pytest.raises(UnsupportedKubeconfigError, match="token"):
        enforce_passive_kubeconfig(config)


# ---------------------------------------------------------------------------
# Error hygiene — messages never echo a credential / endpoint / cert value.
# ---------------------------------------------------------------------------


def test_rejection_message_omits_secret_values() -> None:
    config = _passive_config()
    config["clusters"][0]["cluster"]["server"] = "https://secret-endpoint.internal:6443"
    config["users"][0]["user"] = {
        "exec": {"command": "/bin/exfiltrate-SECRET", "args": ["token-CANARY"]},
    }
    with pytest.raises(UnsupportedKubeconfigError) as excinfo:
        enforce_passive_kubeconfig(config)
    message = str(excinfo.value)
    assert "exfiltrate-SECRET" not in message
    assert "token-CANARY" not in message

    bad_server = _passive_config()
    bad_server["clusters"][0]["cluster"]["server"] = "ftp://secret-endpoint.internal:6443"
    with pytest.raises(UnsupportedKubeconfigError) as server_exc:
        enforce_passive_kubeconfig(bad_server)
    assert "secret-endpoint.internal" not in str(server_exc.value)


# ---------------------------------------------------------------------------
# Loader + connector wiring — the rejected config never reaches the
# library client-build boundary. (Acceptance criteria 2 + 4.)
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="rke2-meho",
    host="rke2-meho.test.invalid",
    port=6443,
    secret_ref="k8s/rke2-meho",
)

_EXEC_KUBECONFIG_YAML = """apiVersion: v1
kind: Config
current-context: default
clusters:
- name: c1
  cluster: {server: 'https://k8s.test:6443'}
contexts:
- name: default
  context: {cluster: c1, user: u1}
users:
- name: u1
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1
      command: /bin/evil-plugin
      args: ['--steal']
"""

_INLINE_KUBECONFIG_YAML = """apiVersion: v1
kind: Config
current-context: default
clusters:
- name: c1
  cluster: {server: 'https://k8s.test:6443'}
contexts:
- name: default
  context: {cluster: c1, user: u1}
users:
- name: u1
  user: {token: inline-bearer}
"""


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


def _make_operator() -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def test_default_loader_rejects_exec_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(monkeypatch, secret={"kubeconfig": _EXEC_KUBECONFIG_YAML})

    async def _check() -> None:
        with pytest.raises(UnsupportedKubeconfigError, match="exec"):
            await load_kubernetes_credential(_TARGET, _make_operator())

    asyncio.run(_check())


def test_default_loader_accepts_inline_token_kubeconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"kubeconfig": _INLINE_KUBECONFIG_YAML})

    async def _check() -> None:
        credential = await load_kubernetes_credential(_TARGET, _make_operator())
        assert isinstance(credential, KubeconfigCredential)
        assert credential.config["users"][0]["user"] == {"token": "inline-bearer"}

    asyncio.run(_check())


def test_exec_config_never_reaches_library_client_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthetic exec config raises before ``new_client_from_config_dict``."""
    install_fake_client(monkeypatch, secret={"kubeconfig": _EXEC_KUBECONFIG_YAML})
    connector = KubernetesConnector()  # default (non-injected) credential loader

    async def _check() -> None:
        with patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
        ) as client_factory:
            with pytest.raises(UnsupportedKubeconfigError):
                await connector._get_api_client(_TARGET, _make_operator())
            client_factory.assert_not_awaited()

    asyncio.run(_check())


def test_wcp_secret_bypasses_the_kubeconfig_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vSphere Supervisor secret (username/password) is unaffected by F03."""
    install_fake_client(
        monkeypatch, secret={"username": "administrator@vsphere.local", "password": "pw"}
    )

    async def _check() -> None:
        credential = await load_kubernetes_credential(_TARGET, _make_operator())
        assert isinstance(credential, WcpSsoCredential)
        assert credential.username == "administrator@vsphere.local"

    asyncio.run(_check())
