# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression coverage for curated typed-connector group metadata.

G0.9-T4b (#732) curated per-group ``when_to_use`` strings on every
shipped typed connector (k8s / vault / vmware-rest composites) so the
``list_operation_groups`` meta-tool surfaces agent-actionable group
selectors rather than the auto-derived template placeholders the v0.6
substrate shipped with (``"Operations grouped under 'kv' for vault
vault."``). The companion structural Task T4a (#731) will remove the
auto-derive default outright; this module's regressions guard against
a future connector reintroducing the placeholder shape without that
removal in place.

Coverage matrix:

* Running each typed connector's registrar lands one
  :class:`~meho_backplane.db.models.OperationGroup` row per declared
  group.
* Every row's ``when_to_use`` column is non-empty and does **not**
  contain the substring ``"Operations grouped under"`` -- the
  template-literal shape T4a is killing.
* Re-running the same registrar with a different curated string
  iterates the existing row (curation is not first-write-wins);
  asserted against an in-memory tweak so the test doesn't depend on
  curating happening across multiple PRs.

The tests reuse the project's autouse SQLite engine plus a per-test
embedding-service stub so neither the real ONNX model nor a network
dep is loaded -- same pattern as
``test_connectors_vmware_rest_composites_register.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Final
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.connectors.vault.ops_sys import register_vault_sys_typed_operations
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.typed_register import (
    _TYPED_OP_REGISTRARS,
    register_typed_operation,
)
from meho_backplane.settings import get_settings

# Expected group_key sets per shipped typed connector. Updating this
# table when a new group lands is the load-bearing knob -- the
# regression test below reads these to assert one OperationGroup row
# per declared group lands with a curated when_to_use.
_K8S_GROUPS: Final[frozenset[str]] = frozenset(
    {"cluster", "inventory", "workload", "network", "config", "events", "logs"}
)
_VAULT_GROUPS: Final[frozenset[str]] = frozenset({"auth", "kv", "sys"})
_VMWARE_COMPOSITE_GROUPS: Final[frozenset[str]] = frozenset(
    {"cluster", "events", "performance", "storage", "networking", "vm", "host"}
)

#: The substring that signals the auto-derive default in
#: :func:`~meho_backplane.operations.typed_register._resolve_or_create_group`.
#: Any group row carrying this substring proves a connector regressed
#: to the auto-derive path.
_PLACEHOLDER_SUBSTRING: Final[str] = "Operations grouped under"


async def _sample_curation_handler(target: object, params: dict[str, object]) -> dict[str, object]:
    """Module-level async handler so ``derive_handler_ref`` accepts it.

    Lives at module scope (not nested inside the test body) because
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    rejects closures / lambdas / partials via
    :func:`~meho_backplane.operations.typed_register.derive_handler_ref`
    -- the dispatcher resolves handler_ref via importlib + getattr at
    dispatch time and that round-trip only works for module-level
    callables.
    """
    del target, params
    return {}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Snapshot + restore the global registrar list and connector registry.

    Mirrors the discipline in
    ``test_connectors_vmware_rest_composites_register.py``: the registrar
    list is a process-global the lifespan iterates, and a registrar
    re-importing one connector would otherwise truncate the list for
    later tests.
    """
    saved_registrars = list(_TYPED_OP_REGISTRARS)
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _TYPED_OP_REGISTRARS[:] = saved_registrars


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registrations don't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _operation_groups_for(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
) -> dict[str, OperationGroup]:
    """Return ``{group_key: OperationGroup}`` for one connector's built-in groups."""
    rows = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.tenant_id.is_(None),
                    OperationGroup.product == product,
                    OperationGroup.version == version,
                    OperationGroup.impl_id == impl_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.group_key: row for row in rows}


@pytest.mark.asyncio
async def test_kubernetes_groups_have_curated_when_to_use(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Every K8s group registers with a curated ``when_to_use`` string.

    Asserts the seven expected group_keys land and none of them carry
    the auto-derive template-literal shape.
    """
    # Patch the singleton-resolving embedding-service hop without
    # touching the public registrar signature: KubernetesConnector's
    # registrar doesn't forward an embedding_service kwarg (#475
    # left it as a v0.2.next refinement), so we stub the embedding
    # path at the helper level.
    from meho_backplane.operations import typed_register as tr

    original = tr.encode_endpoint_text

    async def _stub_encode(text: str, *, service: object | None = None) -> list[float]:
        return await stub_embedding_service.encode_one(text)

    tr.encode_endpoint_text = _stub_encode  # type: ignore[assignment]
    try:
        await KubernetesConnector.register_operations()
    finally:
        tr.encode_endpoint_text = original  # type: ignore[assignment]

    groups = await _operation_groups_for(
        session, product="k8s", version="1.x", impl_id="kubernetes-asyncio"
    )
    assert set(groups) == _K8S_GROUPS, (
        f"k8s groups in DB {set(groups)!r} != expected {_K8S_GROUPS!r}"
    )
    for key, row in groups.items():
        assert row.when_to_use.strip(), f"k8s group {key!r} has empty when_to_use"
        assert _PLACEHOLDER_SUBSTRING not in row.when_to_use, (
            f"k8s group {key!r} regressed to the auto-derive template: {row.when_to_use!r}"
        )


@pytest.mark.asyncio
async def test_vault_groups_have_curated_when_to_use(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Every Vault group (auth / kv / sys) registers with curated prose."""
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)

    groups = await _operation_groups_for(session, product="vault", version="1.x", impl_id="vault")
    assert set(groups) == _VAULT_GROUPS, (
        f"vault groups in DB {set(groups)!r} != expected {_VAULT_GROUPS!r}"
    )
    for key, row in groups.items():
        assert row.when_to_use.strip(), f"vault group {key!r} has empty when_to_use"
        assert _PLACEHOLDER_SUBSTRING not in row.when_to_use, (
            f"vault group {key!r} regressed to the auto-derive template: {row.when_to_use!r}"
        )


@pytest.mark.asyncio
async def test_vmware_composite_groups_have_curated_when_to_use(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Every vmware-rest composite group registers with curated prose."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)

    groups = await _operation_groups_for(
        session, product="vmware", version="9.0", impl_id="vmware-rest"
    )
    assert set(groups) == _VMWARE_COMPOSITE_GROUPS, (
        f"vmware composite groups in DB {set(groups)!r} != expected {_VMWARE_COMPOSITE_GROUPS!r}"
    )
    for key, row in groups.items():
        assert row.when_to_use.strip(), f"vmware composite group {key!r} has empty when_to_use"
        assert _PLACEHOLDER_SUBSTRING not in row.when_to_use, (
            f"vmware composite group {key!r} regressed to the auto-derive "
            f"template: {row.when_to_use!r}"
        )


@pytest.mark.asyncio
async def test_when_to_use_updates_when_text_changes(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Re-registering with a different ``when_to_use`` updates the row.

    Without this behaviour curation can never iterate -- a per-group
    string edit in code never reaches the DB row that
    ``list_operation_groups`` reads. The
    :func:`~meho_backplane.operations.typed_register._resolve_or_create_group`
    body's existing-row branch is the load-bearing check.
    """

    await register_typed_operation(
        product="meho-test",
        version="0.0",
        impl_id="curation",
        op_id="meho-test.curation.op",
        handler=_sample_curation_handler,
        summary="Test op for curation iteration.",
        description="Test op for curation iteration.",
        parameter_schema={"type": "object", "additionalProperties": False},
        group_key="probe",
        when_to_use="First-write string.",
        embedding_service=stub_embedding_service,
    )
    await register_typed_operation(
        product="meho-test",
        version="0.0",
        impl_id="curation",
        op_id="meho-test.curation.op",
        handler=_sample_curation_handler,
        summary="Test op for curation iteration.",
        description="Test op for curation iteration.",
        parameter_schema={"type": "object", "additionalProperties": False},
        group_key="probe",
        when_to_use="Second-write string (curated follow-up).",
        embedding_service=stub_embedding_service,
    )

    groups = await _operation_groups_for(
        session, product="meho-test", version="0.0", impl_id="curation"
    )
    assert set(groups) == {"probe"}
    assert groups["probe"].when_to_use == "Second-write string (curated follow-up)."
