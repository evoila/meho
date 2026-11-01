# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration, contract and audit tests for the gh-rest issue composites.

Checks, for ``gh.composite.issue_search``, ``gh.composite.issue_view``,
``gh.composite.issue_create`` and ``gh.composite.issue_comment``:

* the risk level and approval flag of each operation (the two reads run at
  once; the two writes wait for approval);
* the ``issues`` group with its text;
* every operation meets the operation contract rules of
  :mod:`tests.test_operation_contracts`;
* a call through MEHO's dispatcher writes exactly one audit row, and the
  writes wait for approval until they are approved.

The connector is a session stub; no live GitHub. All names are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.github.composites import (
    register_github_composite_operations,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.settings import get_settings
from tests.test_operation_contracts import OperationContract, check_operation

_EXPECTED_POSTURE: dict[str, tuple[str, bool]] = {
    "gh.composite.issue_search": ("safe", False),
    "gh.composite.issue_view": ("safe", False),
    "gh.composite.issue_create": ("caution", True),
    "gh.composite.issue_comment": ("caution", True),
}
_ISSUE_OP_IDS = tuple(_EXPECTED_POSTURE)


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


async def _registered_issue_ops() -> tuple[list[EndpointDescriptor], dict[int, OperationGroup]]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_ISSUE_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
        groups = {g.id: g for g in (await session.execute(select(OperationGroup))).scalars()}
    return list(rows), groups


@pytest.mark.asyncio
async def test_issue_ops_have_the_expected_safety_and_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, _ = await _registered_issue_ops()
    assert {row.op_id: (row.safety_level, row.requires_approval) for row in rows} == (
        _EXPECTED_POSTURE
    )


@pytest.mark.asyncio
async def test_issue_ops_are_composites_in_the_issues_group(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, groups = await _registered_issue_ops()
    assert len(rows) == len(_ISSUE_OP_IDS)
    for row in rows:
        assert row.source_kind == "composite"
        assert (row.product, row.version, row.impl_id) == ("gh", "3", "gh-rest")
        assert row.handler_ref.startswith("meho_backplane.connectors.github.composites._issues.")
        group = groups[row.group_id]
        assert group.group_key == "issues"
        assert group.when_to_use and "issue_search" in group.when_to_use


@pytest.mark.asyncio
async def test_issue_ops_meet_the_operation_contract_rules(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each operation passes the ten contract rules with no gap."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    rows, groups = await _registered_issue_ops()
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


# ---------------------------------------------------------------------------
# One audit row per call, through the real dispatcher
# ---------------------------------------------------------------------------


class _FakeFingerprint:
    def __init__(self, version: str | None = "3") -> None:
        self.version = version


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "gh"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "gh-target"
        self.host = "api.github.com"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None


class _StubGitHubConnector(HttpConnector):
    """gh-rest stand-in whose session answers from saved payloads."""

    product = "gh"
    version = "3"
    impl_id = "gh-rest"
    supported_version_range = ">=3,<4"
    priority = 1

    gets: ClassVar[dict[str, Any]] = {
        "/repos/acme/widgets/issues": [],
        "/repos/acme/widgets/issues/7": {"number": 7, "id": 9007, "node_id": "I_7", "comments": 0},
        "/repos/acme/widgets/issues/7/parent": {},
        "/repos/acme/widgets/issues/7/sub_issues": [],
    }
    posts: ClassVar[dict[str, Any]] = {
        "/repos/acme/widgets/issues": {"number": 8, "id": 9008, "node_id": "I_8"},
        "/repos/acme/widgets/issues/7/comments": {"id": 31},
    }
    post_calls: ClassVar[list[str]] = []

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(  # type: ignore[override]
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        return self.gets[path]

    async def _post_json(  # type: ignore[override]
        self, target: Any, path: str, *, operator: Operator, verb: str = "POST", **_: Any
    ) -> Any:
        self.post_calls.append(path)
        return self.posts[path]


def _operator() -> Operator:
    return Operator(
        sub="op-gh-issues-audit",
        name="GH Issues Audit Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c2c2"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _audit_rows(op_id: str) -> int:
    async with get_sessionmaker()() as session:
        stmt = select(func.count()).select_from(AuditLog).where(AuditLog.path == op_id)
        return int((await session.execute(stmt)).scalar_one())


_CALLS: dict[str, dict[str, Any]] = {
    "gh.composite.issue_search": {"owner": "acme", "repo": "widgets"},
    "gh.composite.issue_view": {"owner": "acme", "repo": "widgets", "issue_number": 7},
    "gh.composite.issue_create": {"owner": "acme", "repo": "widgets", "title": "New task"},
    "gh.composite.issue_comment": {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 7,
        "body": "Done.",
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ISSUE_OP_IDS)
async def test_each_call_writes_one_audit_row(
    op_id: str,
    stub_embedding_service: AsyncMock,
) -> None:
    """A read runs at once; a write runs once approved. Either way: one audit row."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    register_connector_v2(product="gh", version="3", impl_id="gh-rest", cls=_StubGitHubConnector)
    _StubGitHubConnector.post_calls.clear()
    needs_approval = _EXPECTED_POSTURE[op_id][1]

    result = await dispatch(
        operator=_operator(),
        connector_id="gh-rest-3",
        op_id=op_id,
        target=_FakeTarget(),
        params=_CALLS[op_id],
        _approved=needs_approval,
    )

    assert result.status == "ok", result.error
    assert await _audit_rows(op_id) == 1
    assert len(_StubGitHubConnector.post_calls) == (1 if needs_approval else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["gh.composite.issue_create", "gh.composite.issue_comment"])
async def test_the_writes_wait_for_approval(
    op_id: str,
    stub_embedding_service: AsyncMock,
) -> None:
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    register_connector_v2(product="gh", version="3", impl_id="gh-rest", cls=_StubGitHubConnector)
    _StubGitHubConnector.post_calls.clear()

    result = await dispatch(
        operator=_operator(),
        connector_id="gh-rest-3",
        op_id=op_id,
        target=_FakeTarget(),
        params=_CALLS[op_id],
    )

    assert result.status == "awaiting_approval", result.error
    assert _StubGitHubConnector.post_calls == []
