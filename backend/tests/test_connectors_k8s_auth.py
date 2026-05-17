# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.connectors.kubernetes` (G3.2-T1, #321).

The integration shape (real k3s cluster) lives in
:mod:`tests.integration.test_connectors_k8s_k3d`. This module
exercises the same contract with mocked ``kubernetes_asyncio`` /
``httpx`` so the gate runs in every CI lane regardless of Docker
availability.

Coverage matrix (per #321 acceptance criteria):

* :func:`product_from_git_version` correctly maps rke2 / k3s / eks /
  gke / aks / vanilla.
* ``fingerprint(target)`` builds the canonical shape from a mocked
  ``VersionApi.get_code()`` response — vendor="kubernetes", product
  derived, version + build + extras populated, ``probed_at`` set.
* ``probe(target)`` returns ``ok=True`` for HTTP 200 / 401 on
  ``/readyz`` and ``ok=False`` with an informative reason on
  network failure / non-OK status.
* ``execute`` returns the structured ``unknown_op`` shape for any
  op_id.
* ``_get_api_client`` reuses the cached client for the same target
  and builds distinct clients for different targets — the kubeconfig
  loader is called exactly once per target name.
* ``aclose`` calls ``ApiClient.close`` on every cached client and
  empties the cache.
* :func:`parse_kubeconfig_yaml` rejects non-mapping YAML inputs.
* Importing the package exposes the public surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from meho_backplane.connectors import Connector
from meho_backplane.connectors.kubernetes import (
    KubernetesConnector,
    KubernetesTargetLike,
    parse_kubeconfig_yaml,
    product_from_git_version,
)
from meho_backplane.connectors.kubernetes.connector import _DEFAULT_K8S_PORT
from meho_backplane.connectors.kubernetes.kubeconfig import load_kubeconfig_from_vault
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires.

    The G0.6 refactor (#391) made ``execute`` hit the descriptor table
    via the DB engine, so the lookup path needs ``Settings`` to
    construct -- which requires the three Keycloak/Vault env vars.
    Fingerprint / probe tests don't touch the DB but the fixture is
    autouse so all tests in the module see a consistent env.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Target stub — satisfies KubernetesTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET_A = _StubTarget(
    name="rke2-meho",
    host="rke2-meho.test.invalid",
    port=6443,
    secret_ref="kv/data/k8s/rke2-meho",
)
_TARGET_B = _StubTarget(
    name="rke2-infra",
    host="rke2-infra.test.invalid",
    port=None,
    secret_ref="kv/data/k8s/rke2-infra",
)


def _stub_kubeconfig_dict() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
        "users": [{"name": "u1", "user": {"token": "stub-token"}}],
    }


def _stub_version() -> MagicMock:
    """A duck for ``kubernetes_asyncio.client.VersionInfo``.

    All nine fields VersionApi.get_code() populates are str — we mirror
    the live attribute_map so the connector code path is exercised
    unchanged.
    """
    v = MagicMock()
    v.git_version = "v1.28.5+rke2r1"
    v.build_date = "2024-01-04T15:00:00Z"
    v.major = "1"
    v.minor = "28+"
    v.platform = "linux/amd64"
    v.go_version = "go1.20.13"
    v.git_commit = "abc1234"
    v.git_tree_state = "clean"
    return v


def _make_connector_with_stub_kubeconfig() -> KubernetesConnector:
    """Build a connector whose loader returns the stub kubeconfig dict."""

    async def _loader(target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    return KubernetesConnector(kubeconfig_loader=_loader)


# ---------------------------------------------------------------------------
# Package + ABC plumbing
# ---------------------------------------------------------------------------


def test_kubernetes_connector_subclasses_connector_abc() -> None:
    # G0.6 refactor (#391) flipped ``product`` from ``"kubernetes"`` to
    # the v2-canonical ``"k8s"``. G3.2-T6 precursor (#326) then realigned
    # ``impl_id`` from the library name ``"kubernetes-asyncio"`` to the
    # single-impl ``impl_id == product`` shape so the dispatcher's
    # connector_id parser round-trips the canonical ``"k8s-1.x"`` form
    # (``parse_connector_id("k8s-1.x")`` -> ``("k8s", "1.x", "k8s")``).
    assert issubclass(KubernetesConnector, Connector)
    assert KubernetesConnector.product == "k8s"
    assert KubernetesConnector.version == "1.x"
    assert KubernetesConnector.impl_id == "k8s"


def test_default_loader_raises_until_g03_lands() -> None:
    """The default Vault-shaped loader stays unimplemented until G0.3."""

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"G0\.3"):
            await load_kubeconfig_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# product_from_git_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("git_version", "expected"),
    [
        ("v1.28.5+rke2r1", "rke2"),
        ("v1.27.4+k3s1", "k3s"),
        ("v1.29.0-eks-abc1234", "eks"),
        ("v1.28.3-gke.100", "gke"),
        ("v1.27.7-aks", "aks"),
        ("v1.29.0", "vanilla"),
        ("v1.29.0+0", "vanilla"),
    ],
)
def test_product_from_git_version(git_version: str, expected: str) -> None:
    assert product_from_git_version(git_version) == expected


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_builds_canonical_shape() -> None:
    connector = _make_connector_with_stub_kubeconfig()

    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.VersionApi"
        ) as version_api_cls,
    ):
        version_api_cls.return_value.get_code = AsyncMock(return_value=_stub_version())
        result = await connector.fingerprint(_TARGET_A)

    assert isinstance(result, FingerprintResult)
    assert result.vendor == "kubernetes"
    assert result.product == "rke2"
    assert result.version == "v1.28.5+rke2r1"
    assert result.build == "2024-01-04T15:00:00Z"
    assert result.edition is None
    assert result.reachable is True
    assert result.probe_method == "GET /version"
    assert dict(result.extras) == {
        "major": "1",
        "minor": "28+",
        "platform": "linux/amd64",
        "go_version": "go1.20.13",
        "git_commit": "abc1234",
        "git_tree_state": "clean",
    }


