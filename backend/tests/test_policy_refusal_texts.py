# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The refusal text for a wide tenant entry names the connector.

One op id can exist on two connectors (two versions of one product). The
install sets a risk level for the op on one connector only. The tenant
has a wide entry with a lower level, and a stricter wide entry on the
other connector.

* The text names the op and its connector. The entry it suggests, added
  as written, changes the level of no other operation. An entry without
  the connector would lower the op on the other connector.
* An entry that matches the op on both connectors is a wide entry too.
* The import mistake and the export comment are the same text.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, TenantPolicy
from meho_backplane.operations.op_policy import effective_policy_map
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.classification import (
    OpCoordinates,
    Taxonomy,
    classify,
    entries_from_document,
)
from meho_backplane.policy.service import build_plan, export_policy
from meho_backplane.policy.validate import KnownOp, check_document
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006489")
_OLD = "rtfix-1.x"
_NEW = "rtfix-2.x"
_STOP = "rtfix.vm.stop"
_LIST = "rtfix.vm.list"
_TAX = Taxonomy(categories=("compute", "other"), default="other")

#: The install sets the op to ``dangerous`` on the old connector only.
_INSTALL = {"classifications": [{"match": {"connector": _OLD, "op": _STOP}, "risk": "dangerous"}]}
#: A wide entry with a lower level, and a stricter wide entry on the new connector.
_TENANT_DOC: dict[str, Any] = {
    "classifications": [
        {"match": {"connector": "rtfix-*"}, "risk": "caution"},
        {"match": {"connector": _NEW}, "risk": "destructive"},
    ]
}
_SUGGESTION = re.compile(
    r"Add a tenant entry for `(?P<op>[^`]+)` of connector `(?P<connector>[^`]+)` "
    r"with `risk: (?P<level>[a-z]+)` or higher"
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


def _op(connector: str, op_id: str) -> KnownOp:
    return KnownOp(
        coords=OpCoordinates(connector, "vm", op_id),
        safety_level="safe",
        shows_secret=False,
        caller_address=False,
    )


_OPS = [_op(connector, op_id) for connector in (_OLD, _NEW) for op_id in (_LIST, _STOP)]


def _levels(document: dict[str, Any]) -> dict[tuple[str, str], str | None]:
    entries = [
        *entries_from_document(_INSTALL, scope="install"),
        *entries_from_document(document, scope="tenant"),
    ]
    return {
        (op.coords.connector_id, op.coords.op_id): classify(op.coords, entries, _TAX).risk
        for op in _OPS
    }


def _with(entry: dict[str, Any]) -> dict[str, Any]:
    return {"classifications": [*_TENANT_DOC["classifications"], entry]}


def test_the_suggested_entry_changes_no_other_operations_level() -> None:
    mistakes = check_document(_TENANT_DOC, scope="tenant", install_document=_INSTALL, ops=_OPS)
    assert len(mistakes) == 1
    found = _SUGGESTION.search(mistakes[0])
    assert found is not None, mistakes[0]
    assert found.group("op", "connector", "level") == (_STOP, _OLD, "dangerous")

    suggested = {
        "match": {"connector": found["connector"], "op": found["op"]},
        "risk": found["level"],
    }
    fixed = _with(suggested)
    assert check_document(fixed, scope="tenant", install_document=_INSTALL, ops=_OPS) == []
    before, after = _levels(_TENANT_DOC), _levels(fixed)
    assert after == before
    assert after[(_NEW, _STOP)] == "destructive"

    # The text without the connector would lower the op on the new connector.
    without_connector = _levels(_with({"match": {"op": _STOP}, "risk": "dangerous"}))
    assert without_connector[(_NEW, _STOP)] == "dangerous"


def test_an_entry_for_the_op_on_both_connectors_is_a_wide_entry() -> None:
    tenant = {"classifications": [{"match": {"op": _STOP}, "risk": "caution"}]}
    mistakes = check_document(tenant, scope="tenant", install_document=_INSTALL, ops=_OPS)
    assert len(mistakes) == 1
    assert "still counts for the other operations it matches" in mistakes[0]
    assert f"of connector `{_OLD}`" in mistakes[0]
    assert "Set `risk:" not in mistakes[0]


def test_the_suggested_entry_for_a_group_entry_names_the_group() -> None:
    # The entry names a group, so it outranks an entry with only a connector and an op.
    tenant = {"classifications": [{"match": {"group": "vm", "op": _STOP}, "risk": "caution"}]}
    mistakes = check_document(tenant, scope="tenant", install_document=_INSTALL, ops=_OPS)
    assert len(mistakes) == 1
    found = re.search(
        r"Add a tenant entry for `(?P<op>[^`]+)` of connector `(?P<connector>[^`]+)` "
        r"and group `(?P<group>[^`]+)` with `risk: (?P<level>[a-z]+)` or higher",
        mistakes[0],
    )
    assert found is not None, mistakes[0]
    assert found.group("op", "connector", "group", "level") == (_STOP, _OLD, "vm", "dangerous")

    suggested = {
        "match": {"connector": found["connector"], "group": found["group"], "op": found["op"]},
        "risk": found["level"],
    }
    fixed = {"classifications": [*tenant["classifications"], suggested]}
    assert check_document(fixed, scope="tenant", install_document=_INSTALL, ops=_OPS) == []
    assert _levels(fixed) == _levels(tenant)

    # Without the group, the entry is refused again.
    del suggested["match"]["group"]
    assert check_document(fixed, scope="tenant", install_document=_INSTALL, ops=_OPS) != []


def test_an_entry_for_the_op_on_one_connector_gets_the_narrow_text() -> None:
    tenant = {"classifications": [{"match": {"connector": _OLD, "op": _STOP}, "risk": "caution"}]}
    mistakes = check_document(tenant, scope="tenant", install_document=_INSTALL, ops=_OPS)
    assert len(mistakes) == 1
    assert mistakes[0].endswith(
        "Set `risk: dangerous` or higher in this entry; its other fields can stay."
    )


# ---------------------------------------------------------------------------
# Database: the export comment and the import mistake
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
    """The same two op ids on two versions of one product."""
    for version in ("1.x", "2.x"):
        for op_id in (_LIST, _STOP):
            await register_typed_operation(
                product="rtfix",
                version=version,
                impl_id="rtfix",
                op_id=op_id,
                handler=_handler,
                summary="Fixture operation.",
                description="Fixture operation.",
                parameter_schema={"type": "object", "properties": {}},
                group_key="vm",
                when_to_use="Fixture group.",
                safety_level="safe",
                embedding_service=embedding,
            )


async def _store(tenant_id: uuid.UUID | None, document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


async def _stored_levels() -> dict[tuple[str, str], str]:
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "rtfix")
                )
            ).scalars()
        )
        policies = await effective_policy_map(session, tenant_id=_TENANT, rows=rows)
    return {(f"{k.impl_id}-{k.version}", k.op_id): p.safety_level for k, p in policies.items()}


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_export_comment_and_the_import_mistake_are_the_same_text() -> None:
    await _store(None, _INSTALL)
    await _store(_TENANT, _TENANT_DOC)
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        plan = await build_plan(session, scope="tenant", tenant_id=_TENANT, content=text.encode())
    notes = [line for line in text.splitlines() if line.startswith("# Has no effect: ")]
    assert len(notes) == 1
    assert f"`{_STOP}` of connector `{_OLD}`" in notes[0]
    assert plan.mistakes == [notes[0].removeprefix("# Has no effect: ")]
    levels = await _stored_levels()
    assert levels[(_OLD, _STOP)] == "dangerous"
    assert levels[(_NEW, _STOP)] == "destructive"
