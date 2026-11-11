# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Category, labels and "always wait" with inheritance (``POLICY_CLASSIFICATION``).

* A table test of :func:`~meho_backplane.policy.classification.classify`:
  level, prefix length, scope, and the tie rules.
* The stored document: taxonomy and entries.
* :func:`~meho_backplane.operations.op_policy.effective_policy_map` and the
  lookup with the switch off (nothing read) and on (applied, floors last).
* With the switch off, every registered operation keeps today's values; with
  it on and nothing stored, only the default category is added.
* The ``policy.classify`` change: checks, storage, the rights-change mark,
  and nothing while the switch is off.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.classify import (
    POLICY_CLASSIFY_OP_ID,
    SWITCH_OFF_REFUSAL,
    policy_classify,
    register_policy_classify_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    EndpointDescriptor,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.approval_queue import (
    RightsChangeApprovalError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.grant_approval import (
    check_rights_change_approval,
    is_rights_change,
    is_rights_change_op,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.op_policy import (
    EffectivePolicy,
    OpKey,
    effective_policy_map,
)
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.operations.typed_register import (
    register_typed_operation,
    run_typed_op_registrars,
)
from meho_backplane.policy.classification import (
    DEFAULT_TAXONOMY,
    ClassificationConflictError,
    ClassificationEntry,
    OpCoordinates,
    Taxonomy,
    check_no_conflict,
    classify,
    entries_from_document,
    taxonomy_from_documents,
)
from meho_backplane.policy.store import ClassificationEdit, set_classification
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006037")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006038")
_FIX = {"product": "clsfix", "version": "1.x", "impl_id": "clsfix"}
_CONNECTOR = "clsfix-1.x"
_COORDS = OpCoordinates("k8s-1.x", "workload", "k8s.deployment.scale")
_TAX = Taxonomy(categories=("network", "compute", "storage", "other"), default="other")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("POLICY_CLASSIFICATION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if on else "false")
    get_settings.cache_clear()


def _entry(ref: str, scope: Any = "install", **fields: Any) -> ClassificationEntry:
    return ClassificationEntry(ref=ref, scope=scope, **fields)


# ---------------------------------------------------------------------------
# classify(): the table test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entries", "category", "rule_ref"),
    [
        pytest.param([], "other", "taxonomy.default", id="no-entry-default"),
        pytest.param(
            [_entry("c", connector="k8s-*", category="compute")],
            "compute",
            "c",
            id="connector-only",
        ),
        pytest.param(
            [
                _entry("c", connector="k8s-*", category="compute"),
                _entry("g", group="workload", category="storage"),
            ],
            "storage",
            "g",
            id="group-beats-connector",
        ),
        pytest.param(
            [
                _entry("g", group="workload", category="storage"),
                _entry("o", op="k8s.*", category="network"),
                _entry("c", connector="k8s-1.x", category="compute"),
            ],
            "network",
            "o",
            id="op-beats-group-and-connector",
        ),
        pytest.param(
            [
                _entry("short", op="k8s.*", category="compute"),
                _entry("long", op="k8s.deployment.*", category="network"),
            ],
            "network",
            "long",
            id="longer-prefix-wins",
        ),
        pytest.param(
            [
                _entry("bare", group="workload", category="compute"),
                _entry("narrow", connector="k8s-*", group="workload", category="network"),
            ],
            "network",
            "narrow",
            id="connector-narrows-a-group-entry",
        ),
        pytest.param(
            [
                _entry("install", op="k8s.deployment.scale", category="compute"),
                _entry("tenant", "tenant", op="k8s.deployment.scale", category="network"),
            ],
            "network",
            "tenant",
            id="tenant-beats-install-at-same-rank",
        ),
        pytest.param(
            [_entry("other-group", group="network", category="network")],
            "other",
            "taxonomy.default",
            id="no-match-default",
        ),
        pytest.param(
            [_entry("case", op="K8S.*", category="network")],
            "other",
            "taxonomy.default",
            id="case-sensitive",
        ),
    ],
)
def test_category_inheritance(
    entries: list[ClassificationEntry], category: str, rule_ref: str
) -> None:
    result = classify(_COORDS, entries, _TAX)
    assert (result.category, result.rule_ref, result.conflict) == (category, rule_ref, None)


def test_each_field_comes_from_its_own_most_specific_entry() -> None:
    entries = [
        _entry("c", connector="k8s-*", category="compute", risk="dangerous", always_wait=True),
        _entry("o", op="k8s.deployment.scale", labels={"team": "apps"}),
    ]
    result = classify(_COORDS, entries, _TAX)
    assert result.category == "compute"
    assert result.risk == "dangerous"
    assert result.always_wait is True
    assert result.labels == {"team": "apps"}
    assert result.rule_ref == "o"


def test_more_specific_always_wait_false_undoes_a_connector_wait() -> None:
    entries = [
        _entry("c", connector="k8s-*", always_wait=True),
        _entry("o", op="k8s.deployment.scale", always_wait=False),
    ]
    assert classify(_COORDS, entries, _TAX).always_wait is False


def test_risk_tie_takes_the_stricter_level() -> None:
    entries = [
        _entry("a", op="k8s.deployment.*", risk="caution"),
        _entry("b", op="k8s.deployment.?cale", risk="dangerous"),
    ]
    assert classify(_COORDS, entries, _TAX).risk == "dangerous"


def test_always_wait_tie_takes_true() -> None:
    entries = [
        _entry("a", op="k8s.deployment.*", always_wait=False),
        _entry("b", op="k8s.deployment.?cale", always_wait=True),
    ]
    assert classify(_COORDS, entries, _TAX).always_wait is True


def test_category_tie_is_a_conflict_that_waits() -> None:
    entries = [
        _entry("first", op="k8s.deployment.*", category="compute"),
        _entry("second", op="k8s.deployment.?cale", category="network"),
    ]
    result = classify(_COORDS, entries, _TAX)
    assert result.category is None
    assert result.always_wait is True
    assert result.conflict == (
        "first and second give k8s.deployment.scale a different category at the same "
        "level, so make one of them more specific."
    )
    with pytest.raises(ClassificationConflictError, match=r"^first and second give"):
        check_no_conflict([_COORDS], entries, _TAX)


def test_same_category_at_same_rank_is_no_conflict() -> None:
    entries = [
        _entry("a", op="k8s.deployment.*", category="compute"),
        _entry("b", op="k8s.deployment.?cale", category="compute"),
    ]
    assert classify(_COORDS, entries, _TAX).conflict is None
    check_no_conflict([_COORDS], entries, _TAX)


def test_labels_add_up_and_the_specific_one_replaces_a_key() -> None:
    entries = [
        _entry("c", connector="k8s-*", labels={"team": "platform", "zone": "a"}),
        _entry("o", op="k8s.deployment.scale", labels={"team": "apps"}),
    ]
    assert classify(_COORDS, entries, _TAX).labels == {"team": "apps", "zone": "a"}


def test_label_tie_with_different_values_is_a_conflict() -> None:
    entries = [
        _entry("a", op="k8s.deployment.*", labels={"team": "apps"}),
        _entry("b", op="k8s.deployment.?cale", labels={"team": "net"}),
    ]
    result = classify(_COORDS, entries, _TAX)
    assert result.conflict is not None
    assert "label 'team'" in result.conflict


def test_a_label_tie_below_a_more_specific_entry_is_no_conflict() -> None:
    entries = [
        _entry("a", connector="k8s-*", labels={"team": "apps"}),
        _entry("b", connector="k8s-?.x", labels={"team": "net"}),
        _entry("o", op="k8s.deployment.scale", labels={"team": "ops"}),
    ]
    result = classify(_COORDS, entries, _TAX)
    assert (result.conflict, result.labels) == (None, {"team": "ops"})


def test_group_entry_does_not_match_an_operation_without_a_group() -> None:
    coords = OpCoordinates("k8s-1.x", None, "k8s.deployment.scale")
    result = classify(coords, [_entry("g", group="*", category="compute")], _TAX)
    assert result.category == "other"


# ---------------------------------------------------------------------------
# The stored document
# ---------------------------------------------------------------------------


def test_taxonomy_default_and_tenant_additions() -> None:
    assert taxonomy_from_documents(None, None) == DEFAULT_TAXONOMY
    install = {"taxonomy": {"categories": ["network", "misc"], "default": "misc"}}
    tenant = {"taxonomy": {"categories": ["network", "db"], "default": "db"}}
    assert taxonomy_from_documents(install, tenant) == Taxonomy(("network", "misc", "db"), "misc")


def test_entries_from_document_names_each_entry() -> None:
    doc = {
        "classifications": [
            {"match": {"connector": "vault-*"}, "category": "secrets"},
            {"match": {"op": "vault.kv.read"}, "always_wait": True, "labels": {"a": "b"}},
        ]
    }
    entries = entries_from_document(doc, scope="tenant")
    assert [e.ref for e in entries] == ["tenant:classifications[0]", "tenant:classifications[1]"]
    assert entries[1].always_wait is True
    assert entries[1].labels == {"a": "b"}


@pytest.mark.parametrize(
    "raw",
    [
        {"category": "x"},
        {"match": {"op": "a"}, "risk": "extreme"},
        {"match": {"op": "a"}, "always_wait": "yes"},
        {"match": {"op": "a"}, "labels": {"a": 1}},
    ],
)
def test_entries_from_document_refuses_a_bad_entry(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"^install:classifications\[0\]: "):
        entries_from_document({"classifications": [raw]}, scope="install")


# ---------------------------------------------------------------------------
# Effective policy: switch off and on
# ---------------------------------------------------------------------------


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(
    embedding: AsyncMock,
    op_id: str,
    *,
    safety_level: Any = "safe",
    requires_approval: bool = False,
    group_key: str | None = "compute",
) -> None:
    await register_typed_operation(
        **_FIX,
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key=group_key,
        when_to_use="Fixture group." if group_key else None,
        safety_level=safety_level,
        requires_approval=requires_approval,
        embedding_service=embedding,
    )


async def _store_policy(tenant_id: uuid.UUID | None, document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


async def _policies(tenant_id: uuid.UUID) -> dict[str, EffectivePolicy]:
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "clsfix")
                )
            ).scalars()
        )
        policies = await effective_policy_map(session, tenant_id=tenant_id, rows=rows)
    return {key.op_id: policy for key, policy in policies.items()}