# ---------------------------------------------------------------------------
# probe — happy path + failure modes
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """Minimal async context-manager stub for ``httpx.AsyncClient``.

    The connector uses ``async with httpx.AsyncClient(...) as http:``;
    we replace the constructor with a factory that returns this stub
    so the test never hits the network. Pass ``status_code`` for a
    fixed-response stub or ``status_by_url`` for a per-URL mapping
    that exercises the ``/readyz`` → ``/healthz`` fallback path.
    """

    def __init__(
        self,
        *,
        status_code: int | None = None,
        status_by_url: dict[str, int] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._status_code = status_code
        self._status_by_url = status_by_url or {}
        self._exc = exc
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str) -> Any:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        resp = MagicMock()
        if self._status_by_url:
            for suffix, code in self._status_by_url.items():
                if url.endswith(suffix):
                    resp.status_code = code
                    return resp
            raise AssertionError(f"unstubbed URL: {url}")
        resp.status_code = self._status_code
        return resp


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 401])
async def test_probe_ok_on_200_and_401(status_code: int) -> None:
    """200 = healthy; 401 = up + speaking TLS, auth surfaces elsewhere."""
    connector = _make_connector_with_stub_kubeconfig()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.httpx.AsyncClient",
        return_value=_FakeAsyncClient(status_code=status_code),
    ):
        result = await connector.probe(_TARGET_A)
    assert isinstance(result, ProbeResult)
    assert result.ok is True
    assert result.reason is None
    assert result.latency_ms is not None and result.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_probe_uses_default_port_when_target_port_is_none() -> None:
    connector = _make_connector_with_stub_kubeconfig()
    captured: dict[str, str] = {}

    class _Capturing(_FakeAsyncClient):
        async def get(self, url: str) -> Any:
            captured["url"] = url
            return await super().get(url)

    with patch(
        "meho_backplane.connectors.kubernetes.connector.httpx.AsyncClient",
        return_value=_Capturing(status_code=200),
    ):
        await connector.probe(_TARGET_B)
    assert captured["url"] == f"https://{_TARGET_B.host}:{_DEFAULT_K8S_PORT}/readyz"


