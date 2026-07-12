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

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import Connector
from meho_backplane.connectors._shared import credential_backend as cb
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
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

from ._vault_fakes import install_fake_client


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires.

    The G0.6 refactor (#391) made ``execute`` hit the descriptor table
    via the DB engine, so the lookup path needs ``Settings`` to
    construct -- which requires the three Keycloak/Vault env vars.
    Fingerprint / probe tests don't touch the DB but the fixture is
    autouse so all tests in the module see a consistent env. The Vault
    JWT/OIDC vars are needed too because G3.10-T4 (#948) wired the
    default kubeconfig loader against ``vault_client_for_operator``,
    which reads these on every call.
    """
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
    secret_ref="k8s/rke2-meho",
)
_TARGET_B = _StubTarget(
    name="rke2-infra",
    host="rke2-infra.test.invalid",
    port=None,
    secret_ref="k8s/rke2-infra",
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


def _make_operator(*, raw_jwt: str = "op.test.jwt") -> Operator:
    """Build a non-system operator with a non-empty ``raw_jwt``.

    The empty-``raw_jwt`` case is the fail-closed system-call carve-out;
    tests that exercise it pass ``raw_jwt=""`` explicitly.
    :func:`synthesise_system_operator` now carries a non-empty placeholder
    JWT (per G3.10 hygiene -- the cache fast-path's defense-in-depth
    empty-jwt guard) so it is not a substitute for an explicit empty
    ``raw_jwt`` operator.
    """
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=__import__("uuid").UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_connector_with_stub_kubeconfig() -> KubernetesConnector:
    """Build a connector whose loader returns the stub kubeconfig dict.

    The injected loader accepts the same ``(target, operator)`` pair the
    production default
    :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`
    accepts (the G3.10-T4 #948 contract) so the wiring is exercised
    end-to-end through the test seam as well.
    """

    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del operator  # accepted by signature; stub doesn't need it
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


def test_default_loader_fails_closed_on_empty_operator_jwt() -> None:
    """The default loader fails closed on the system-call carve-out (no JWT).

    Replaces the pre-G3.10-T4 (#948) ``deliberate stub`` test: the loader
    is now live (reads Vault under the operator's identity) but still
    fails closed when ``operator.raw_jwt`` is empty — the same fail-closed
    boundary :func:`load_basic_credentials` enforces. The Vault leaf is
    never reached on this path; the guard runs before
    :func:`vault_client_for_operator` is touched.

    Uses an explicit empty-``raw_jwt`` operator rather than
    :func:`synthesise_system_operator` (which now carries a non-empty
    placeholder JWT per G3.10 hygiene -- the cache fast-path's
    defense-in-depth empty-jwt guard would otherwise short-circuit every
    probe path). The empty-``raw_jwt`` constructor here is the precise
    contract under test.
    """

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match="no operator JWT"):
            await load_kubeconfig_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    asyncio.run(_check())


def test_default_loader_fails_closed_on_unset_secret_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``target.secret_ref`` raises before Vault is touched."""
    unconfigured = _StubTarget(name="unconfigured", host="x.test.invalid", port=6443, secret_ref="")

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match="no secret_ref configured"):
            await load_kubeconfig_from_vault(unconfigured, _make_operator())

    asyncio.run(_check())


def test_default_loader_returns_parsed_kubeconfig_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live loader reads the ``kubeconfig`` KV-v2 field + returns the parsed dict.

    Uses the shared in-process Vault fake (``install_fake_client``)
    seeded with a kubeconfig YAML; the loader's real
    :func:`vault_client_for_operator` + ``read_secret_version`` code
    path runs against the fake.
    """
    kubeconfig_yaml = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "current-context: default\n"
        "contexts:\n"
        "- name: default\n"
        "  context: {cluster: c1, user: u1}\n"
        "clusters:\n"
        "- name: c1\n"
        "  cluster: {server: 'https://k8s.test:6443'}\n"
        "users:\n"
        "- name: u1\n"
        "  user: {token: stub-token}\n"
    )
    fake = install_fake_client(monkeypatch, secret={"kubeconfig": kubeconfig_yaml})

    async def _check() -> None:
        operator = _make_operator(raw_jwt="op.live.jwt")
        result = await load_kubeconfig_from_vault(_TARGET_A, operator)
        assert result["apiVersion"] == "v1"
        assert result["clusters"][0]["cluster"]["server"] == "https://k8s.test:6443"
        assert result["current-context"] == "default"
        # The KV-v2 read happened under the operator's JWT (operator-context
        # Vault read — the locked Option A decision).
        assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.live.jwt"
        assert fake.secrets.kv.v2.read_calls[-1]["path"] == _TARGET_A.secret_ref

    asyncio.run(_check())


def test_default_loader_raises_on_missing_kubeconfig_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KV-v2 secret missing the ``kubeconfig`` field surfaces a clear error."""
    install_fake_client(monkeypatch, secret={"username": "wrong-shape"})

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match="missing required field 'kubeconfig'"):
            await load_kubeconfig_from_vault(_TARGET_A, _make_operator())

    asyncio.run(_check())


def test_default_loader_raises_on_non_string_kubeconfig_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``kubeconfig`` field that isn't a string surfaces a clear error.

    Defence against a misconfigured secret that stored the kubeconfig as
    a nested dict (e.g. someone wrote a parsed dict instead of the YAML
    text) — the loader rejects it cleanly rather than passing a non-string
    to :func:`parse_kubeconfig_yaml`.
    """
    install_fake_client(monkeypatch, secret={"kubeconfig": {"oops": "dict"}})

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match="expected a YAML string"):
            await load_kubeconfig_from_vault(_TARGET_A, _make_operator())

    asyncio.run(_check())


def test_default_loader_propagates_yaml_parse_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed kubeconfig YAML surfaces a :class:`ValueError` (not a bare YAMLError).

    The loader's contract is one failure path for malformed YAML — the
    :func:`parse_kubeconfig_yaml` normalisation. Callers don't need to
    import ``yaml`` just to catch parse failures.
    """
    install_fake_client(monkeypatch, secret={"kubeconfig": "key: 'unterminated"})

    async def _check() -> None:
        with pytest.raises(ValueError, match="failed to parse"):
            await load_kubeconfig_from_vault(_TARGET_A, _make_operator())

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Credential-backend seam dispatch (#2397) — the kubeconfig loader routes
# through split_credential_ref instead of reading Vault directly, so a
# gsm: ref resolves on a CREDENTIAL_BACKEND=gsm / no-Vault deployment and
# the Vault-kind API-path-shape guard now covers kubeconfig refs too.
# ---------------------------------------------------------------------------


def test_default_loader_dispatches_gsm_ref_through_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``gsm:`` secret_ref routes to the gsm backend, never a Vault read.

    Before #2397 the loader read Vault directly, so a ``gsm:`` ref was
    interpreted as a literal KV-v2 path and failed on a no-Vault deploy.
    Now it goes through
    :func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`,
    which splits the scheme and dispatches to the registered ``gsm``
    backend; this loader then pulls the ``kubeconfig`` field out of the
    returned dict and parses it. The Vault fake proves the read never
    falls through to Vault.
    """
    kubeconfig_yaml = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "current-context: default\n"
        "clusters:\n"
        "- name: c1\n"
        "  cluster: {server: 'https://gke.test:443'}\n"
    )
    # Seeded but must never be read — the gsm ref must not hit Vault.
    vault_fake = install_fake_client(monkeypatch, secret={"kubeconfig": "SHOULD-NOT-BE-READ"})

    class _FakeGsmBackend:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def load_secret_data(
            self,
            secret_ref: str,
            operator: Operator,
            *,
            target_name: str,
            mount: str,
        ) -> dict[str, object]:
            del operator, mount
            self.calls.append({"secret_ref": secret_ref, "target_name": target_name})
            return {"kubeconfig": kubeconfig_yaml}

    fake_backend = _FakeGsmBackend()
    monkeypatch.setitem(cb.CREDENTIAL_BACKEND_REGISTRY, "gsm", fake_backend)

    gsm_target = _StubTarget(
        name="gke-meho",
        host="gke-meho.test.invalid",
        port=443,
        secret_ref="gsm:my-project/gke-meho-kubeconfig#kubeconfig",
    )

    async def _check() -> None:
        result = await load_kubeconfig_from_vault(gsm_target, _make_operator())
        assert result["apiVersion"] == "v1"
        assert result["clusters"][0]["cluster"]["server"] == "https://gke.test:443"
        # Dispatched to the gsm backend with the scheme stripped.
        assert fake_backend.calls[-1]["secret_ref"] == "my-project/gke-meho-kubeconfig#kubeconfig"
        # Vault was never touched — the ref was not treated as a KV-v2 path.
        assert vault_fake.secrets.kv.v2.read_calls == []
        assert vault_fake.auth.jwt.login_calls == []

    asyncio.run(_check())


def test_default_loader_rejects_api_path_shaped_secret_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KV-v2 API-path-shaped secret_ref fails closed via the seam's guard.

    Routing through the seam means the kubeconfig loader inherits the
    Vault backend's API-path-shape guard: a ``secret/data/…``-shaped ref
    (which hvac would double-resolve to a 404) is rejected with an
    actionable error before Vault is touched — a latent defect the old
    direct-read bypass silently carried.
    """
    fake = install_fake_client(monkeypatch, secret={"kubeconfig": "apiVersion: v1"})
    bad = _StubTarget(
        name="rke2-bad",
        host="rke2-bad.test.invalid",
        port=6443,
        secret_ref="secret/data/k8s/rke2-meho",
    )

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match="API-path-shaped secret_ref"):
            await load_kubeconfig_from_vault(bad, _make_operator())
        # The guard fires before any login / read.
        assert fake.secrets.kv.v2.read_calls == []
        assert fake.auth.jwt.login_calls == []

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

    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del operator
        loader_calls.append(target.name)
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    op = _make_operator()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    ) as factory:
        c1 = await connector._get_api_client(_TARGET_A, op)
        c2 = await connector._get_api_client(_TARGET_A, op)

    assert c1 is c2
    assert factory.call_count == 1
    assert loader_calls == ["rke2-meho"]


