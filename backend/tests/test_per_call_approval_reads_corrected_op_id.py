# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A call that a connector marks to wait on every call waits for a stored row too.

The mark is a set of op ids. A stored row can have another op id than the one
the connector's ingest correction gives the same method and path. The checks
read both ids. The connector, the product and the paths here are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane import op_classes
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import spec_corrections
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.run_approval_schemas import RunApprovalEntry
from meho_backplane.operations.run_approvals import _entry_refusal
from meho_backplane.operations.service_grants import (
    descriptor_needs_per_call_approval,
    never_covered_reason,
    per_call_approval_grant_reason,
)
from meho_backplane.policy.shadow import _op_class
from meho_backplane.settings import get_settings

_PRODUCT = "examplebackup"
_VERSION = "1.0"
_IMPL = "examplebackup-api"
_NEW_ID = "POST:/groups/{id}/members"
_OLD_ID = "POST:/groups/{gid}/members"
_PLAIN_ID = "GET:/groups"
_TENANT = uuid.uuid4()


def _correct(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    del version
    if proto.method == "POST" and proto.path.endswith("/members"):
        return proto.model_copy(update={"op_id": _NEW_ID})
    return proto


def _failing(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    raise RuntimeError("the correction cannot read this row")


@pytest.fixture(autouse=True)
def _marked_connector(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    monkeypatch.setattr(op_classes, "_PER_CALL_APPROVAL", {_NEW_ID})
    monkeypatch.setattr(spec_corrections, "_CORRECTIONS", {(_PRODUCT, _IMPL): _correct})
    yield
    get_settings.cache_clear()


def _row(op_id: str, method: str, path: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        op_id=op_id,
        method=method,
        path=path,
        source_kind="ingested",
        safety_level="caution",
        is_enabled=True,
        tags=[],
        parameter_schema={},
        policy_always_wait=False,
    )


def _old_row() -> EndpointDescriptor:
    return _row(_OLD_ID, "POST", "/groups/{gid}/members")


def test_a_row_with_the_old_op_id_waits() -> None:
    assert descriptor_needs_per_call_approval(_old_row())
    assert per_call_approval_grant_reason(_OLD_ID, _old_row()) is not None
    kind = never_covered_reason(_old_row())
    assert kind is not None and kind[0] == "per_call_approval"


def test_the_op_id_alone_is_not_marked() -> None:
    assert per_call_approval_grant_reason(_OLD_ID) is None


def test_a_row_with_the_marked_op_id_waits() -> None:
    assert descriptor_needs_per_call_approval(_row(_NEW_ID, "POST", "/groups/{id}/members"))


def test_a_row_the_correction_leaves_alone_keeps_its_answer() -> None:
    row = _row(_PLAIN_ID, "GET", "/groups")
    assert not descriptor_needs_per_call_approval(row)
    assert per_call_approval_grant_reason(_PLAIN_ID, row) is None
    assert never_covered_reason(row) is None


def test_a_connector_without_a_correction_keeps_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spec_corrections, "_CORRECTIONS", {})
    assert not descriptor_needs_per_call_approval(_old_row())


def test_a_failing_correction_counts_the_call_as_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spec_corrections, "_CORRECTIONS", {(_PRODUCT, _IMPL): _failing})
    assert descriptor_needs_per_call_approval(_old_row())


def test_the_policy_shadow_report_marks_the_old_row() -> None:
    assert "per_call_approval" in _op_class(_old_row(), None)
    assert "per_call_approval" not in _op_class(_row(_PLAIN_ID, "GET", "/groups"), None)


@pytest.mark.asyncio
async def test_a_run_approval_for_the_old_op_id_is_refused() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(_old_row())
    entry = RunApprovalEntry(
        connector_id=f"{_IMPL}-{_VERSION}", op_id=_OLD_ID, target_product=_PRODUCT
    )
    refusal = await _entry_refusal(_TENANT, entry)
    assert refusal is not None and "a person approves each call" in refusal