@pytest.mark.asyncio
async def test_probe_returns_not_ok_on_non_ok_status() -> None:
    connector = _make_connector_with_stub_kubeconfig()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.httpx.AsyncClient",
        return_value=_FakeAsyncClient(status_code=503),
    ):
        result = await connector.probe(_TARGET_A)
    assert result.ok is False
    assert result.reason is not None and "503" in result.reason


@pytest.mark.asyncio
async def test_probe_returns_not_ok_on_transport_error() -> None:
    """Network-level failures surface as ``ok=False`` with a typed reason."""
    connector = _make_connector_with_stub_kubeconfig()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.httpx.AsyncClient",
        return_value=_FakeAsyncClient(exc=httpx.ConnectError("connection refused")),
    ):
        result = await connector.probe(_TARGET_A)
    assert result.ok is False
    assert result.reason is not None and "ConnectError" in result.reason
    assert result.latency_ms is None


@pytest.mark.asyncio
async def test_probe_falls_back_to_healthz_on_readyz_404() -> None:
    """Legacy clusters without ``/readyz`` succeed via the ``/healthz`` fallback."""
    connector = _make_connector_with_stub_kubeconfig()
    stub = _FakeAsyncClient(status_by_url={"/readyz": 404, "/healthz": 200})
    with patch(
        "meho_backplane.connectors.kubernetes.connector.httpx.AsyncClient",
        return_value=stub,
    ):
        result = await connector.probe(_TARGET_A)
    assert result.ok is True
    assert [c.rsplit(":", 1)[1].split("/", 1)[1] for c in stub.calls] == ["readyz", "healthz"]


@pytest.mark.asyncio
async def test_probe_reports_healthz_in_reason_when_both_endpoints_fail() -> None:
    """Both endpoints non-OK → reason names ``/healthz`` (the last call)."""
    connector = _make_connector_with_stub_kubeconfig()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.httpx.AsyncClient",
        return_value=_FakeAsyncClient(status_by_url={"/readyz": 404, "/healthz": 503}),
    ):
        result = await connector.probe(_TARGET_A)
    assert result.ok is False
    assert result.reason is not None
    assert "503" in result.reason
    assert "/healthz" in result.reason


# ---------------------------------------------------------------------------
# execute — skeleton dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_unknown_op_for_every_op_id() -> None:
    """Unknown op_ids surface the dispatcher's structured ``unknown_op`` shape.

    Post-G0.6 (#391) the connector's ``execute`` is a thin shim that
    delegates to a global ``endpoint_descriptor`` lookup; this test
    never calls ``register_operations()``, so the DB row set is empty
    and any op_id -- including ones T3+ have since registered
    elsewhere -- hits
    :func:`~meho_backplane.operations._errors.result_unknown_op` and
    returns the dispatcher's canonical error envelope rather than the
    pre-refactor hard-coded ``{"known_ops": []}`` shape.
    """
    connector = _make_connector_with_stub_kubeconfig()
    result = await connector.execute(_TARGET_A, "k8s.pod.list", {"namespace": "argocd"})
    assert isinstance(result, OperationResult)
    assert result.status == "error"
    assert result.op_id == "k8s.pod.list"
    assert result.error is not None and result.error.startswith("unknown_op:")
    # Dispatcher's error envelope carries ``error_code`` + a numeric
    # ``known_op_count`` (the count of *enabled* descriptors for the
    # connector's natural-key triple) so callers can show a
    # "did-you-mean" hint without enumerating every op.
    assert result.extras.get("error_code") == "unknown_op"
    assert isinstance(result.extras.get("known_op_count"), int)


# ---------------------------------------------------------------------------
# _get_api_client — per-target caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_client_reused_for_same_target() -> None:
    loader_calls: list[str] = []

    async def _loader(target: KubernetesTargetLike) -> dict[str, Any]:
        loader_calls.append(target.name)
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    ) as factory:
        c1 = await connector._get_api_client(_TARGET_A)
        c2 = await connector._get_api_client(_TARGET_A)

    assert c1 is c2
    assert factory.call_count == 1
    assert loader_calls == ["rke2-meho"]