@pytest.mark.asyncio
async def test_api_clients_per_target_are_distinct() -> None:
    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del operator
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    op = _make_operator()

    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        side_effect=lambda _d: MagicMock(close=AsyncMock()),
    ):
        c_a = await connector._get_api_client(_TARGET_A, op)
        c_b = await connector._get_api_client(_TARGET_B, op)

    assert c_a is not c_b


@pytest.mark.asyncio
async def test_api_client_cache_key_is_secret_ref_not_name() -> None:
    """Two tenants holding same-named targets get distinct ApiClients.

    Locks in the forward-compat fix for G0.3 tenant-scoped target name
    uniqueness — keying on ``target.name`` alone would silently
    cross-pollinate ApiClients across tenants. ``secret_ref`` is the
    operator's chosen globally-unique Vault path.
    """
    tenant_a = _StubTarget(name="rke2-meho", host="t-a.test", port=6443, secret_ref="tenant-a/k8s")
    tenant_b = _StubTarget(name="rke2-meho", host="t-b.test", port=6443, secret_ref="tenant-b/k8s")

    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del operator
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    op = _make_operator()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        side_effect=lambda _d: MagicMock(close=AsyncMock()),
    ) as factory:
        c_a = await connector._get_api_client(tenant_a, op)
        c_b = await connector._get_api_client(tenant_b, op)

    assert c_a is not c_b
    assert factory.call_count == 2
    assert set(connector._api_clients.keys()) == {
        tenant_a.secret_ref,
        tenant_b.secret_ref,
    }