_DOC = {
    "classifications": [
        {"match": {"connector": "clsfix-*"}, "category": "compute", "labels": {"team": "ops"}},
        {"match": {"group": "compute"}, "always_wait": True},
        {"match": {"op": "clsfix.vm.delete"}, "risk": "destructive"},
        {"match": {"op": "clsfix.vm.restart"}, "risk": "safe", "always_wait": False},
    ]
}


@pytest.mark.asyncio
async def test_switch_off_reads_nothing(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, False)
    await _register(embedding, "clsfix.vm.list")
    await _store_policy(None, _DOC)
    policy = (await _policies(_TENANT))["clsfix.vm.list"]
    assert policy == EffectivePolicy("safe", False)
    assert (policy.category, dict(policy.labels), policy.rule_ref) == (None, {}, None)


@pytest.mark.asyncio
async def test_switch_on_applies_the_classification(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding, "clsfix.vm.list")
    await _register(embedding, "clsfix.vm.delete", group_key=None)
    await _register(
        embedding, "clsfix.vm.restart", safety_level="dangerous", requires_approval=True
    )
    await _store_policy(None, _DOC)
    policies = await _policies(_TENANT)

    listed = policies["clsfix.vm.list"]
    assert (listed.safety_level, listed.requires_approval) == ("safe", True)
    assert (listed.category, dict(listed.labels)) == ("compute", {"team": "ops"})
    assert listed.rule_ref == "install:classifications[1]"

    deleted = policies["clsfix.vm.delete"]
    # A raise to destructive always waits; no group, so the group wait does not apply.
    assert (deleted.safety_level, deleted.requires_approval) == ("destructive", True)

    # risk "safe" and always_wait false never go below the code's values.
    restart = policies["clsfix.vm.restart"]
    assert (restart.safety_level, restart.requires_approval) == ("dangerous", True)


