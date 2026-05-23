# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.10-T4 — k3d + live Vault dev-mode E2E for the k8s kubeconfig-read chain.

The **live** counterpart to the recorded-fixture E2E
(``tests/test_connectors_k8s_credread.py``). Where that replays a canned
Vault read + a mocked kubernetes_asyncio client in the secret-free unit
lane, this test runs the *real* chain end to end against:

* a real k3s container (booted via :class:`testcontainers.k3s.K3SContainer`),
* a real Vault dev-mode container (booted via
  :class:`testcontainers.vault.VaultContainer`),

seeded with the k3s container's emitted kubeconfig. The operator-context
JWT/OIDC login + KV-v2 read + YAML parse + ``ApiClient`` build +
``CoreV1Api.list_namespace`` round-trip all run for real — the rubric
**State 2** proof against live infrastructure.

It is **opt-in and skips cleanly by default** so secret-free CI stays
green. Two gates:

* Docker socket missing — every integration test in this package skips
  on the same heuristic (mirrors
  ``tests/integration/conftest.py``).
* ``MEHO_RUN_LIVE_K3D_VAULT`` is not set to ``"1"`` — the explicit opt-in
  signal: even when Docker is available (e.g. on the integration-lane CI
  runner), the test only runs when the operator flips this flag. This is
  the same pattern other heavier opt-in tests in the suite use; the
  default integration lane runs the recorded-fixture suite for k8s and
  saves the live container boot for an operator-driven smoke.

What this test proves
======================

The full dispatch->loader->Vault->kubeconfig->k3s chain executes once
under a real operator JWT (synthesised against the dev-mode Vault's
JWT/OIDC backend), reads a real kubeconfig out of a real Vault, parses
it, builds a real ``ApiClient`` against a real k3s API server, and lists
real namespaces. The kubeconfig YAML itself is asserted absent from the
``OperationResult`` and from every captured structlog event — the
no-leak contract pinned in the recorded-fixture E2E, re-asserted here
against the live chain.

What it does NOT prove
======================

* It does not exercise the real Keycloak->Vault federation chain. The
  dev-mode Vault here is configured with a JWT/OIDC auth backend that
  accepts a self-signed token built locally; the real
  ``keycloak.<env>.example`` -> Vault Identity entity binding is a
  deploy concern, validated by the cross-repo deploy runbook
  (``docs/cross-repo/connector-vault-policy.md``) and by an operator-driven
  smoke against the real lab Vault.
* It does not run the dispatcher's full audit + broadcast machinery
  end-to-end against a real Postgres; the recorded-fixture E2E
  (``tests/test_connectors_k8s_credread.py``) covers that against the
  per-worker SQLite engine.

How to run it
=============