@pytest.mark.asyncio
async def test_api_clients_per_target_are_distinct() -> None:
    async def _loader(target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)

    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        side_effect=lambda _d: MagicMock(close=AsyncMock()),
    ):
        c_a = await connector._get_api_client(_TARGET_A)
        c_b = await connector._get_api_client(_TARGET_B)

    assert c_a is not c_b


@pytest.mark.asyncio
async def test_api_client_cache_key_is_secret_ref_not_name() -> None:
    """Two tenants holding same-named targets get distinct ApiClients.

    Locks in the forward-compat fix for G0.3 tenant-scoped target name
    uniqueness — keying on ``target.name`` alone would silently
    cross-pollinate ApiClients across tenants. ``secret_ref`` is the
    operator's chosen globally-unique Vault path.
    """
    tenant_a = _StubTarget(
        name="rke2-meho", host="t-a.test", port=6443, secret_ref="kv/data/tenant-a/k8s"
    )
    tenant_b = _StubTarget(
        name="rke2-meho", host="t-b.test", port=6443, secret_ref="kv/data/tenant-b/k8s"
    )

    async def _loader(target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        side_effect=lambda _d: MagicMock(close=AsyncMock()),
    ) as factory:
        c_a = await connector._get_api_client(tenant_a)
        c_b = await connector._get_api_client(tenant_b)

    assert c_a is not c_b
    assert factory.call_count == 2
    assert set(connector._api_clients.keys()) == {
        tenant_a.secret_ref,
        tenant_b.secret_ref,
    }


@pytest.mark.asyncio
async def test_aclose_closes_every_cached_client_and_clears_cache() -> None:
    async def _loader(target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    client_a = MagicMock(close=AsyncMock())
    client_b = MagicMock(close=AsyncMock())

    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        side_effect=[client_a, client_b],
    ):
        await connector._get_api_client(_TARGET_A)
        await connector._get_api_client(_TARGET_B)

    await connector.aclose()

    client_a.close.assert_awaited_once()
    client_b.close.assert_awaited_once()
    assert connector._api_clients == {}

    # Idempotent: a second aclose is a no-op (no extra close calls).
    await connector.aclose()
    assert client_a.close.await_count == 1


# ---------------------------------------------------------------------------
# parse_kubeconfig_yaml
# ---------------------------------------------------------------------------


def test_parse_kubeconfig_yaml_round_trips_a_real_kubeconfig() -> None:
    text = """
apiVersion: v1
kind: Config
current-context: default
contexts:
- name: default
  context: {cluster: c1, user: u1}
clusters:
- name: c1
  cluster: {server: 'https://k8s.test:6443'}
users:
- name: u1
  user: {token: stub-token}
"""
    parsed = parse_kubeconfig_yaml(text)
    assert parsed["apiVersion"] == "v1"
    assert parsed["current-context"] == "default"
    assert parsed["clusters"][0]["cluster"]["server"] == "https://k8s.test:6443"


@pytest.mark.parametrize("malformed", ["", "  ", "just a string", "42"])
def test_parse_kubeconfig_yaml_rejects_non_mapping(malformed: str) -> None:
    with pytest.raises(ValueError, match="must parse to a mapping"):
        parse_kubeconfig_yaml(malformed)


@pytest.mark.parametrize(
    "broken_yaml",
    [
        "invalid: [unclosed",
        "key: 'unterminated",
        "{bad: [mismatched}",
    ],
)
def test_parse_kubeconfig_yaml_normalises_parser_errors_to_value_error(
    broken_yaml: str,
) -> None:
    """``yaml.YAMLError`` (parser/scanner) is re-raised as ``ValueError``."""
    with pytest.raises(ValueError, match="failed to parse"):
        parse_kubeconfig_yaml(broken_yaml)