@pytest.mark.asyncio
async def test_tenant_entry_applies_to_its_tenant_only(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding, "clsfix.vm.list")
    await _store_policy(
        _TENANT,
        {
            "taxonomy": {"categories": ["fleet"]},
            "classifications": [{"match": {"op": "clsfix.*"}, "category": "fleet"}],
        },
    )
    assert (await _policies(_TENANT))["clsfix.vm.list"].category == "fleet"
    assert (await _policies(_OTHER_TENANT))["clsfix.vm.list"].category == "other"


@pytest.mark.asyncio
async def test_override_category_and_labels_are_an_operation_entry(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding, "clsfix.vm.list")
    await _store_policy(None, _DOC)
    async with get_sessionmaker()() as session:
        session.add(
            OperationPolicyOverride(
                tenant_id=_TENANT,
                **_FIX,
                op_id="clsfix.vm.list",
                category="storage",
                labels={"zone": "b"},
                set_by_sub="fixture",
            )
        )
        await session.commit()
    policy = (await _policies(_TENANT))["clsfix.vm.list"]
    assert policy.category == "storage"
    assert dict(policy.labels) == {"team": "ops", "zone": "b"}
    assert policy.rule_ref == "tenant:override"


@pytest.mark.asyncio
async def test_a_conflict_makes_the_operation_wait(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding, "clsfix.vm.list")
    await _store_policy(
        None,
        {
            "classifications": [
                {"match": {"op": "clsfix.vm.*"}, "category": "compute"},
                {"match": {"op": "clsfix.vm.?ist"}, "category": "storage"},
            ]
        },
    )
    policy = (await _policies(_TENANT))["clsfix.vm.list"]
    assert (policy.category, policy.requires_approval) == (None, True)