```bash
# Run once against a Docker-having sandbox or CI runner:
MEHO_RUN_LIVE_K3D_VAULT=1 \\
  pytest backend/tests/integration/test_connectors_k8s_live_vault.py -v
```
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.kubernetes import (
    KubernetesConnector,
    parse_kubeconfig_yaml,
    register_kubernetes_typed_operations,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Opt-in gates: Docker AND the explicit MEHO_RUN_LIVE_K3D_VAULT=1 signal.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE: bool = _docker_socket_present()
_RUN_LIVE: bool = os.environ.get("MEHO_RUN_LIVE_K3D_VAULT") == "1"

_SKIP_REASON: str = (
    "live k3d + Vault E2E is opt-in: set MEHO_RUN_LIVE_K3D_VAULT=1 to run it "
    "against real container infra (skipped in default CI; the recorded-fixture "
    "E2E covers the dispatch chain in the secret-free unit lane)."
)


pytestmark = pytest.mark.skipif(not (_DOCKER_AVAILABLE and _RUN_LIVE), reason=_SKIP_REASON)


# ---------------------------------------------------------------------------
# Container fixtures — module-scoped (one boot per session, multiple tests)
# ---------------------------------------------------------------------------


_VAULT_KUBECONFIG_PATH = "k8s/k3d-live-vault"


@dataclass
class _LiveK3dTarget:
    """Target carrying the resolver + connector + loader attributes."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    product: str = "k8s"
    auth_model: str = "shared_service_account"

    def __post_init__(self) -> None:
        self.id: UUID = uuid4()
        self.preferred_impl_id: str | None = None
        self.fingerprint = type("_FP", (), {"version": "1.32.0"})()


@pytest.fixture(scope="module")
def k3s_kubeconfig_text() -> Iterator[str]:
    """Boot a k3s container; yield the kubeconfig YAML it exposes."""
    try:
        from testcontainers.k3s import K3SContainer
    except ImportError as exc:  # pragma: no cover — testcontainers ships k3s
        pytest.skip(f"testcontainers.k3s unavailable: {exc}")

    image = os.environ.get("MEHO_TEST_K3S_IMAGE", "rancher/k3s:v1.32.5-k3s1")
    try:
        container = K3SContainer(image=image)
        container.start()
    except Exception as exc:
        pytest.skip(f"k3s container failed to start ({type(exc).__name__}): {exc}")

    try:
        yield container.config_yaml()
    finally:
        container.stop()


@pytest.fixture(scope="module")
def vault_container_and_token() -> Iterator[tuple[str, str]]:
    """Boot a Vault dev-mode container; yield (address, root_token).

    Dev mode mounts KV-v2 at ``secret/`` and seeds a known root token;
    this test seeds the kubeconfig at ``secret/<path>`` directly via the
    root token (the real JWT/OIDC backend wiring is the deploy concern
    documented in ``docs/cross-repo/connector-vault-policy.md``).
    """
    try:
        from testcontainers.vault import VaultContainer
    except ImportError as exc:  # pragma: no cover — testcontainers ships vault
        pytest.skip(f"testcontainers.vault unavailable: {exc}")

    root_token = os.environ.get("MEHO_TEST_VAULT_ROOT_TOKEN", "meho-test-root-token")
    image = os.environ.get("MEHO_TEST_VAULT_IMAGE", "hashicorp/vault:1.18.4")
    try:
        container = VaultContainer(image=image, root_token=root_token)
        container.start()
    except Exception as exc:
        pytest.skip(f"Vault container failed to start ({type(exc).__name__}): {exc}")

    try:
        yield container.get_connection_url(), root_token
    finally:
        container.stop()


@pytest.fixture(scope="module")
def seeded_vault_target(
    k3s_kubeconfig_text: str,
    vault_container_and_token: tuple[str, str],
) -> _LiveK3dTarget:
    """Seed the kubeconfig into Vault; return the matching target stub.

    Uses an hvac client with the dev-mode root token to write the
    kubeconfig YAML at ``secret/<path>`` under the ``kubeconfig`` field
    — the exact convention the live loader reads.
    """
    import hvac

    vault_addr, root_token = vault_container_and_token
    client = hvac.Client(url=vault_addr, token=root_token)
    client.secrets.kv.v2.create_or_update_secret(
        path=_VAULT_KUBECONFIG_PATH,
        secret={"kubeconfig": k3s_kubeconfig_text},
        mount_point="secret",
    )

    # Derive the target's host/port from the kubeconfig the container
    # emitted — the live loader will parse the *same* YAML, build an
    # ApiClient, and the dispatch's resolver-driven connector reach the
    # same k3s API.
    parsed_kc = parse_kubeconfig_yaml(k3s_kubeconfig_text)
    server_url = parsed_kc["clusters"][0]["cluster"]["server"]
    parsed = urlparse(server_url)
    return _LiveK3dTarget(
        name="k3d-live-vault",
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port,
        secret_ref=_VAULT_KUBECONFIG_PATH,
    )


# ---------------------------------------------------------------------------
# Settings + dispatcher wiring (mirrors the recorded-fixture suite, but
# points Settings at the live Vault and configures the dev-mode JWT/OIDC
# backend that mints tokens for the synthesised operator JWT below).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(
    monkeypatch: pytest.MonkeyPatch,
    vault_container_and_token: tuple[str, str],
) -> Iterator[None]:
    """Point Settings at the live Vault. The Keycloak vars are stubs;
    the dev-mode Vault accepts the synthesised JWT via a configured
    JWT/OIDC backend (configured by the ``configured_oidc`` fixture).
    """
    vault_addr, _ = vault_container_and_token
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", vault_addr)
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "10.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def configured_jwt_backend(
    vault_container_and_token: tuple[str, str],
) -> dict[str, Any]:
    """Configure Vault's JWT/OIDC backend to accept a self-signed JWT.

    Production reaches Vault via Keycloak's OIDC discovery + JWKS; the
    dev-mode container here is configured with a static JWT validation
    key matching the key the test signs the operator JWT with. This
    short-circuits the Keycloak dependency without weakening what the
    test proves: the loader's ``vault_client_for_operator`` code path
    runs in full, the JWT is forwarded to Vault, Vault validates it
    against its configured key, and the resulting token authorises the
    KV-v2 read.

    Returns ``{"private_key_pem", "kid", "role_name"}`` so the
    operator-JWT minter can sign a token Vault will accept.
    """
    import hvac
    from authlib.jose import JsonWebKey

    vault_addr, root_token = vault_container_and_token
    client = hvac.Client(url=vault_addr, token=root_token)

    # Mount the JWT/OIDC auth method at "jwt/" (the default mount the
    # ``VAULT_OIDC_MOUNT_PATH=jwt`` setting tracks).
    import contextlib

    with contextlib.suppress(Exception):
        # pragma: no cover — already enabled across re-runs
        client.sys.enable_auth_method(method_type="jwt", path="jwt")

    # Generate an RSA keypair the test signs the operator JWT with;
    # Vault validates against the public half.
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    public_pem = key.get_public_key().as_pem(is_private=False).decode("utf-8")
    private_pem = key.as_pem(is_private=True).decode("utf-8")

    # Configure the JWT/OIDC backend to validate against the public key.
    client.auth.jwt.configure(
        jwt_validation_pubkeys=[public_pem],
        path="jwt",
    )

    # Create the role the connector uses (the ``VAULT_OIDC_ROLE`` setting).
    # ``bound_audiences`` matches what ``vault_client_for_operator`` sends
    # (Settings.keycloak_audience). The role grants the templated policy
    # that lets the operator's identity read the kubeconfig path.
    role_name = "meho-mcp"
    policy_name = "meho-mcp-test"
    client.sys.create_or_update_policy(
        name=policy_name,
        policy=f'path "secret/data/{_VAULT_KUBECONFIG_PATH}" {{ capabilities = ["read"] }}',
    )
    client.auth.jwt.create_role(
        name=role_name,
        token_policies=[policy_name],
        role_type="jwt",
        bound_audiences=["meho-backplane"],
        user_claim="sub",
        path="jwt",
    )

    return {
        "private_key_pem": private_pem,
        "role_name": role_name,
    }


def _mint_operator_jwt(private_key_pem: str) -> str:
    """Mint a self-signed operator JWT Vault will accept."""
    import time

    from authlib.jose import jwt

    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "sub": "op-live-k3d",
        "aud": "meho-backplane",
        "iss": "https://keycloak.test/realms/meho",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "name": "Live K3d Operator",
    }
    return jwt.encode(header, payload, private_key_pem).decode("utf-8")


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    clear_registry()
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=KubernetesConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def k8s_live_wired(
    stub_embedding_service: AsyncMock,
    pg_engine: None,
) -> AsyncIterator[None]:
    """Register the typed K8s ops into the live ``endpoint_descriptor`` table."""
    await register_kubernetes_typed_operations(embedding_service=stub_embedding_service)
    yield


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


async def test_live_dispatch_kubeconfig_from_vault_lists_namespaces(
    seeded_vault_target: _LiveK3dTarget,
    configured_jwt_backend: dict[str, Any],
    captured_events: list[BroadcastEvent],
    k8s_live_wired: None,
    k3s_kubeconfig_text: str,
) -> None:
    """Live chain: dispatch -> live Vault read -> live k3s namespace list -> status=ok.

    Exercises the full G3.10-T4 wiring against real containers. The
    default loader is **not** overridden; the resolver builds
    ``KubernetesConnector()`` and the
    :func:`load_kubeconfig_from_vault` path runs verbatim under the
    operator's JWT.

    Asserts:

    * ``status="ok"`` with a non-empty namespace list (k3s ships
      ``default`` + ``kube-system`` + ``kube-public`` +
      ``kube-node-lease`` at boot).
    * The kubeconfig YAML never appears in the ``OperationResult`` or
      in any captured structlog event — the no-leak contract.
    """
    operator_jwt = _mint_operator_jwt(configured_jwt_backend["private_key_pem"])
    operator = Operator(
        sub="op-live-k3d",
        name="Live K3d Operator",
        email=None,
        raw_jwt=operator_jwt,
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )

    with capture_logs() as captured:
        result = await dispatch(
            operator=operator,
            connector_id="k8s-1.x",
            op_id="k8s.namespace.list",
            target=seeded_vault_target,
            params={},
        )

    # The live chain returned status=ok with a real namespace list.
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["total"] >= 4  # k3s default boot set
    namespace_names = {row["name"] for row in result.result["rows"]}
    assert "default" in namespace_names
    assert "kube-system" in namespace_names

    # No kubeconfig content appears in the result envelope. The server URL,
    # client-cert PEM, and the token from the kubeconfig YAML are all
    # canaries — independently asserted so a partial leak is named.
    result_blob = repr(result)
    parsed_kc = parse_kubeconfig_yaml(k3s_kubeconfig_text)
    server_url = parsed_kc["clusters"][0]["cluster"]["server"]
    cluster_block = parsed_kc["clusters"][0]["cluster"]
    cert_data = cluster_block.get("certificate-authority-data")
    user_block = parsed_kc["users"][0]["user"]
    user_token = user_block.get("token")
    user_cert = user_block.get("client-certificate-data")
    user_key = user_block.get("client-key-data")

    assert server_url not in result_blob, "server URL leaked into OperationResult"
    if cert_data:
        assert cert_data not in result_blob, "cluster CA cert leaked into OperationResult"
    if user_token:
        assert user_token not in result_blob, "bearer token leaked into OperationResult"
    if user_cert:
        assert user_cert not in result_blob, "client cert leaked into OperationResult"
    if user_key:
        assert user_key not in result_blob, "client key leaked into OperationResult"

    # And no kubeconfig content in any captured structlog event.
    log_blob = repr(captured)
    assert server_url not in log_blob, "server URL leaked into structlog events"
    if cert_data:
        assert cert_data not in log_blob, "cluster CA cert leaked into structlog events"
    if user_token:
        assert user_token not in log_blob, "bearer token leaked into structlog events"
    if user_cert:
        assert user_cert not in log_blob, "client cert leaked into structlog events"
    if user_key:
        assert user_key not in log_blob, "client key leaked into structlog events"
