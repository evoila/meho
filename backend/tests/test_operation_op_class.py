# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every hand-written operation gets a ``read`` or a write class.

A hand-written (typed or multi-step) operation gets its class from the risk
level it states where it is registered
(:func:`~meho_backplane.broadcast.events.resolve_operation_op_class`), not
only from the last word of its op id:

* ``caution`` or higher: a write class;
* ``safe``: ``read``, except the few ``safe`` operations that make the target
  do work, which are pinned ``write``;
* the credential and audit-query classes always win.

The sweep runs every registrar MEHO runs at start-up, so a new operation is
covered as soon as it is registered. The last test writes dispatch audit rows
and checks that the ``op_class`` filter of the audit query finds them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Final

import pytest
from sqlalchemy import select

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import resolve_operation_op_class
from meho_backplane.broadcast.history import WRITE_OP_CLASSES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, EndpointDescriptor
from meho_backplane.operations._audit import write_audit_row
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings
from tests.test_operation_contracts import _stub_embedding_service

#: Classes that win over the risk level: the broadcast shows only an aggregate.
_SECRET_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write", "audit_query"}
)

#: ``safe`` operations that are not plain reads. Each one makes the target do
#: work, so it is pinned ``write`` in ``broadcast/events.py`` (``_WRITE_OPS``).
_SAFE_OPERATIONS_THAT_CHANGE_THINGS: Final[frozenset[str]] = frozenset(
    {
        "vmware.composite.datastore.refresh",
        "rke2.etcd-snapshot.save",
        "vcfa.tenant.cloud_account.enumerate_images",
        "hcx.service_mesh.diagnose",
    }
)

#: Operations named in the bug reports, with the class they must get.
_EXPECTED: Final[dict[str, str]] = {
    "vmware.host.usage": "read",
    "vmware.object.collect": "read",
    "vmware.composite.event.tail": "read",
    "vmware.vm.hardware.show": "read",
    "pfsense.firewall.rules": "read",
    "azure.vm.show": "read",
    "vmware.composite.vm.destroy": "write",
    "k8s.apply": "write",
    "winsrv.power.reboot": "write",
    "k8s.scale": "write",
    "vmware.composite.datastore.refresh": "write",
    "vault.kv.read": "credential_read",
    "vault.kv.put": "credential_write",
}

_TENANT: Final[uuid.UUID] = uuid.UUID("00000000-0000-0000-0000-00000000c1a5")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the env vars :class:`Settings` needs (the registrars read it)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _hand_written_rows() -> list[EndpointDescriptor]:
    await run_typed_op_registrars(embedding_service=_stub_embedding_service())
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.source_kind.in_(("typed", "composite")),
            )
        )
        return list(rows.scalars().all())


def _op_class(row: EndpointDescriptor) -> str:
    return resolve_operation_op_class(
        row.op_id, source_kind=row.source_kind, safety_level=row.safety_level
    )


@pytest.mark.asyncio
async def test_every_hand_written_operation_has_a_read_or_write_class() -> None:
    """No registered hand-written operation falls back to ``other``."""
    rows = await _hand_written_rows()
    assert rows, "no typed or multi-step operation was registered"

    wrong: list[str] = []
    for row in rows:
        op_class = _op_class(row)
        if op_class in _SECRET_CLASSES:
            continue
        if row.safety_level != "safe":
            expected = WRITE_OP_CLASSES
        elif row.op_id in _SAFE_OPERATIONS_THAT_CHANGE_THINGS:
            expected = frozenset({"write"})
        else:
            expected = frozenset({"read"})
        if op_class not in expected:
            wrong.append(f"{row.op_id} ({row.safety_level}): {op_class}")
    assert not wrong, "hand-written operations with the wrong class:\n" + "\n".join(sorted(wrong))

    safe_ids = {row.op_id for row in rows if row.safety_level == "safe"}
    stale = sorted(_SAFE_OPERATIONS_THAT_CHANGE_THINGS - safe_ids)
    assert not stale, f"not registered as a 'safe' operation; remove from the list: {stale}"


@pytest.mark.asyncio
async def test_named_operations_get_the_expected_class() -> None:
    """The operations from the bug reports get the class they must have."""
    classes: dict[str, set[str]] = {}
    for row in await _hand_written_rows():
        if row.op_id in _EXPECTED:
            classes.setdefault(row.op_id, set()).add(_op_class(row))
    assert classes == {op_id: {op_class} for op_id, op_class in _EXPECTED.items()}


def _operator() -> Operator:
    return Operator(
        sub="user-a",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
async def test_audit_query_class_filter_finds_hand_written_reads_and_writes() -> None:
    """Dispatch rows of hand-written ops carry their class; the filter finds them."""
    operator = _operator()
    ops = {
        "vmware.host.usage": "safe",
        "vmware.object.collect": "safe",
        "vmware.composite.vm.destroy": "destructive",
        "k8s.scale": "caution",
    }
    for op_id, safety_level in ops.items():
        descriptor = SimpleNamespace(
            op_id=op_id,
            source_kind="typed",
            safety_level=safety_level,
            product="acme",
            version="1.x",
            impl_id="acme-1",
        )
        await write_audit_row(
            audit_id=uuid.uuid4(),
            event_class=AuditEventClass.OPERATION_EXECUTION,
            operator=operator,
            descriptor=descriptor,  # type: ignore[arg-type]
            target=None,
            params_hash="",
            result_status="ok",
            duration_ms=1.0,
        )

    found: dict[str, set[str]] = {}
    async with get_sessionmaker()() as session:
        for op_class in ("read", "write", "other"):
            result = await query_audit(
                AuditQueryFilters(op_class=op_class), tenant_id=_TENANT, session=session
            )
            found[op_class] = {row.op_id for row in result.rows}
    assert found == {
        "read": {"vmware.host.usage", "vmware.object.collect"},
        "write": {"vmware.composite.vm.destroy", "k8s.scale"},
        "other": set(),
    }
