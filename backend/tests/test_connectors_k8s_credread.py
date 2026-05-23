# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the k8s live kubeconfig-read chain (G3.10-T4 #948).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) kubeconfig loader:

    dispatch(...)
      -> _resolve_connector_instance -> KubernetesConnector()    # default loader
      -> dispatch_typed -> handler                               # operator-aware
      -> connector._get_api_client(target, operator)
      -> load_kubeconfig_from_vault(target, operator)            # the live loader
      -> vault_client_for_operator(operator)                     # operator-context login
      -> read_secret_version("...", field="kubeconfig")          # KV-v2 read
      -> parse_kubeconfig_yaml                                   # YAML -> dict
      -> kubernetes_asyncio.config.new_client_from_config_dict
      -> CoreV1Api.list_namespace                                # mocked out
      -> OperationResult(status="ok")

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against a canned kubeconfig
  YAML. The default
  :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`
  is exercised verbatim — *not* an injected stub. This is the
  load-bearing difference from
  :mod:`tests.test_connectors_k8s_auth` (which injects a stub loader):
  here the connector is constructed by the resolver as
  ``KubernetesConnector()`` with no ``kubeconfig_loader``, so the
  default loader runs.
* **kubernetes_asyncio** —
  ``config.new_client_from_config_dict`` and ``CoreV1Api.list_namespace``
  are mocked so the parsed kubeconfig never reaches a real cluster.
  No network. The kubeconfig YAML in the Vault fake is the canary the
  no-leak assertions grep against.

The live k3d + live-Vault E2E (a real Vault dev container seeded with a
real k3s kubeconfig) is the env-gated opt-in in
:mod:`tests.integration.test_connectors_k8s_live_vault`; it skips
cleanly in the default sweep.

Why this lives in the unit lane (not ``tests/integration``)
===========================================================

The acceptance criterion requires this E2E to run in the default
``pytest -n auto`` sweep — which deselects ``tests/integration`` (the
container/PG lane). The recorded-fixture replay needs neither a real
Vault nor a real k3s (both stubbed at their boundaries) and only the
autouse SQLite ``endpoint_descriptor`` schema the top-level conftest
already migrates per worker. So it belongs here, mirroring the
:mod:`tests.test_connectors_vmware_rest_credread` precedent verbatim
(G3.9-T3 #942).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary kubeconfig values — asserted to NEVER appear in the result or logs.
# Generated nowhere near a real cluster; these strings are the leak canaries.
# The kubeconfig's server URL + client token are the load-bearing secret
# fields: the server URL gives a remote attacker the API endpoint, and a
# bearer token is a direct credential. Both MUST stay out of every log
# event, OperationResult, and broadcast payload.
# ---------------------------------------------------------------------------

_CANARY_SERVER_URL = "https://k8s-credread-canary.test.invalid:6443"
_CANARY_BEARER_TOKEN = "k8s-canary-bearer-must-not-leak-credread"
_CANARY_CLIENT_CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBANARYCERTIFICATEBLOBfMUSTNOTLEAK\n"
    "credread-k8s-cert-pem-canary==\n"
    "-----END CERTIFICATE-----"
)

#: A kubeconfig YAML seeded into the Vault fake. The server URL +
#: bearer token + client-cert are the canary fields the no-leak
#: assertions grep against.
_CANARY_KUBECONFIG_YAML = f"""apiVersion: v1
kind: Config
current-context: default
contexts:
- name: default
  context: {{cluster: c1, user: u1}}
clusters:
- name: c1
  cluster:
    server: {_CANARY_SERVER_URL}
    certificate-authority-data: {_CANARY_CLIENT_CERT_PEM!r}
users:
- name: u1
  user:
    token: {_CANARY_BEARER_TOKEN}
"""

#: The connector triple ``k8s-1.x`` decodes to.
_PRODUCT = "k8s"
_VERSION = "1.x"
_IMPL_ID = "k8s"
_CONNECTOR_ID = "k8s-1.x"

#: A typed read op. ``k8s.namespace.list`` was wired in G3.2-T2 and
#: takes empty params; the handler reaches CoreV1Api.list_namespace,
#: which is mocked at the boundary so no real cluster is touched.
_OP_ID = "k8s.namespace.list"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
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


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test.

    The connector-instance cache must be clear so the resolver builds a
    fresh ``KubernetesConnector()`` (default loader) for this test
    rather than reusing one a sibling test wired with an injected stub
    loader. The pass-through reducer is the v0.2 default; set explicitly
    so the dispatcher returns the handler's dict verbatim.
    """
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=KubernetesConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub for the seeded descriptor row."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record broadcast events so the audit/broadcast leg is asserted."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _CredReadTarget:
    """Target satisfying both ``KubernetesTargetLike`` and the resolver shape.

    Carries the resolver attributes (``product`` / ``fingerprint`` /
    ``preferred_impl_id`` / ``id``) plus the k8s-loader attributes
    (``name`` / ``host`` / ``port`` / ``secret_ref``). ``secret_ref`` is
    the **logical** KV-v2 path the live default loader reads — relative
    to the mount root, no ``secret/`` prefix and no ``/data/`` segment
    (hvac inserts ``/data/`` itself). This is the exact shape an operator
    stores per the kubeconfig-in-Vault convention documented in
    ``docs/cross-repo/kubernetes-onboarding.md``.
    """

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": "1.32.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "k8s-credread"
        self.host = "k8s-credread.test.invalid"
        self.port = 6443
        self.secret_ref = "targets/op-credread/k8s-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread",
        name="Cred Read Operator",
        email=None,
        raw_jwt="op.credread.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_typed_descriptor(session: AsyncSession, embedding: list[float]) -> None:
    """Insert one enabled typed descriptor for ``k8s.namespace.list``.

    Points at the canonical bound-method handler shape — the
    dispatcher's resolver finds the connector class via the v2 registry,
    rebinds the unbound method against the resolver-built instance, and
    invokes ``handler(operator=..., target=..., params=...)`` because
    the bound method names ``operator`` in its signature.
    """
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        source_kind="typed",
        method=None,
        path=None,
        handler_ref=(
            "meho_backplane.connectors.kubernetes.connector.KubernetesConnector.k8s_namespace_list"
        ),
        summary="List namespaces",
        description="List Kubernetes namespaces — one row per namespace.",
        tags=["inventory"],
        parameter_schema={"type": "object", "additionalProperties": False, "properties": {}},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()


def _stub_namespace_list_response() -> Any:
    """Build a CoreV1Api.list_namespace stub response with two namespaces.

    The handler projects each :class:`V1Namespace` through
    :func:`~meho_backplane.connectors.kubernetes.ops_core.namespace_row`,
    which reads ``metadata.name`` / ``metadata.creation_timestamp`` /
    ``metadata.labels`` / ``status.phase``. Use ``MagicMock`` for the
    namespace items (sufficient for the test seam — the helper is
    unit-tested directly against real V1Namespace instances elsewhere).
    """
    response = MagicMock()
    ns1 = MagicMock()
    ns1.metadata.name = "default"
    ns1.metadata.creation_timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    ns1.metadata.labels = {}
    ns1.status.phase = "Active"
    ns2 = MagicMock()
    ns2.metadata.name = "kube-system"
    ns2.metadata.creation_timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    ns2.metadata.labels = {}
    ns2.status.phase = "Active"
    response.items = [ns1, ns2]
    return response


@pytest.mark.asyncio
async def test_dispatch_executes_full_credread_chain_returns_ok(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full dispatch->loader->kubeconfig->handler chain returns status="ok"."""
    await _seed_typed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary kubeconfig via
    # the real ``load_kubeconfig_from_vault`` ->
    # ``vault_client_for_operator`` path. No kubeconfig_loader is
    # injected anywhere — the resolver builds ``KubernetesConnector()``
    # so the default loader runs verbatim.
    fake = install_fake_client(monkeypatch, secret={"kubeconfig": _CANARY_KUBECONFIG_YAML})

    target = _CredReadTarget()
    operator = _make_operator()

    # Mock ``config.new_client_from_config_dict`` so the parsed kubeconfig
    # never reaches a real cluster — and ``CoreV1Api.list_namespace`` so
    # the handler returns a deterministic response.
    api_client_mock = MagicMock(close=AsyncMock())
    core_v1_mock = MagicMock()
    core_v1_mock.list_namespace = AsyncMock(return_value=_stub_namespace_list_response())

    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=api_client_mock,
        ) as config_factory,
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.CoreV1Api",
            return_value=core_v1_mock,
        ),
    ):
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    # The load-bearing assertion: the chain executed end to end.
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["total"] == 2
    assert {row["name"] for row in result.result["rows"]} == {"default", "kube-system"}

    # The default loader actually read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref

    # The parsed kubeconfig dict was actually built and passed to k_a.
    assert config_factory.call_count == 1
    forwarded_kubeconfig = config_factory.call_args.args[0]
    assert isinstance(forwarded_kubeconfig, dict)
    assert forwarded_kubeconfig["apiVersion"] == "v1"

    # The handler ran against the mocked CoreV1Api.
    core_v1_mock.list_namespace.assert_awaited_once()

    # One audit + one broadcast for the dispatched op.
    assert len(captured_events) == 1


@pytest.mark.asyncio
async def test_credread_chain_never_leaks_kubeconfig_in_result_or_logs(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No kubeconfig content rides the OperationResult or any captured log event.

    The kubeconfig YAML in the Vault fake carries three canary fields
    (server URL, bearer token, client cert PEM); each is asserted absent
    from the dispatch result, every structlog event, and the broadcast
    payload. The kubeconfig is treated as ephemeral in-memory state —
    same discipline the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper enforces for the REST connectors.
    """
    await _seed_typed_descriptor(session, stub_embedding_service.encode_one.return_value)

    install_fake_client(monkeypatch, secret={"kubeconfig": _CANARY_KUBECONFIG_YAML})

    target = _CredReadTarget()
    operator = _make_operator()

    api_client_mock = MagicMock(close=AsyncMock())
    core_v1_mock = MagicMock()
    core_v1_mock.list_namespace = AsyncMock(return_value=_stub_namespace_list_response())

    with (
        capture_logs() as captured,
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=api_client_mock,
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.CoreV1Api",
            return_value=core_v1_mock,
        ),
    ):
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    assert result.status == "ok", result.error

    # The kubeconfig never rides the OperationResult (result, error, or
    # any extras the envelope carries). All three canary fields checked
    # independently so a partial leak surfaces with a clear name.
    result_blob = repr(result)
    assert _CANARY_SERVER_URL not in result_blob, "server URL leaked into OperationResult"
    assert _CANARY_BEARER_TOKEN not in result_blob, "bearer token leaked into OperationResult"
    assert _CANARY_CLIENT_CERT_PEM not in result_blob, "client cert PEM leaked into OperationResult"

    # The kubeconfig never rides any structlog event (loader, connector,
    # dispatcher, audit, broadcast).
    log_blob = repr(captured)
    assert _CANARY_SERVER_URL not in log_blob, "server URL leaked into structlog events"
    assert _CANARY_BEARER_TOKEN not in log_blob, "bearer token leaked into structlog events"
    assert _CANARY_CLIENT_CERT_PEM not in log_blob, "client cert PEM leaked into structlog events"

    # The kubeconfig never rides the broadcast event payload either.
    events_blob = repr(captured_events)
    assert _CANARY_SERVER_URL not in events_blob, "server URL leaked into broadcast events"
    assert _CANARY_BEARER_TOKEN not in events_blob, "bearer token leaked into broadcast events"
    assert _CANARY_CLIENT_CERT_PEM not in events_blob, (
        "client cert PEM leaked into broadcast events"
    )
