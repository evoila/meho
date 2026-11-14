# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An install-wide "always wait" is binding for every tenant; a tenant can only add.

* :func:`~meho_backplane.policy.classification.classify`: an install entry
  with ``always_wait: true`` wins over every tenant entry; a tenant entry
  adds a wait; the install's own more specific entry still decides.
* The policy file: a tenant entry that sets an install-wide wait back is a
  mistake that names the install entry; the install file has no warning.
* The single edit (``policy.classify``): the dry run shows the refusal, the
  request is refused, and the stored change is refused.
* The upgrade case: a stored tenant entry that set the wait back no longer
  does, and the export marks it "Has no effect".
* The policy evaluator and its compare-only run read the same wait.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, TenantPolicy
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.op_policy import effective_policy_map
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.bulk import (
    ClassificationEditError,
    preview_classification_edit,
    request_classification_edit,
)
from meho_backplane.policy.classification import (
    ClassificationEntry,
    OpCoordinates,
    Taxonomy,
    classify,
)
from meho_backplane.policy.evaluate import Asker, Lookups, Verdict, evaluate
from meho_backplane.policy.service import build_plan, export_policy
from meho_backplane.policy.shadow import asked_of
from meho_backplane.policy.store import ClassificationEdit, set_classification
from meho_backplane.policy.validate import KnownOp, check_document
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006401")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006402")
_FIX = {"product": "iwfix", "version": "1.x", "impl_id": "iwfix"}
_CONNECTOR = "iwfix-1.x"
_COORDS = OpCoordinates(_CONNECTOR, "vm", "iwfix.vm.stop")
_TAX = Taxonomy(categories=("compute", "other"), default="other")

_INSTALL_WAIT = {"classifications": [{"match": {"op": "iwfix.vm.stop"}, "always_wait": True}]}
_TENANT_UNDO = {"classifications": [{"match": {"op": "iwfix.vm.*"}, "always_wait": False}]}
_REFUSAL = (
    "sets `always_wait: false` on `iwfix.vm.stop`, but `install:classifications[0]` makes it "
    "always wait for the whole install. A tenant can make more operations wait, not fewer."
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _install(ref: str, **fields: Any) -> ClassificationEntry:
    return ClassificationEntry(ref=ref, scope="install", **fields)


def _tenant(ref: str, **fields: Any) -> ClassificationEntry:
    return ClassificationEntry(ref=ref, scope="tenant", **fields)


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant_entry",
    [
        pytest.param(_tenant("t", op="iwfix.vm.stop", always_wait=False), id="same-rank"),
        pytest.param(_tenant("t", op="iwfix.vm.st*", always_wait=False), id="pattern"),
        pytest.param(_tenant("t", connector=_CONNECTOR, always_wait=False), id="connector"),
    ],
)
def test_a_tenant_entry_cannot_switch_off_an_install_wait(
    tenant_entry: ClassificationEntry,
) -> None:
    install = _install("i", connector="iwfix-*", always_wait=True)
    assert classify(_COORDS, [install, tenant_entry], _TAX).always_wait is True


def test_a_tenant_entry_can_add_a_wait() -> None:
    entries = [_tenant("t", op="iwfix.vm.stop", always_wait=True)]
    assert classify(_COORDS, entries, _TAX).always_wait is True


def test_the_installs_own_more_specific_entry_still_decides() -> None:
    entries = [
        _install("broad", connector="iwfix-*", always_wait=True),
        _install("narrow", op="iwfix.vm.stop", always_wait=False),
    ]
    assert classify(_COORDS, entries, _TAX).always_wait is False


def test_a_tenant_entry_still_sets_its_other_fields() -> None:
    entries = [
        _install("i", op="iwfix.vm.stop", always_wait=True),
        _tenant("t", op="iwfix.vm.stop", always_wait=False, category="compute"),
    ]
    result = classify(_COORDS, entries, _TAX)
    assert (result.always_wait, result.category, result.rule_ref) == (True, "compute", "t")


# ---------------------------------------------------------------------------
# The policy file
# ---------------------------------------------------------------------------


def _op(op_id: str) -> KnownOp:
    return KnownOp(
        coords=OpCoordinates(_CONNECTOR, "vm", op_id),
        safety_level="safe",
        shows_secret=False,
        caller_address=False,
    )


_OPS = [_op("iwfix.vm.list"), _op("iwfix.vm.stop")]


def test_a_tenant_file_that_sets_an_install_wait_back_is_refused() -> None:
    mistakes = check_document(
        _TENANT_UNDO, scope="tenant", install_document=_INSTALL_WAIT, ops=_OPS
    )
    assert mistakes == [f"`classifications[0]` {_REFUSAL}"]


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            {"classifications": [{"match": {"op": "iwfix.vm.list"}, "always_wait": True}]},
            id="adds-a-wait",
        ),
        pytest.param(
            {"classifications": [{"match": {"op": "iwfix.vm.list"}, "always_wait": False}]},
            id="false-where-the-install-does-not-wait",
        ),
    ],
)
def test_a_tenant_file_that_adds_or_leaves_the_install_wait_is_fine(
    document: dict[str, Any],
) -> None:
    assert check_document(document, scope="tenant", install_document=_INSTALL_WAIT, ops=_OPS) == []


