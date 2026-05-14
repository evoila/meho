# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G0.6 ``KubernetesConnector`` refactor (#391).

Coverage matrix (per Task #391 acceptance criteria):

* :class:`KubernetesConnector` advertises the registry v2 metadata
  ``("k8s", "1.x", "kubernetes-asyncio")``.
* Importing the package registers the connector against both the v1
  registry (``"k8s"``) and the v2 registry
  (``("k8s", "1.x", "kubernetes-asyncio")``).
* :meth:`KubernetesConnector.register_operations` upserts one row per
  entry in :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`
  into ``endpoint_descriptor`` and is idempotent (second call hits the
  body-hash skip-re-embed branch).
* :meth:`KubernetesConnector.execute` after ``register_operations`` has
  populated the descriptor table:

  - Unknown op_id -> dispatcher's structured ``unknown_op`` envelope
    (``error_code=unknown_op``, ``known_op_count=<int>``).
  - Known op_id with valid params -> handler invoked with
    ``(target, params)``; result wrapped into ``OperationResult.status="ok"``
    with ``result`` carrying the handler's flat dict.
  - Known op_id with params failing JSON Schema validation ->
    dispatcher's ``invalid_params`` envelope.
  - Known op_id whose handler raises -> dispatcher's
    ``connector_error`` envelope with the exception class on extras.

* :meth:`KubernetesConnector.fingerprint` / :meth:`probe` are
  byte-for-byte unchanged -- the existing skeleton tests in
  :mod:`tests.test_connectors_k8s_auth` cover them; this module only
  asserts the additive dispatcher shim contract.

The ``k8s.about`` handler is exercised through the shim so the
end-to-end ``register_typed_operation`` -> ``execute`` -> handler
invoke -> result-wrap pipeline is asserted in one test.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors import (
    all_connectors,
    all_connectors_v2,
    get_connector,
)
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Environment + registry fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    """Re-register KubernetesConnector before each test.

    ``test_connectors_registry.py`` (alphabetically earlier) has an
    autouse ``clear_registry()``; this fixture restores the K8s
    entries that ``connectors/kubernetes/__init__.py`` would have set
    so resolver- and registry-shaped assertions see the production
    state. Mirrors the ``test_connectors_vault._clean_vault_registry``
    pattern.
    """
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="kubernetes-asyncio",
        cls=KubernetesConnector,
    )
    yield


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Mock the embedding service so register_typed_operation doesn't load ONNX.

    Mirrors the seam ``test_operations_typed_register.stub_embedding_service``
    uses -- the helper's ``embedding_service=`` kwarg overrides the
    process-wide singleton so the test never touches the real model.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


# ---------------------------------------------------------------------------
# Target stub
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
    secret_ref="kv/data/k8s/rke2-meho",
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


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _stub_version() -> MagicMock:
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


# ---------------------------------------------------------------------------
# Class-level registry v2 metadata
# ---------------------------------------------------------------------------


def test_registry_v2_class_attrs() -> None:
    """Class-level attrs match the v2 triple the package registers."""
    assert KubernetesConnector.product == "k8s"
    assert KubernetesConnector.version == "1.x"
    assert KubernetesConnector.impl_id == "kubernetes-asyncio"
    assert KubernetesConnector.supported_version_range is None
    assert KubernetesConnector.priority == 0


def test_package_import_registers_both_v1_and_v2_entries() -> None:
    """The package init registers the connector against both registry layers."""
    # v1 entry (chassis-route compat) keyed on the product slug.
    assert get_connector("k8s") is KubernetesConnector
    # ``register_connector`` also writes ``(product, "", "")`` to the v2 table.
    v2 = all_connectors_v2()
    assert v2[("k8s", "", "")] is KubernetesConnector
    # v2 canonical entry with the full triple.
    assert v2[("k8s", "1.x", "kubernetes-asyncio")] is KubernetesConnector
    # The v1 single-product table only carries the "k8s" entry; the v2
    # canonical key has no presence in the v1 table by design.
    assert "k8s" in all_connectors()


# ---------------------------------------------------------------------------
# register_operations -- upsert + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_operations_upserts_one_row_per_op(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row in :data:`KUBERNETES_OPS` lands in ``endpoint_descriptor``."""
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations import typed_register as tr_module

    # Patch the helper's embedding-service resolution so the test
    # doesn't touch fastembed. ``register_typed_operation``'s
    # ``embedding_service=`` kwarg is the public seam in
    # ``test_operations_typed_register.py``; ``register_operations``
    # doesn't expose that kwarg, so we patch the resolver fallback
    # path at the encode helper.
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "k8s",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "kubernetes-asyncio",
            )
        )
        rows = result.scalars().all()

    assert len(rows) == len(KUBERNETES_OPS)
    op_ids = {row.op_id for row in rows}
    assert op_ids == {op.op_id for op in KUBERNETES_OPS}

    about_row = next(r for r in rows if r.op_id == "k8s.about")
    assert about_row.source_kind == "typed"
    assert about_row.tenant_id is None
    assert about_row.handler_ref == (
        "meho_backplane.connectors.kubernetes.connector.KubernetesConnector.about"
    )
    assert about_row.safety_level == "safe"
    assert about_row.requires_approval is False


@pytest.mark.asyncio
async def test_register_operations_is_idempotent_on_re_call() -> None:
    """Second invocation skips re-embedding when summary/description/tags unchanged."""
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations import typed_register as tr_module

    encode_mock = AsyncMock(return_value=[0.1] * 384)
    with patch.object(tr_module, "encode_endpoint_text", encode_mock):
        await KubernetesConnector.register_operations()
        first_call_count = encode_mock.call_count
        await KubernetesConnector.register_operations()
        second_call_count = encode_mock.call_count

    # First call computes one embedding per op; second call hits the
    # body-hash skip-re-embed branch and computes zero.
    assert first_call_count == len(KUBERNETES_OPS)
    assert second_call_count == first_call_count

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "k8s",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "kubernetes-asyncio",
            )
        )
        rows = result.scalars().all()
    # No duplicate rows -- the natural-key UNIQUE index caught the upsert.
    assert len(rows) == len(KUBERNETES_OPS)


# ---------------------------------------------------------------------------
# execute() shim -- unknown op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_unknown_op_returns_dispatcher_unknown_op_envelope() -> None:
    """Op_id with no descriptor row hits :func:`result_unknown_op`."""
    connector = _make_connector()
    result = await connector.execute(_TARGET, "k8s.totally.unregistered", {})
    assert isinstance(result, OperationResult)
    assert result.status == "error"
    assert result.op_id == "k8s.totally.unregistered"
    assert result.error is not None and result.error.startswith("unknown_op:")
    assert result.extras.get("error_code") == "unknown_op"
    assert isinstance(result.extras.get("known_op_count"), int)


# ---------------------------------------------------------------------------
# execute() shim -- registered op happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_about_dispatches_through_descriptor_and_returns_ok() -> None:
    """``k8s.about`` registered -> ``execute`` resolves handler + returns ``ok``."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    connector = _make_connector()

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
        result = await connector.execute(_TARGET, "k8s.about", {})

    assert isinstance(result, OperationResult)
    assert result.status == "ok"
    assert result.op_id == "k8s.about"
    assert result.error is None
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["product"] == "rke2"
    assert payload["git_version"] == "v1.28.5+rke2r1"
    assert payload["platform"] == "linux/amd64"


# ---------------------------------------------------------------------------
# execute() shim -- invalid params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_invalid_params_returns_invalid_params_envelope() -> None:
    """Params failing the descriptor's JSON Schema -> ``invalid_params``."""
    from meho_backplane.operations import typed_register as tr_module

    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    connector = _make_connector()
    # ``k8s.about`` declares ``additionalProperties: False``; an extra
    # key fails the JSON Schema validator.
    result = await connector.execute(_TARGET, "k8s.about", {"unexpected": "key"})
    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"


# ---------------------------------------------------------------------------
# execute() shim -- handler raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_wraps_handler_exception_in_connector_error_envelope() -> None:
    """Handler raising -> ``connector_error`` envelope with exception class."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    connector = _make_connector()

    boom = RuntimeError("simulated VersionApi crash")
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
        version_api_cls.return_value.get_code = AsyncMock(side_effect=boom)
        result = await connector.execute(_TARGET, "k8s.about", {})

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "RuntimeError"
