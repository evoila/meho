# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration and contract tests for the gh-rest git composites.

Checks, for ``gh.composite.git_ref_view``, ``gh.composite.git_tree_view``
and ``gh.composite.branch_commit_files``:

* the ``safety_level`` and ``requires_approval`` of each operation (the two
  reads run at once; the write always waits for approval);
* the ``git`` group with its text;
* the write has a preview builder;
* every operation meets the operation contract rules that
  :mod:`tests.test_vmware_operation_contracts` checks (summary, field
  descriptions, response schema, agent guidance, example, no ticket
  numbers, approval, group, preview builder).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.github.composites import (
    register_github_composite_operations,
)
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.settings import get_settings
from tests.test_vmware_operation_contracts import OperationContract, check_operation

_GIT_OP_IDS: tuple[str, ...] = (
    "gh.composite.git_ref_view",
    "gh.composite.git_tree_view",
    "gh.composite.branch_commit_files",
)

_EXPECTED_POSTURE: dict[str, tuple[str, bool]] = {
    "gh.composite.git_ref_view": ("safe", False),
    "gh.composite.git_tree_view": ("safe", False),
    "gh.composite.branch_commit_files": ("dangerous", True),
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _registered_git_ops() -> tuple[list[EndpointDescriptor], dict[int, OperationGroup]]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_GIT_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
        groups = {g.id: g for g in (await session.execute(select(OperationGroup))).scalars()}
    return list(rows), groups


@pytest.mark.asyncio
async def test_git_ops_have_the_expected_safety_and_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, _ = await _registered_git_ops()
    posture = {row.op_id: (row.safety_level, row.requires_approval) for row in rows}
    assert posture == _EXPECTED_POSTURE


@pytest.mark.asyncio
async def test_git_ops_land_in_the_git_group_with_a_text(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, groups = await _registered_git_ops()
    assert len(rows) == len(_GIT_OP_IDS)
    for row in rows:
        group = groups[row.group_id]
        assert group.group_key == "git"
        assert group.when_to_use and "branch" in group.when_to_use


@pytest.mark.asyncio
async def test_git_ops_are_composites_on_the_gh_rest_triple(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, _ = await _registered_git_ops()
    for row in rows:
        assert row.source_kind == "composite"
        assert (row.product, row.version, row.impl_id) == ("gh", "3", "gh-rest")
        assert row.handler_ref.startswith("meho_backplane.connectors.github.composites._git.")


@pytest.mark.asyncio
async def test_git_ops_meet_the_operation_contract_rules(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each operation passes the nine contract rules with no gap."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, groups = await _registered_git_ops()
    problems: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        group = groups.get(row.group_id) if row.group_id else None
        contract = OperationContract(
            connector=row.impl_id,
            op_id=row.op_id,
            summary=row.summary,
            description=row.description,
            parameter_schema=row.parameter_schema or {},
            response_schema=row.response_schema,
            llm_instructions=row.llm_instructions,
            safety_level=row.safety_level,
            requires_approval=row.requires_approval,
            group_text=group.when_to_use if group is not None else None,
            has_preview_builder=row.op_id in _PREVIEW_BUILDERS,
        )
        found = check_operation(contract)
        if found:
            problems[row.op_id] = found
    assert problems == {}


@pytest.mark.asyncio
async def test_the_write_has_a_preview_builder_and_the_reads_do_not(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    assert "gh.composite.branch_commit_files" in _PREVIEW_BUILDERS
    assert "gh.composite.git_ref_view" not in _PREVIEW_BUILDERS
    assert "gh.composite.git_tree_view" not in _PREVIEW_BUILDERS
