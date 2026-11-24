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
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane import op_classes
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.operations import (
    _validate,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
    service_read_scopes,
)
from meho_backplane.operations._validate import PER_CALL_WAIT_NOTE, policy_gate
from meho_backplane.operations.composite import (
    _covered_by_approved_run,
    approved_run_var,
    composite_depth_var,
    enforce_subop_policy,
    per_call_step_waits_after_approval,
)
from meho_backplane.operations.ingest import spec_corrections
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.rights_change_console import CONSOLE_RUN_REASON, ConsoleCheck
from meho_backplane.operations.run_approval_schemas import RunApprovalEntry
from meho_backplane.operations.run_approvals import _entry_refusal
from meho_backplane.operations.service_grants import (
    descriptor_needs_per_call_approval,
    never_covered_reason,
    per_call_approval_grant_reason,
)
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.policy.shadow import _op_class, evaluate_call
from meho_backplane.settings import get_settings

_PRODUCT = "examplebackup"
_VERSION = "1.0"
_IMPL = "examplebackup-api"
_NEW_ID = "POST:/groups/{id}/members"
_OLD_ID = "POST:/groups/{gid}/members"
_PLAIN_ID = "GET:/groups"
_TENANT = uuid.uuid4()
_AGENT = "examplebackup-agent"
_CONNECTOR = f"{_IMPL}-{_VERSION}"
_GROUP_TAG = "spec:groups"


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


def _row(
    op_id: str,
    method: str | None,
    path: str | None,
    *,
    safety_level: str = "caution",
    tags: list[str] | None = None,
    parameter_schema: dict[str, object] | None = None,
) -> EndpointDescriptor:
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
        safety_level=safety_level,
        requires_approval=False,
        is_enabled=True,
        tags=tags or [],
        parameter_schema=parameter_schema or {},
        policy_always_wait=False,
    )


def _old_row(safety_level: str = "caution") -> EndpointDescriptor:
    return _row(_OLD_ID, "POST", "/groups/{gid}/members", safety_level=safety_level)


def _service() -> Operator:
    return Operator(
        sub="examplebackup-service",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


def _agent() -> Operator:
    return Operator(
        sub=str(uuid.uuid5(uuid.NAMESPACE_URL, _AGENT)),
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=_AGENT,
    )


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


def test_a_row_without_method_or_path_keeps_its_answer() -> None:
    row = _row("examplebackup.groups.list", None, None)
    assert not descriptor_needs_per_call_approval(row)
    assert never_covered_reason(row) is None


def _correct_by_tag(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    del version
    if proto.method == "POST" and _GROUP_TAG in proto.tags:
        return proto.model_copy(update={"op_id": _NEW_ID})
    return proto


def _correct_by_schema(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    del version
    properties = proto.parameter_schema["properties"]
    if isinstance(properties, dict) and "gid" in properties:
        return proto.model_copy(update={"op_id": _NEW_ID})
    return proto


def test_the_correction_reads_the_stored_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spec_corrections, "_CORRECTIONS", {(_PRODUCT, _IMPL): _correct_by_tag})
    path = "/groups/{gid}/members"
    assert descriptor_needs_per_call_approval(_row(_OLD_ID, "POST", path, tags=[_GROUP_TAG]))
    assert not descriptor_needs_per_call_approval(_row(_OLD_ID, "POST", path, tags=["spec:other"]))


def test_the_correction_reads_the_stored_parameter_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spec_corrections, "_CORRECTIONS", {(_PRODUCT, _IMPL): _correct_by_schema})
    path = "/groups/{gid}/members"
    marked = _row(_OLD_ID, "POST", path, parameter_schema={"properties": {"gid": {}}})
    plain = _row(_PLAIN_ID, "GET", "/groups", parameter_schema={"properties": {"name": {}}})
    assert descriptor_needs_per_call_approval(marked)
    assert not descriptor_needs_per_call_approval(plain)
    assert never_covered_reason(plain) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [_service(), _agent()], ids=["service", "agent"])
