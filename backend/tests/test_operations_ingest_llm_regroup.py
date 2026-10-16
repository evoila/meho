# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Re-run grouping on a connector that already has groups (#3713).

Covers the partial-regrouping rules in
:mod:`meho_backplane.operations.ingest.llm_groups`:

* a group that holds a typed op is never a Pass-2 candidate, so no
  ingested op is filed into it;
* a group that holds only a composite op is still a candidate (composites
  share the normal semantic groups);
* when only typed groups exist, the run is a full grouping run and
  creates new groups;
* ops that fit no existing group get an "extend" Pass 1 that may
  create new groups for them;
* the result reports, per group, how many ops the run added and how
  many typed, composite, and ingested ops the group holds.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import (
    GroupSummary,
    register_ingested_operations,
    run_llm_grouping,
)
from meho_backplane.operations.ingest._llm_grouping_internals import (
    parse_proposal_response,
    render_propose_groups_prompt,
)
from meho_backplane.settings import get_settings
from tests.fixtures.llm_groups import small_corpus

_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL = "vmware-rest"
_TYPED_GROUP_KEY = "vmware-object-collect"
_TYPED_OP_ID = "vmware.object.collect"
_COMPOSITE_OP_ID = "vmware.composite.inventory.summary"
_OPERATOR_SUB = "op-test-regroup"
_OPERATOR_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_connector_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> Any:
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