@pytest.mark.asyncio
async def test_lookup_matches_the_joined_group(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(embedding, "clsfix.vm.list")
    await _store_policy(
        None, {"classifications": [{"match": {"group": "comp*"}, "always_wait": True}]}
    )

    _switch(monkeypatch, False)
    row = await lookup_descriptor(tenant_id=_TENANT, op_id="clsfix.vm.list", **_FIX)
    assert row is not None
    assert row.requires_approval is False

    _switch(monkeypatch, True)
    row = await lookup_descriptor(tenant_id=_TENANT, op_id="clsfix.vm.list", **_FIX)
    assert row is not None
    assert row.requires_approval is True
    async with get_sessionmaker()() as session:
        stored = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "clsfix.vm.list")
            )
        ).scalar_one()
    assert stored.requires_approval is False


# ---------------------------------------------------------------------------
# Every registered operation: switch off keeps today's values
# ---------------------------------------------------------------------------


async def _all_policies(tenant_id: uuid.UUID) -> dict[OpKey, EffectivePolicy]:
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(EndpointDescriptor))).scalars())
        return await effective_policy_map(session, tenant_id=tenant_id, rows=rows)


@pytest.mark.asyncio
async def test_every_registered_operation_keeps_its_values(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switch off: no classification field; switch on with nothing stored: same levels."""
    _switch(monkeypatch, False)
    await run_typed_op_registrars(embedding_service=embedding)
    off = await _all_policies(_TENANT)
    assert len(off) > 100
    assert all(p.category is None and not p.labels and p.rule_ref is None for p in off.values())

    _switch(monkeypatch, True)
    on = await _all_policies(_TENANT)
    assert on.keys() == off.keys()
    for key, policy in on.items():
        before = off[key]
        assert (policy.safety_level, policy.requires_approval) == (
            before.safety_level,
            before.requires_approval,
        ), key.op_id
        assert (policy.category, policy.rule_ref) == ("other", "taxonomy.default"), key.op_id


# ---------------------------------------------------------------------------
# policy.classify: the change, its checks and its approval mark
# ---------------------------------------------------------------------------


def _admin(sub: str = "admin-a") -> Operator:
    return Operator(
        sub=sub,
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
    )


@pytest.mark.asyncio
async def test_set_classification_stores_and_merges(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding, "clsfix.vm.list")
    await _register(embedding, "clsfix.vm.stop")
    match = {"connector": _CONNECTOR, "op": "clsfix.vm.*"}
    async with get_sessionmaker()() as session, session.begin():
        first = await set_classification(
            session,
            tenant_id=_TENANT,
            set_by_sub="admin-a",
            edit=ClassificationEdit(match=match, category="network", labels=None, always_wait=None),
        )
    async with get_sessionmaker()() as session, session.begin():
        second = await set_classification(
            session,
            tenant_id=_TENANT,
            set_by_sub="admin-b",
            edit=ClassificationEdit(
                match=match, category=None, labels={"t": "x"}, always_wait=True
            ),
        )
    assert first["rule_ref"] == second["rule_ref"] == "tenant:classifications[0]"
    assert second["matched_operations"] == 2
    assert second["entry"] == {
        "match": match,
        "category": "network",
        "labels": {"t": "x"},
        "always_wait": True,
    }
    policy = (await _policies(_TENANT))["clsfix.vm.stop"]
    assert (policy.category, policy.requires_approval) == ("network", True)
    assert (await _policies(_OTHER_TENANT))["clsfix.vm.stop"].category == "other"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            ClassificationEdit(match={}, category="network", labels=None, always_wait=None),
            "`match` needs at least one of connector, group or op.",
        ),
        (
            ClassificationEdit(match={"op": "a"}, category=None, labels=None, always_wait=None),
            "Set at least one of category, labels or always_wait.",
        ),
        (
            ClassificationEdit(match={"op": "a"}, category="cars", labels=None, always_wait=None),
            "Category 'cars' is not in the taxonomy",
        ),
        (
            ClassificationEdit(
                match={"op": "a"}, category=None, labels={"k": ""}, always_wait=None
            ),
            "The value of label 'k' must have 1 to 255 characters.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_set_classification_refuses_a_bad_edit(
    edit: ClassificationEdit, message: str
) -> None:
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError, match=re.escape(message)):
            await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
        assert (await session.execute(select(TenantPolicy))).first() is None


@pytest.mark.asyncio
async def test_set_classification_refuses_a_conflict(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding, "clsfix.vm.list")
    await _store_policy(
        _TENANT, {"classifications": [{"match": {"op": "clsfix.vm.*"}, "category": "compute"}]}
    )
    edit = ClassificationEdit(
        match={"op": "clsfix.vm.?ist"}, category="storage", labels=None, always_wait=None
    )
    async with get_sessionmaker()() as session:
        with pytest.raises(ClassificationConflictError, match="make one of them more specific"):
            await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
    async with get_sessionmaker()() as session:
        stored = (await session.execute(select(TenantPolicy))).scalar_one()
    assert len(stored.document["classifications"]) == 1


@pytest.mark.asyncio
async def test_policy_classify_with_the_switch_off(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, False)
    await register_policy_classify_operation(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == POLICY_CLASSIFY_OP_ID)
            )
        ).all()
    assert rows == []
    with pytest.raises(ValueError, match="Classification is off"):
        await policy_classify(_admin(), None, {"match": {"op": "a"}, "category": "network"})
    assert SWITCH_OFF_REFUSAL.endswith("POLICY_CLASSIFICATION setting.")


@pytest.mark.asyncio
async def test_policy_classify_registers_as_a_waiting_rights_change(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await register_policy_classify_operation(embedding_service=embedding)
    row = await lookup_descriptor(
        tenant_id=_TENANT,
        product="policy",
        version="1.x",
        impl_id="policy-registry",
        op_id=POLICY_CLASSIFY_OP_ID,
    )
    assert row is not None
    assert (row.safety_level, row.requires_approval) == ("dangerous", True)
    assert role_floor_for(POLICY_CLASSIFY_OP_ID) == TenantRole.TENANT_ADMIN
    assert is_rights_change_op(POLICY_CLASSIFY_OP_ID)

    result = await policy_classify(
        _admin(), None, {"match": {"connector": "vault-*"}, "category": "secrets"}
    )
    assert result["scope"] == "tenant"
    assert result["rule_ref"] == "tenant:classifications[0]"


def test_a_classify_request_needs_another_admin() -> None:
    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        principal_sub="admin-a",
        connector_id="policy-registry-1.x",
        op_id=POLICY_CLASSIFY_OP_ID,
        params={},
    )
    assert is_rights_change(request)
    with pytest.raises(RightsChangeApprovalError):
        check_rights_change_approval(_admin("admin-a"), request)
    check_rights_change_approval(_admin("admin-b"), request)
    operator_b = Operator(
        sub="operator-b",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
    )
    with pytest.raises(RightsChangeApprovalError):
        check_rights_change_approval(operator_b, request)


async def test_a_classify_call_waits_and_only_another_admin_approves_it(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the call waits with the mark; the asker is refused; a second admin runs it."""
    _switch(monkeypatch, True)
    await register_policy_classify_operation(embedding_service=embedding)
    asker, approver = _admin("admin-a"), _admin("admin-b")
    call = await call_operation(
        asker,
        {
            "connector_id": "policy-registry-1.x",
            "op_id": POLICY_CLASSIFY_OP_ID,
            "params": {"match": {"connector": "vault-*"}, "category": "secrets"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    request_id = uuid.UUID(str(call["extras"]["approval_request_id"]))
    async with get_sessionmaker()() as session:
        assert (await session.get(ApprovalRequest, request_id)).rights_change is True  # type: ignore[union-attr]
        assert (await session.execute(select(TenantPolicy))).first() is None
        with pytest.raises(RightsChangeApprovalError):
            await approve_request(session, request_id, operator=asker)
        await session.rollback()
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    result = await resume_dispatch_after_approval(operator=approver, request=request)
    assert result.status == "ok", result
    async with get_sessionmaker()() as session:
        stored = (await session.execute(select(TenantPolicy))).scalar_one()
    assert stored.tenant_id == _TENANT
    assert stored.document["classifications"] == [
        {"match": {"connector": "vault-*"}, "category": "secrets"}
    ]