def test_an_install_file_with_a_wait_has_no_warning() -> None:
    assert check_document(_INSTALL_WAIT, scope="install", install_document=None, ops=_OPS) == []


# ---------------------------------------------------------------------------
# Database: two tenants, the upgrade case, the export, the single edit
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


@pytest.fixture
async def ops(embedding: AsyncMock) -> None:
    for op_id in ("iwfix.vm.list", "iwfix.vm.stop"):
        await register_typed_operation(
            **_FIX,
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="vm",
            when_to_use="Fixture group.",
            embedding_service=embedding,
        )


async def _store(tenant_id: uuid.UUID | None, document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


async def _waits(tenant_id: uuid.UUID) -> dict[str, tuple[bool, bool]]:
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "iwfix")
                )
            ).scalars()
        )
        policies = await effective_policy_map(session, tenant_id=tenant_id, rows=rows)
    return {k.op_id: (p.requires_approval, p.always_wait) for k, p in policies.items()}


def _operator(tenant_id: uuid.UUID = _TENANT) -> Operator:
    return Operator(
        sub="admin-a",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_an_install_wait_holds_in_two_tenants_and_after_the_upgrade() -> None:
    await _store(None, _INSTALL_WAIT)
    # Stored before the upgrade: this entry used to switch the wait off.
    await _store(_TENANT, _TENANT_UNDO)
    for tenant_id in (_TENANT, _OTHER_TENANT):
        waits = await _waits(tenant_id)
        assert waits["iwfix.vm.stop"] == (True, True), tenant_id
        assert waits["iwfix.vm.list"] == (False, False), tenant_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_evaluator_and_its_compare_run_see_the_install_wait() -> None:
    await _store(None, _INSTALL_WAIT)
    await _store(_TENANT, _TENANT_UNDO)
    row = await lookup_descriptor(tenant_id=_TENANT, op_id="iwfix.vm.stop", **_FIX)
    assert row is not None
    asked = asked_of(row, connector_id=_CONNECTOR, target=None)
    assert asked.always_wait is True
    assert "classified_always_wait" in asked.op_class
    asker = Asker(
        kind="person",
        sub="admin-a",
        tenant_id=_TENANT,
        roles=frozenset({"tenant_admin"}),
        via="web",
    )
    assert evaluate(asker, asked, Lookups()).verdict == Verdict.WAIT


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_export_marks_an_entry_that_has_no_effect() -> None:
    await _store(None, _INSTALL_WAIT)
    await _store(
        _TENANT,
        {
            "classifications": [
                {"match": {"op": "iwfix.vm.list"}, "category": "compute"},
                *_TENANT_UNDO["classifications"],
            ]
        },
    )
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        plan = await build_plan(session, scope="tenant", tenant_id=_TENANT, content=text.encode())
    assert text == (
        "policy: v1\n"
        "classifications:\n"
        "- match:\n"
        "    op: iwfix.vm.list\n"
        "  category: compute\n"
        f"# Has no effect: `classifications[1]` {_REFUSAL}\n"
        "- match:\n"
        "    op: iwfix.vm.*\n"
        "  always_wait: false\n"
    )
    # The same entry in a new import is refused.
    assert plan.mistakes == [f"`classifications[1]` {_REFUSAL}"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_export_has_no_mark_without_an_install_wait() -> None:
    await _store(_TENANT, _TENANT_UNDO)
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
    assert "#" not in text


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_single_edit_that_sets_an_install_wait_back_is_refused() -> None:
    await _store(None, _INSTALL_WAIT)
    edit = ClassificationEdit(
        match={"op": "iwfix.vm.*"}, category=None, labels=None, always_wait=False
    )
    preview = await preview_classification_edit(_operator(), edit)
    assert preview.conflict == f"This edit {_REFUSAL}"
    with pytest.raises(ClassificationEditError, match="A tenant can make more operations wait"):
        await request_classification_edit(_operator(), edit)
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError, match="A tenant can make more operations wait"):
            await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
    async with get_sessionmaker()() as session:
        stored = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id == _TENANT))
        ).scalar_one_or_none()
    assert stored is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_single_edit_that_adds_a_wait_is_stored() -> None:
    await _store(None, _INSTALL_WAIT)
    edit = ClassificationEdit(
        match={"op": "iwfix.vm.list"}, category=None, labels=None, always_wait=True
    )
    preview = await preview_classification_edit(_operator(), edit)
    assert preview.conflict is None
    async with get_sessionmaker()() as session, session.begin():
        await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
    assert (await _waits(_TENANT))["iwfix.vm.list"] == (True, True)
    assert (await _waits(_OTHER_TENANT))["iwfix.vm.list"] == (False, False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_category_edit_on_an_old_entry_is_not_refused() -> None:
    """Only an edit that sets ``always_wait: false`` is refused, not one that keeps an old value."""
    await _store(None, _INSTALL_WAIT)
    await _store(_TENANT, _TENANT_UNDO)
    edit = ClassificationEdit(
        match={"op": "iwfix.vm.*"}, category="compute", labels=None, always_wait=None
    )
    async with get_sessionmaker()() as session, session.begin():
        await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
    assert (await _waits(_TENANT))["iwfix.vm.stop"] == (True, True)