class _StubLlm:
    """Return canned responses in order; record every user prompt."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        self.prompts.append(user_prompt)
        if not self._responses:
            raise AssertionError(f"stub LLM ran out of responses (call #{len(self.prompts)})")
        return self._responses.pop(0)


async def _seed(
    embedding_service: Any,
    *,
    ingested_group: bool,
    composite_in_ingested_group: bool = False,
) -> None:
    """Seed a typed group with one typed op, the 5 small-corpus ops, and
    optionally an ingested group ``inventory`` (empty, or holding one
    composite op)."""
    await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        spec_source="vcenter.yaml",
        operations=small_corpus.PROTOS,
        embedding_service=embedding_service,
    )
    async with get_sessionmaker()() as session:
        typed_group = OperationGroup(
            tenant_id=None,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL,
            group_key=_TYPED_GROUP_KEY,
            name="Object Collect",
            when_to_use="Use to read many properties of many objects in one call.",
            review_status="enabled",
        )
        session.add(typed_group)
        await session.flush()
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL,
                op_id=_TYPED_OP_ID,
                source_kind="typed",
                handler_ref="tests:typed_handler",
                summary="Collect object properties",
                group_id=typed_group.id,
                tags=[],
                parameter_schema={"type": "object", "properties": {}},
                safety_level="safe",
            )
        )
        if ingested_group:
            proposal = parse_proposal_response(small_corpus.STUB_PROPOSE_RESPONSE)[0]
            inventory_group = OperationGroup(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL,
                group_key=proposal.group_key,
                name=proposal.name,
                when_to_use=proposal.when_to_use,
                review_status="staged",
            )
            session.add(inventory_group)
            if composite_in_ingested_group:
                await session.flush()
                session.add(
                    EndpointDescriptor(
                        tenant_id=None,
                        product=_PRODUCT,
                        version=_VERSION,
                        impl_id=_IMPL,
                        op_id=_COMPOSITE_OP_ID,
                        source_kind="composite",
                        handler_ref="tests:composite_handler",
                        summary="Summarise the inventory",
                        group_id=inventory_group.id,
                        tags=[],
                        parameter_schema={"type": "object", "properties": {}},
                        safety_level="safe",
                    )
                )
        await session.commit()


async def _run(stub: _StubLlm) -> Any:
    return await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
    )


async def _group_of(op_id: str) -> str | None:
    async with get_sessionmaker()() as session:
        row: str | None = (
            await session.execute(
                select(OperationGroup.group_key)
                .join(EndpointDescriptor, EndpointDescriptor.group_id == OperationGroup.id)
                .where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one_or_none()
    return row


def _summary(result: Any, key: str) -> GroupSummary:
    matches = [g for g in result.groups if g.group_key == key]
    assert len(matches) == 1, f"no summary for {key!r}: {result.groups}"
    return matches[0]


@pytest.mark.asyncio
async def test_only_typed_groups_runs_full_grouping_and_leaves_typed_group_alone(
    stub_embedding_service: Any,
) -> None:
    """Acceptance fixture: one typed group + ungrouped ingested ops.

    The typed group is not a candidate, so the run proposes new groups
    (Pass 1) and assigns every ingested op into them (Pass 2). The typed
    group keeps exactly its one typed op.
    """
    await _seed(stub_embedding_service, ingested_group=False)
    stub = _StubLlm([small_corpus.STUB_PROPOSE_RESPONSE, small_corpus.STUB_ASSIGNMENT_RESPONSE])

    result = await _run(stub)

    assert len(stub.prompts) == 2
    assert result.groups_created == 2
    ungrouped = len(small_corpus.PROTOS)
    assert result.operations_assigned + result.operations_unassigned == ungrouped
    assert result.operations_assigned / ungrouped >= 0.9
    # The typed group is never offered to the model as a target.
    assert all(_TYPED_GROUP_KEY not in prompt for prompt in stub.prompts)

    typed = _summary(result, _TYPED_GROUP_KEY)
    assert typed == GroupSummary(
        group_key=_TYPED_GROUP_KEY,
        created=False,
        operations_added=0,
        typed_operation_count=1,
        composite_operation_count=0,
        ingested_operation_count=0,
    )
    inventory = _summary(result, "inventory")
    assert inventory.created is True
    assert inventory.operations_added == 2
    assert inventory.ingested_operation_count == 2
    assert inventory.typed_operation_count == 0
    assert _summary(result, "vm_lifecycle").operations_added == 3
    assert await _group_of(_TYPED_OP_ID) == _TYPED_GROUP_KEY


@pytest.mark.asyncio
async def test_regroup_extends_with_new_groups_for_leftovers(
    stub_embedding_service: Any,
) -> None:
    """Pass 2 against the ingested group, then an "extend" Pass 1 for leftovers.

    A proposal that reuses an existing key is dropped, so no persisted
    group is renamed or duplicated.
    """
    await _seed(stub_embedding_service, ingested_group=True)
    first_pass2 = json.dumps(
        {
            "GET:/api/vcenter/cluster": "inventory",
            "GET:/api/vcenter/vm": "inventory",
            "POST:/api/vcenter/vm": "none",
            "DELETE:/api/vcenter/vm/{vm}": "none",
            # The typed group is not a candidate, so this is refused.
            "POST:/api/vcenter/vm/{vm}/snapshot": _TYPED_GROUP_KEY,
        }
    )
    # The extend Pass 1 proposes vm_lifecycle plus a clash with "inventory".
    extend_pass1 = small_corpus.STUB_PROPOSE_RESPONSE
    leftover_pass2 = json.dumps(
        {
            "POST:/api/vcenter/vm": "vm_lifecycle",
            "DELETE:/api/vcenter/vm/{vm}": "vm_lifecycle",
            "POST:/api/vcenter/vm/{vm}/snapshot": "vm_lifecycle",
        }
    )
    stub = _StubLlm([first_pass2, extend_pass1, leftover_pass2])

    result = await _run(stub)

    assert len(stub.prompts) == 3
    assert result.llm_call_count == 3
    assert result.groups_created == 1
    assert result.operations_assigned == 5
    assert result.operations_unassigned == 0
    assert all(_TYPED_GROUP_KEY not in prompt for prompt in stub.prompts)
    # The extend prompt lists the existing group and asks for new ones only.
    assert "already has these operation groups" in stub.prompts[1]
    assert "- inventory (Inventory)" in stub.prompts[1]
    # The leftover Pass 2 offers only the new group.
    assert "- inventory (" not in stub.prompts[2]
    assert "- vm_lifecycle (" in stub.prompts[2]

    inventory = _summary(result, "inventory")
    assert (inventory.created, inventory.operations_added) == (False, 2)
    vm_lifecycle = _summary(result, "vm_lifecycle")
    assert (vm_lifecycle.created, vm_lifecycle.operations_added) == (True, 3)
    assert _summary(result, _TYPED_GROUP_KEY).operations_added == 0
    assert [g.group_key for g in result.groups] == sorted(g.group_key for g in result.groups)

    async with get_sessionmaker()() as session:
        keys = (
            (
                await session.execute(
                    select(OperationGroup.group_key).where(OperationGroup.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(keys) == ["inventory", "vm_lifecycle", _TYPED_GROUP_KEY]


@pytest.mark.asyncio
async def test_group_with_composite_op_still_takes_ingested_ops(
    stub_embedding_service: Any,
) -> None:
    """A snake_case group holding one composite op is a Pass-2 target.

    Composites live in the shared semantic groups (``vm``, ``storage``,
    ...). They must not make the group typed, or regrouping could never
    fill those groups.
    """
    await _seed(stub_embedding_service, ingested_group=True, composite_in_ingested_group=True)
    first_pass2 = json.dumps(
        {
            "GET:/api/vcenter/cluster": "inventory",
            "GET:/api/vcenter/vm": "inventory",
            "POST:/api/vcenter/vm": "inventory",
            "DELETE:/api/vcenter/vm/{vm}": "inventory",
            "POST:/api/vcenter/vm/{vm}/snapshot": "inventory",
        }
    )
    stub = _StubLlm([first_pass2])

    result = await _run(stub)

    # The first call is Pass 2, and it offers the composite's group.
    assert len(stub.prompts) == 1
    assert stub.prompts[0].startswith("Assign each operation")
    assert "- inventory (Inventory)" in stub.prompts[0]
    assert _TYPED_GROUP_KEY not in stub.prompts[0]
    assert result.groups_created == 0
    assert result.operations_assigned == 5
    assert result.operations_unassigned == 0
    assert await _group_of("POST:/api/vcenter/vm") == "inventory"

    inventory = _summary(result, "inventory")
    assert inventory == GroupSummary(
        group_key="inventory",
        created=False,
        operations_added=5,
        typed_operation_count=0,
        composite_operation_count=1,
        ingested_operation_count=5,
    )


@pytest.mark.asyncio
async def test_regroup_extend_may_propose_nothing(
    stub_embedding_service: Any,
) -> None:
    """An empty extend proposal leaves the leftovers ungrouped, no extra Pass 2."""
    await _seed(stub_embedding_service, ingested_group=True)
    first_pass2 = json.dumps(
        {
            "GET:/api/vcenter/cluster": "inventory",
            "GET:/api/vcenter/vm": "inventory",
        }
    )
    stub = _StubLlm([first_pass2, "[]"])

    result = await _run(stub)

    assert result.llm_call_count == 2
    assert result.groups_created == 0
    assert result.operations_assigned == 2
    assert result.operations_unassigned == 3
    assert await _group_of("POST:/api/vcenter/vm") is None


@pytest.mark.asyncio
async def test_regroup_extend_invalid_output_keeps_first_assignments(
    stub_embedding_service: Any,
) -> None:
    """A bad extend Pass-1 answer does not undo the Pass-2 assignments."""
    await _seed(stub_embedding_service, ingested_group=True)
    first_pass2 = json.dumps({"GET:/api/vcenter/cluster": "inventory"})
    stub = _StubLlm([first_pass2, "not json"])

    result = await _run(stub)

    assert result.llm_call_count == 2
    assert result.groups_created == 0
    assert result.operations_assigned == 1
    assert result.operations_unassigned == 4
    assert await _group_of("GET:/api/vcenter/cluster") == "inventory"


def test_render_propose_groups_prompt_extend_mode() -> None:
    """With existing groups the prompt asks for new groups only, maybe none."""
    existing = parse_proposal_response(small_corpus.STUB_PROPOSE_RESPONSE)[:1]
    rendered = render_propose_groups_prompt(
        connector_id="vmware-rest-9.0",
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        operations=small_corpus.PROTOS,
        max_groups=4,
        existing_groups=existing,
    )
    assert "- inventory (Inventory): Use when" in rendered
    assert "propose at\nmost 4 NEW operation GROUPS" in rendered
    assert "return an empty array: []" in rendered
    assert "between" not in rendered
    assert "Anchor your\ngroupings on the existing OpenAPI tags" in rendered
