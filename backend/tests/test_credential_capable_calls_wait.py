# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.exec``, ``proxmox.api.write`` and a ``k8s.apply`` of a Secret always wait for a person.

A command in a container and a raw Proxmox write can read, write or make
any credential. A ``k8s.apply`` whose manifest holds a Secret writes one.
These calls get the rule of a credential call: a person approves each
call, and no standing grant, no agent grant and no run approval covers
it, also for a step of a multi-step operation. Making a standing grant,
an agent grant or a run approval that names ``k8s.exec`` or
``proxmox.api.write`` is refused. A ``k8s.apply`` of other kinds keeps
the rules of ``k8s.apply``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import op_classes as k8s_op_classes
from meho_backplane.connectors.kubernetes.op_classes import (
    MAX_CHECKED_OBJECTS,
    manifest_holds_credential,
)
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.operations import dispatcher
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import CREDENTIAL_WAIT_NOTE, policy_gate
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.governed_subops import classify_subop_grantability
from meho_backplane.operations.op_policy import _with_floors, effective_policy
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    CREDENTIAL_CARD_LINE,
    GrantValidationError,
    ServicePrincipalGrantService,
    credential_grant_reason,
    never_covered_reason,
)
from meho_backplane.operations.wait_when_set import raise_descriptor_for_params
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.policy.shadow import evaluate_call
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings
from tests.test_credential_calls_always_wait import (
    _TENANT,
    _agent,
    _agent_grant,
    _name_in_every_grant,
    _register_all,
    _service,
)

_ALWAYS = ("k8s.exec", "proxmox.api.write")
_RUN = "run-credential-calls"

_SECRET = """
apiVersion: v1
kind: Secret
metadata:
  name: db-login
  namespace: apps
stringData:
  password: example-value
"""
_CONFIG_MAP = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: web-settings
  namespace: apps
data:
  mode: fast
"""
_TOKEN_SECRET = """
apiVersion: v1
kind: Secret
type: kubernetes.io/service-account-token
metadata:
  name: builder-token
  namespace: apps
  annotations:
    kubernetes.io/service-account.name: builder
"""
_LIST_WITH_SECRET = """
apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: ConfigMap
    metadata: {name: web-settings, namespace: apps}
  - apiVersion: v1
    kind: Secret
    metadata: {name: db-login, namespace: apps}
"""


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _row(rows: list[EndpointDescriptor], op_id: str) -> EndpointDescriptor:
    return next(row for row in rows if row.op_id == op_id)


async def _gate(
    operator: Operator, row: EndpointDescriptor, params: dict[str, Any] | None = None
) -> tuple[PermissionVerdict, str, Any]:
    """The verdict of today's gate and of the shadow evaluator for one call."""
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    raise_descriptor_for_params(descriptor, params or {})
    connector_id = connector_id_of(descriptor)
    token = work_ref_var.set(_RUN)
    try:
        today, reason = await policy_gate(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id,
            params=params,
            decide_only=True,
        )
        shadow = await evaluate_call(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id,
            decide_only=True,
        )
    finally:
        work_ref_var.reset(token)
    return today, reason or "", shadow.verdict


# ---------------------------------------------------------------------------
# The manifest check of k8s.apply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest",
    [
        _SECRET,
        _TOKEN_SECRET,
        _LIST_WITH_SECRET,
        _CONFIG_MAP + "\n---\n" + _SECRET,
        '{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "a"}}',
        "kind: [not, a, kind]\n",
        "apiVersion: v1\nmetadata: {name: a}\n",
        "- just\n- a list\n",
        "key: [unclosed\n",
        "",
        "---\n",
        None,
    ],
    ids=[
        "secret",
        "service-account-token",
        "list-with-secret",
        "second-document",
        "json",
        "kind-not-text",
        "no-kind",
        "not-a-mapping",
        "not-yaml",
        "empty",
        "only-separator",
        "not-text",
    ],
)
def test_a_manifest_with_a_secret_or_that_meho_cannot_read_counts(manifest: Any) -> None:
    assert manifest_holds_credential({"manifest": manifest}) is True