@pytest.mark.asyncio
async def test_loader_receives_operator_and_target() -> None:
    """The injected loader receives the same ``(target, operator)`` pair the default does.

    Locks in the G3.10-T4 (#948) contract: ``KubeconfigLoader`` carries
    ``operator`` so the dispatch path threads the operator's identity to
    the kubeconfig read. An injected test loader receives the same pair.
    """
    captured: list[tuple[str, str]] = []

    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        captured.append((target.name, operator.sub))
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    op = _make_operator()
    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    ):
        await connector._get_api_client(_TARGET_A, op)

    assert captured == [(_TARGET_A.name, "op-test")]


@pytest.mark.asyncio
async def test_aclose_closes_every_cached_client_and_clears_cache() -> None:
    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del operator
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_loader)
    client_a = MagicMock(close=AsyncMock())
    client_b = MagicMock(close=AsyncMock())
    op = _make_operator()

    with patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        side_effect=[client_a, client_b],
    ):
        await connector._get_api_client(_TARGET_A, op)
        await connector._get_api_client(_TARGET_B, op)

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


# ---------------------------------------------------------------------------
# G0.16-T4 (#1306) probe-vs-dispatch convergence
# ---------------------------------------------------------------------------


def test_fingerprint_with_route_operator_forwards_to_kubeconfig_loader() -> None:
    """``fingerprint(target, operator=<route op>)`` forwards the operator
    to the injected ``KubeconfigLoader`` verbatim — back-to-back with a
    dispatch-style call, both legs see the same identity.

    The v0.8.0 dogfood cycle (#1302 + ``claude-rdc-hetzner-dc#771``)
    surfaced ``vault OIDC malformed jwt: must have three parts`` on
    probe for K8s targets while dispatch worked. The bug: probe
    hard-coded the synthesised system operator (placeholder JWT) on
    the way into :func:`load_kubeconfig_from_vault`; dispatch passed
    the real route operator. Vault rejected the placeholder before the
    Vault KV-v2 read could fire.

    This test pins the convergence shape: when the probe route forwards
    its operator (the post-#1306 wiring), the kubeconfig loader sees
    that exact operator — not the system-operator stand-in — and the
    forwarded JWT has the compact-JWS shape (≥3 dot-separated parts,
    the issue body's acceptance criterion 4).

    The companion legacy carve-out — ``operator=None`` falling back to
    a system operator — is covered by
    :func:`test_fingerprint_without_operator_still_synthesises_system_operator`.
    """
    captured: list[Operator] = []
    stub_kubeconfig = _stub_kubeconfig_dict()

    async def _capturing_loader(
        target: KubernetesTargetLike,
        operator: Operator,
    ) -> dict[str, Any]:
        captured.append(operator)
        return stub_kubeconfig

    connector = KubernetesConnector(kubeconfig_loader=_capturing_loader)

    # The probe-route operator that the REST + UI routes lift via
    # ``resolve_operator_or_403`` / ``require_operator``. Real
    # production tokens land here as compact-JWS strings; we use a
    # synthetic three-part stand-in so the assertion is deterministic
    # without minting a real Keycloak token.
    route_operator = _make_operator(raw_jwt="header.payload.signature")

    fake_version = MagicMock()
    fake_version.git_version = "v1.31.4+rke2r1"
    fake_version.major = "1"
    fake_version.minor = "31"
    fake_version.build_date = "2026-02-14T18:01:25Z"
    fake_version.platform = "linux/amd64"
    fake_version.go_version = "go1.22.10"
    fake_version.git_commit = "abc"
    fake_version.git_tree_state = "clean"

    async def _run() -> None:
        with (
            patch(
                "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "meho_backplane.connectors.kubernetes.connector.client.VersionApi"
            ) as mock_version_api,
        ):
            mock_version_api.return_value.get_code = AsyncMock(return_value=fake_version)
            # Probe-style call (the post-#1306 wiring: the route forwards
            # its operator). Asserts the kubeconfig loader sees the route
            # operator's exact identity.
            await connector.fingerprint(_TARGET_A, operator=route_operator)
            # Dispatch-style call (the operator-aware path that always
            # worked) for back-to-back comparison.
            await connector.about(route_operator, _TARGET_B, params={})

    asyncio.run(_run())

    # The kubeconfig loader was invoked for each distinct target's
    # cold-cache path. Both call sites flowed the same identity — the
    # convergence point #1306 promises.
    assert len(captured) == 2, (
        f"expected two cold-cache loader invocations (one per target); got {len(captured)}"
    )
    for invocation_operator in captured:
        assert invocation_operator.sub == route_operator.sub, (
            "the kubeconfig loader saw a different identity than the route operator; "
            "this is the #1306 divergence — probe and dispatch must converge on the "
            "same operator"
        )
        # Acceptance criterion 4: compact-JWS sanity check on the
        # forwarded token. A real JWT has three dot-separated parts;
        # the pre-#1306 placeholder had zero.
        assert len(invocation_operator.raw_jwt.split(".")) >= 3, (
            f"forwarded JWT does not look like a compact-JWS (got "
            f"{len(invocation_operator.raw_jwt.split('.'))} parts; expected ≥3) — "
            "Vault would reject this with ``malformed jwt: must have three parts``"
        )