async def test_a_safe_old_row_waits_at_the_gate_and_a_plain_safe_row_runs(
    operator: Operator,
) -> None:
    plain = _row(_PLAIN_ID, "GET", "/groups", safety_level="safe")
    verdict, reason = await policy_gate(
        operator=operator, descriptor=plain, target=None, connector_id=_CONNECTOR
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    verdict, reason = await policy_gate(
        operator=operator,
        descriptor=_old_row(safety_level="safe"),
        target=None,
        connector_id=_CONNECTOR,
        decide_only=True,
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert reason is not None and PER_CALL_WAIT_NOTE in reason
    shadow = await evaluate_call(
        operator=operator,
        descriptor=_old_row(safety_level="safe"),
        target=None,
        connector_id=_CONNECTOR,
        decide_only=True,
    )
    assert shadow.verdict is Verdict.WAIT


def _person(*, web: bool = False, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub="examplebackup-person",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        web_session=web,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN])
async def test_a_person_token_call_of_a_safe_old_row_waits_and_a_plain_safe_row_runs(
    role: TenantRole,
) -> None:
    operator = _person(role=role)
    plain = _row(_PLAIN_ID, "GET", "/groups", safety_level="safe")
    verdict, reason = await policy_gate(
        operator=operator, descriptor=plain, target=None, connector_id=_CONNECTOR
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    old = _old_row(safety_level="safe")
    verdict, reason = await policy_gate(
        operator=operator, descriptor=old, target=None, connector_id=_CONNECTOR, decide_only=True
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert reason == PER_CALL_WAIT_NOTE
    shadow = await evaluate_call(
        operator=operator, descriptor=old, target=None, connector_id=_CONNECTOR, decide_only=True
    )
    assert shadow.verdict is Verdict.WAIT


@pytest.mark.asyncio
@pytest.mark.parametrize("safety_level", ["safe", "caution"])
async def test_a_person_web_call_of_an_old_row_keeps_the_web_rules(safety_level: str) -> None:
    operator = _person(web=True)
    old = _old_row(safety_level=safety_level)
    verdict, reason = await policy_gate(
        operator=operator, descriptor=old, target=None, connector_id=_CONNECTOR, decide_only=True
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    shadow = await evaluate_call(
        operator=operator, descriptor=old, target=None, connector_id=_CONNECTOR, decide_only=True
    )
    assert shadow.verdict is Verdict.RUN


@pytest.mark.asyncio
async def test_an_admin_change_of_rights_in_the_web_console_still_runs_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_validate, "runs_at_once_in_console", lambda *_a, **_k: True)
    monkeypatch.setattr(_validate, "console_check", AsyncMock(return_value=ConsoleCheck()))
    verdict, reason = await policy_gate(
        operator=_person(web=True, role=TenantRole.TENANT_ADMIN),
        descriptor=_old_row(safety_level="dangerous"),
        target=None,
        connector_id=_CONNECTOR,
        decide_only=True,
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason == CONSOLE_RUN_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_scope", "verdict", "shadow_verdict"),
    [
        (False, PermissionVerdict.DENY, Verdict.DENY),
        (True, PermissionVerdict.NEEDS_APPROVAL, Verdict.WAIT),
    ],
    ids=["outside-scope", "inside-scope"],
)
async def test_a_marked_read_outside_the_read_scope_is_denied_and_waits_inside_it(
    monkeypatch: pytest.MonkeyPatch,
    in_scope: bool,
    verdict: PermissionVerdict,
    shadow_verdict: Verdict,
) -> None:
    denial = None if in_scope else "read_scope_denied: the target is outside the read scope"
    monkeypatch.setattr(service_read_scopes, "read_scope_denial", AsyncMock(return_value=denial))
    old = _old_row(safety_level="safe")
    got, reason = await policy_gate(
        operator=_service(), descriptor=old, target=None, connector_id=_CONNECTOR, decide_only=True
    )
    assert got is verdict, reason
    assert reason is not None
    assert reason.startswith("read_scope_denied") is not in_scope
    shadow = await evaluate_call(
        operator=_service(), descriptor=old, target=None, connector_id=_CONNECTOR, decide_only=True
    )
    assert shadow.verdict is shadow_verdict


def test_the_approval_of_a_run_does_not_cover_a_marked_step() -> None:
    assert not _covered_by_approved_run(
        op_id=_NEW_ID, safety_level="safe", requires_approval=False, per_call_approval=True
    )
    token = approved_run_var.set(True)
    try:
        assert per_call_step_waits_after_approval(_old_row(safety_level="safe"))
        assert not per_call_step_waits_after_approval(_row(_PLAIN_ID, "GET", "/groups"))
    finally:
        approved_run_var.reset(token)
    assert not per_call_step_waits_after_approval(_old_row(safety_level="safe"))


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [_service(), _person()], ids=["service", "person"])
async def test_an_approved_run_does_not_cover_a_marked_step(operator: Operator) -> None:
    token = approved_run_var.set(True)
    try:
        result = await enforce_subop_policy(
            operator=operator,
            connector_id=_CONNECTOR,
            op_id=_NEW_ID,
            safety_level="safe",
            requires_approval=False,
            target=None,
            params={},
        )
    finally:
        approved_run_var.reset(token)
    assert result is not None
    assert result.status == "awaiting_approval", result.error


_CHILD_PRODUCT = "examplechild"
_CHILD_CONNECTOR = "examplechild-1.x"
_MARKED_CHILD = "examplechild.group.member.add"
_PLAIN_CHILD = "examplechild.group.list"
_handler_calls: list[str] = []


class _ChildStub(Connector):
    """Lets the resolver pick a connector; the typed handlers never call it."""

    product = _CHILD_PRODUCT
    version = "1.x"
    impl_id = _CHILD_PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


class _Target:
    def __init__(self) -> None:
        self.product = _CHILD_PRODUCT
        self.fingerprint = None
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.name = "test-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _handler_calls.append("ran")
    return {"ran": True}


@pytest.fixture
def child_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(op_classes, "_PER_CALL_APPROVAL", {_NEW_ID, _MARKED_CHILD})
    _handler_calls.clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product=_CHILD_PRODUCT, version="", impl_id="", cls=_ChildStub)
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.mark.asyncio
@pytest.mark.usefixtures("child_stub")
@pytest.mark.parametrize(
    ("op_id", "status", "calls"),
    [(_MARKED_CHILD, "awaiting_approval", []), (_PLAIN_CHILD, "ok", ["ran"])],
    ids=["marked", "plain"],
)
async def test_an_approved_run_does_not_cover_a_marked_child_call(
    op_id: str, status: str, calls: list[str]
) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product=_CHILD_PRODUCT,
        version="1.x",
        impl_id=_CHILD_PRODUCT,
        op_id=op_id,
        handler=_child_handler,
        summary="A child step.",
        description="A child step.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        embedding_service=embedding,
    )
    approved = approved_run_var.set(True)
    depth = composite_depth_var.set(1)
    try:
        result = await dispatch(
            operator=_service(),
            connector_id=_CHILD_CONNECTOR,
            op_id=op_id,
            target=_Target(),
            params={},
        )
    finally:
        composite_depth_var.reset(depth)
        approved_run_var.reset(approved)
    assert result.status == status, result.error
    assert _handler_calls == calls