@pytest.mark.parametrize(
    "manifest",
    [
        _CONFIG_MAP,
        _CONFIG_MAP + "\n---\n" + _CONFIG_MAP.replace("web-settings", "other"),
        "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: v1\n    kind: ConfigMap\n"
        "    metadata: {name: a}\n",
        "apiVersion: example.com/v1\nkind: Playlist\nmetadata: {name: a}\nitems: [one, two]\n",
        "apiVersion: example.com/v1\nkind: Basket\nmetadata: {name: a}\n"
        "items:\n  - {kind: Secret, name: not-a-k8s-object}\n",
    ],
    ids=[
        "config-map",
        "two-config-maps",
        "list-of-config-maps",
        "custom-resource-with-items",
        "custom-resource-items-are-spec",
    ],
)
def test_a_manifest_of_other_kinds_does_not_count(manifest: str) -> None:
    assert manifest_holds_credential({"manifest": manifest}) is False


def _alias_manifest(levels: int, width: int) -> str:
    """A small manifest whose aliases name the same list objects many times."""
    lines = [
        "apiVersion: v1",
        "kind: List",
        "x0: &l0 {apiVersion: v1, kind: List, items: [{apiVersion: v1, kind: ConfigMap}]}",
    ]
    for level in range(1, levels):
        refs = ", ".join([f"*l{level - 1}"] * width)
        lines.append(f"x{level}: &l{level} {{apiVersion: v1, kind: List, items: [{refs}]}}")
    lines.append(f"items: [{', '.join([f'*l{levels - 1}'] * width)}]")
    return "\n".join(lines) + "\n"


def test_a_manifest_of_many_aliases_is_read_once_per_object() -> None:
    manifest = _alias_manifest(levels=30, width=10)
    assert len(manifest) < 4096
    started = time.perf_counter()
    assert manifest_holds_credential({"manifest": manifest}) is False
    assert time.perf_counter() - started < 1.0


def test_a_manifest_with_too_many_objects_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_CHECKED_OBJECTS == 10_000
    monkeypatch.setattr(k8s_op_classes, "MAX_CHECKED_OBJECTS", 5)
    items = "".join(
        f"  - {{apiVersion: v1, kind: ConfigMap, metadata: {{name: c{i}}}}}\n" for i in range(4)
    )
    manifest = "apiVersion: v1\nkind: List\nitems:\n" + items
    assert manifest_holds_credential({"manifest": manifest}) is False
    items += "  - {apiVersion: v1, kind: ConfigMap, metadata: {name: c4}}\n"
    manifest = "apiVersion: v1\nkind: List\nitems:\n" + items
    assert manifest_holds_credential({"manifest": manifest}) is True


def _shared_items_manifest(lists: int, objects: int) -> str:
    """Many different list objects that all name one shared ``items`` array."""
    shared = ", ".join(
        f"{{apiVersion: v1, kind: ConfigMap, metadata: {{name: c{i}}}}}" for i in range(objects)
    )
    lists_text = ", ".join(
        f"{{apiVersion: v1, kind: List, metadata: {{name: l{i}}}, items: *shared}}"
        for i in range(lists)
    )
    return f"apiVersion: v1\nkind: List\nx: &shared [{shared}]\nitems: [{lists_text}]\n"


def test_list_objects_that_share_one_items_array_read_it_once() -> None:
    manifest = _shared_items_manifest(lists=3000, objects=3000)
    assert manifest_holds_credential({"manifest": manifest}) is False
    docs = list(yaml.safe_load_all(manifest))
    started = time.perf_counter()
    assert k8s_op_classes._holds_credential(docs) is False
    assert time.perf_counter() - started < 0.5


def test_list_objects_that_share_one_items_array_with_a_secret_count() -> None:
    manifest = _shared_items_manifest(lists=50, objects=3).replace(
        "x: &shared [", "x: &shared [{apiVersion: v1, kind: Secret}, "
    )
    assert manifest_holds_credential({"manifest": manifest}) is True