def test_fingerprint_without_operator_still_synthesises_system_operator() -> None:
    """``fingerprint(target)`` without ``operator`` keeps the legacy
    system-operator fall-back, preserving the architectural posture
    that *system-initiated calls cannot perform an operator-context
    Vault read* (the locked Option A decision).

    The fall-back's purpose is the readiness probe / topology refresh
    worker that has no real operator in scope; routing a real operator
    through every existing call site is out of scope for #1306. The
    fall-back identity is the system-operator stand-in whose sub is
    ``"system:connector-probe"`` (its non-empty placeholder JWT still
    fails closed at the live Vault round-trip).
    """
    captured: list[Operator] = []

    async def _capturing_loader(
        target: KubernetesTargetLike,
        operator: Operator,
    ) -> dict[str, Any]:
        captured.append(operator)
        return _stub_kubeconfig_dict()

    connector = KubernetesConnector(kubeconfig_loader=_capturing_loader)

    fake_version = MagicMock()
    fake_version.git_version = "v1.31.4+rke2r1"
    fake_version.major = "1"
    fake_version.minor = "31"
    fake_version.build_date = "2026-02-14T18:01:25Z"
    fake_version.platform = "linux/amd64"
    fake_version.go_version = "go1.22.10"
    fake_version.git_commit = "abc"
    fake_version.git_tree_state = "clean"

    async def _run() -> None:
        with (
            patch(
                "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "meho_backplane.connectors.kubernetes.connector.client.VersionApi"
            ) as mock_version_api,
        ):
            mock_version_api.return_value.get_code = AsyncMock(return_value=fake_version)
            # No ``operator=`` kwarg → legacy fall-back.
            await connector.fingerprint(_TARGET_A)

    asyncio.run(_run())
    assert len(captured) == 1
    assert captured[0].sub == "system:connector-probe", (
        "the legacy fall-back must synthesise the system operator (sub="
        "'system:connector-probe') — the #1306 widening preserved this "
        "carve-out for callers that have no real operator in scope"
    )