def test_every_items_entry_counts_toward_the_object_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(k8s_op_classes, "MAX_CHECKED_OBJECTS", 5)
    shared = (
        "apiVersion: v1\nkind: List\n"
        "c: &c {apiVersion: v1, kind: ConfigMap}\n"
        "a: &a [*c, *c]\n"
        "items: [{apiVersion: v1, kind: List, items: *a},"
        " {apiVersion: v1, kind: List, items: *a}]\n"
    )
    assert manifest_holds_credential({"manifest": shared}) is False
    separate = shared.replace("items: *a}]", "items: [*c, *c]}]")
    assert manifest_holds_credential({"manifest": separate}) is True


def test_a_list_object_that_refers_to_itself_is_read_once() -> None:
    manifest = "&self {apiVersion: v1, kind: List, items: [*self]}\n"
    assert manifest_holds_credential({"manifest": manifest}) is False


# ---------------------------------------------------------------------------
# k8s.exec and proxmox.api.write: every call waits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_grant_and_no_run_approval_covers_exec_or_a_raw_proxmox_write() -> None:
    rows = await _register_all()
    marked = [_row(rows, op_id) for op_id in _ALWAYS]
    await _name_in_every_grant(marked)

    for row in marked:
        never = never_covered_reason(row)
        assert never is not None and never[0] == "credential", row.op_id
        assert classify_subop_grantability(row.op_id).grantable is False
        for operator in (_service(), _agent()):
            today, reason, shadow = await _gate(operator, row)
            assert today is PermissionVerdict.NEEDS_APPROVAL, (row.op_id, reason)
            assert shadow is Verdict.WAIT, (row.op_id, reason)
            assert "auto-granted" not in reason, (row.op_id, reason)
            assert "by run approval" not in reason, (row.op_id, reason)
            if operator.principal_kind is PrincipalKind.SERVICE:
                assert reason.endswith(CREDENTIAL_WAIT_NOTE), (row.op_id, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
async def test_grant_create_refuses_exec_and_a_raw_proxmox_write(op_id: str) -> None:
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            ServiceGrantCreate(
                principal_sub="svc-credential-calls",
                op_id=op_id,
                connector_id="kubernetes-1.x",
                reason="unattended build",
            ),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == (
        f"A standing grant, an agent grant or a run approval never covers {op_id!r}: "
        "it reads, writes or makes a credential, so a person approves each call. Leave it out."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
async def test_run_approval_create_refuses_exec_and_a_raw_proxmox_write(op_id: str) -> None:
    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id="kubernetes-1.x", op_id=op_id)],
                reason="one call",
            ),
        )
    assert refused.value.message == f"entries[0]: {credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
async def test_agent_grant_create_refuses_auto_execute_for_exec_and_a_raw_proxmox_write(
    op_id: str,
) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(op_id, PermissionVerdict.AUTO_EXECUTE)
        )
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.parametrize("op_id", _ALWAYS)
def test_an_override_does_not_turn_off_approval_of_exec_or_a_raw_proxmox_write(
    op_id: str,
) -> None:
    from meho_backplane.db.models import OperationPolicyOverride

    row = EndpointDescriptor(op_id=op_id, safety_level="dangerous", requires_approval=True)
    off = effective_policy("dangerous", True, [OperationPolicyOverride(requires_approval=False)])
    assert off.requires_approval is False
    assert _with_floors(row, off).requires_approval is True


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
@pytest.mark.parametrize("operator", [_service(), _agent()], ids=["service", "agent"])
async def test_a_step_of_exec_or_a_raw_proxmox_write_waits_under_every_grant(
    op_id: str, operator: Operator
) -> None:
    step = EndpointDescriptor(product="example", version="1.x", impl_id="example", op_id=op_id)
    await _name_in_every_grant([step])
    token = work_ref_var.set(_RUN)
    try:
        result = await enforce_subop_policy(
            operator=operator,
            connector_id="example-1.x",
            op_id=op_id,
            safety_level="caution",
            requires_approval=False,
            target=None,
            params={"command": ["id"]},
        )
    finally:
        work_ref_var.reset(token)
    assert result is not None
    assert result.status == "awaiting_approval", result.error


# ---------------------------------------------------------------------------
# k8s.apply: a Secret waits, other kinds keep the rules of k8s.apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_create_still_takes_k8s_apply() -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    assert credential_grant_reason("k8s.apply") is None
    created = await ServicePrincipalGrantService().create(
        _TENANT,
        "op-admin",
        ServiceGrantCreate(
            principal_sub="svc-credential-calls",
            op_id="k8s.apply",
            connector_id=connector_id_of(apply_row),
            reason="unattended deploy",
        ),
        creator_role=TenantRole.TENANT_ADMIN,
    )
    assert created.op_id == "k8s.apply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [_SECRET, _TOKEN_SECRET, _LIST_WITH_SECRET, _CONFIG_MAP + "\n---\n" + _SECRET, "a: [b"],
    ids=["secret", "token-secret", "list", "multi-document", "unreadable"],
)
async def test_a_k8s_apply_of_a_secret_waits_under_a_grant_and_a_run_approval(
    manifest: str,
) -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    await _name_in_every_grant([apply_row])

    today, reason, shadow = await _gate(_service(), apply_row, {"manifest": manifest})

    assert today is PermissionVerdict.NEEDS_APPROVAL, reason
    assert shadow is Verdict.WAIT, reason
    assert "auto-granted" not in reason
    assert reason.endswith(CREDENTIAL_WAIT_NOTE), reason


@pytest.mark.asyncio
async def test_a_k8s_apply_of_other_kinds_keeps_its_grant() -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    await _name_in_every_grant([apply_row])

    today, reason, _ = await _gate(_service(), apply_row, {"manifest": _CONFIG_MAP})

    assert today is PermissionVerdict.AUTO_EXECUTE, reason
    assert "auto-granted by standing grant" in reason


@pytest.mark.asyncio
async def test_a_k8s_apply_of_a_secret_waits_for_an_agent_with_an_auto_execute_row() -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    await _name_in_every_grant([apply_row])

    today, reason, shadow = await _gate(_agent(), apply_row, {"manifest": _SECRET})

    assert today is PermissionVerdict.NEEDS_APPROVAL, reason
    assert shadow is Verdict.WAIT, reason


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [_service(), _agent()], ids=["service", "agent"])
async def test_a_step_that_applies_a_secret_waits_and_other_kinds_keep_their_grant(
    operator: Operator,
) -> None:
    step = EndpointDescriptor(
        product="example", version="1.x", impl_id="example", op_id="k8s.apply"
    )
    await _name_in_every_grant([step])

    async def _step(manifest: str) -> Any:
        token = work_ref_var.set(_RUN)
        try:
            return await enforce_subop_policy(
                operator=operator,
                connector_id="example-1.x",
                op_id="k8s.apply",
                safety_level="caution",
                requires_approval=False,
                target=None,
                params={"manifest": manifest},
            )
        finally:
            work_ref_var.reset(token)

    waiting = await _step(_SECRET)
    assert waiting is not None
    assert waiting.status == "awaiting_approval", waiting.error
    if operator.principal_kind is PrincipalKind.SERVICE:
        assert await _step(_CONFIG_MAP) is None


# ---------------------------------------------------------------------------
# The approval card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "line"),
    [
        ("k8s.exec", {"pod": "web", "namespace": "apps", "command": ["id"]}, True),
        ("proxmox.api.write", {"path": "nodes/a/qemu/1/config", "method": "PUT"}, True),
        ("k8s.apply", {"manifest": _SECRET}, True),
        ("k8s.apply", {"manifest": _CONFIG_MAP}, False),
    ],
    ids=["exec", "proxmox-write", "apply-secret", "apply-config-map"],
)
async def test_the_approval_card_says_why_the_call_waits(
    op_id: str, params: dict[str, Any], line: bool
) -> None:
    descriptor = EndpointDescriptor(
        product="example",
        version="1.x",
        impl_id="example",
        op_id=op_id,
        source_kind="typed",
        safety_level="dangerous",
        requires_approval=True,
        parameter_schema={},
    )
    raise_descriptor_for_params(descriptor, params)
    effect = await dispatcher._build_proposed_effect(
        op_id=op_id,
        connector_id="example-1.x",
        descriptor=descriptor,
        operator=_service(),
        target=None,
        params=params,
    )
    assert effect is not None
    assert (effect.get("credential_wait") == CREDENTIAL_CARD_LINE) is line
