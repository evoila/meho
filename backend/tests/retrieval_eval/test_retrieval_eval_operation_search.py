# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The operations surface of the retrieval eval searches operations (#4752).

Operations live in ``endpoint_descriptor``. The eval must score them with
the operation search the agent uses, not with the ``documents`` search.
A question whose connector cannot be searched on this install is "not
run": it carries a reason and does not turn the verdict red.

The tests run against the migrated SQLite engine of the test suite, so
the search takes its SQLite path (substring ranking on summary and
description).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from scripts.retrieval_eval.corpus import load_corpus
from scripts.retrieval_eval.operation_search import (
    OperationSearchResult,
    search_corpus_operations,
)
from scripts.retrieval_eval.runner import eval_all, eval_surface
from sqlalchemy import func, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, EndpointDescriptor
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_CORPUS_CONNECTOR = "vmware-rest-9.0"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the search from loading the embedding model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)


async def _refuse_documents_search(**_: Any) -> list[Any]:
    raise AssertionError("the operations surface must not search the documents table")


async def _seed_corpus_operations(
    *,
    tenant_id: uuid.UUID | None = None,
    is_enabled: bool = True,
    owner_package: str | None = None,
) -> None:
    """Write one ingested operation per corpus question for the corpus connector.

    The summary of each row is its question, so the SQLite search ranks
    the right row first. No ``documents`` row is written.
    """
    now = datetime.now(UTC)
    seen: set[str] = set()
    async with get_sessionmaker()() as session, session.begin():
        for row in load_corpus("operations"):
            op_id = row.expected_op_ids[0]
            if op_id in seen:
                continue
            seen.add(op_id)
            method, _, path = op_id.partition(":")
            session.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    owner_package=owner_package,
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    handler_ref=None,
                    summary=row.query,
                    description=f"Operation for: {row.query}.",
                    group_id=None,
                    tags=[],
                    parameter_schema={"type": "object"},
                    response_schema=None,
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=is_enabled,
                    embedding=None,
                    custom_description=None,
                    custom_notes=None,
                    created_at=now,
                    updated_at=now,
                )
            )


async def _document_count() -> int:
    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count()).select_from(Document))).scalar())


@pytest.mark.asyncio
async def test_live_operations_eval_finds_operations_in_endpoint_descriptor() -> None:
    await _seed_corpus_operations()
    assert await _document_count() == 0

    result = await eval_surface(
        "operations", tenant_id=_TENANT_A, retrieve_fn=_refuse_documents_search
    )

    assert result.not_run_count == 0
    assert result.query_count == len(load_corpus("operations"))
    for query in result.queries:
        assert query.not_run_reason is None
        assert query.meho_hits[0] == query.expected_hits[0]
        assert query.reciprocal_rank == 1.0
    assert result.mrr == 1.0
    assert result.coverage == 1.0
    # Other "list ..." rows also match, so precision@5 is below 1 but not 0.
    assert result.precision_at_5 > 0.0


@pytest.mark.asyncio
async def test_operations_eval_without_the_connector_is_not_run_and_not_red() -> None:
    result = await eval_all(
        tenant_id=_TENANT_A,
        retrieve_fn=_refuse_documents_search,
        surfaces=["operations"],
    )

    surface = result.surfaces[0]
    assert surface.query_count == 0
    assert surface.not_run_count == len(load_corpus("operations"))
    assert surface.verdict == "green"
    assert result.overall_verdict == "green"
    for query in surface.queries:
        assert query.meho_hits == []
        assert query.not_run_reason == (
            f"connector {_CORPUS_CONNECTOR!r} has no operations on this install"
        )


@pytest.mark.asyncio
async def test_operations_eval_with_no_enabled_operation_is_not_run() -> None:
    await _seed_corpus_operations(is_enabled=False)

    result = await eval_surface("operations", tenant_id=_TENANT_A)

    assert result.query_count == 0
    assert result.verdict == "green"
    assert {q.not_run_reason for q in result.queries} == {
        f"connector {_CORPUS_CONNECTOR!r} has no enabled operations"
    }


@pytest.mark.asyncio
async def test_operations_eval_with_package_not_installed_is_not_run() -> None:
    await _seed_corpus_operations(owner_package="missing-connector-pkg")

    found = await search_corpus_operations(
        tenant_id=_TENANT_A, connector_id=_CORPUS_CONNECTOR, query="list hosts", limit=5
    )

    assert found.op_ids == []
    assert found.not_run_reason is not None
    assert "missing-connector-pkg" in found.not_run_reason


@pytest.mark.asyncio
async def test_operations_of_another_tenant_are_not_searched() -> None:
    await _seed_corpus_operations(tenant_id=_TENANT_B)

    found = await search_corpus_operations(
        tenant_id=_TENANT_A, connector_id=_CORPUS_CONNECTOR, query="list hosts", limit=5
    )

    assert found == OperationSearchResult(
        not_run_reason=f"connector {_CORPUS_CONNECTOR!r} has no operations on this install"
    )


@pytest.mark.asyncio
async def test_a_real_miss_is_scored_not_skipped() -> None:
    await _seed_corpus_operations()

    found = await search_corpus_operations(
        tenant_id=_TENANT_A, connector_id=_CORPUS_CONNECTOR, query="zzz-unrelated", limit=5
    )

    assert found == OperationSearchResult(op_ids=[], not_run_reason=None)


@pytest.mark.asyncio
async def test_not_run_questions_do_not_move_the_metrics() -> None:
    """Half the questions not run, half found: the scored half decides the verdict."""
    rows = load_corpus("operations")
    answers = {row.query: row.expected_op_ids[0] for row in rows[: len(rows) // 2]}

    async def half(
        *, tenant_id: uuid.UUID, connector_id: str, query: str, limit: int
    ) -> OperationSearchResult:
        if query in answers:
            return OperationSearchResult(op_ids=[answers[query]])
        return OperationSearchResult(not_run_reason="not here")

    result = await eval_surface("operations", tenant_id=_TENANT_A, search_operations_fn=half)

    assert result.query_count == len(answers)
    assert result.not_run_count == len(rows) - len(answers)
    assert len(result.queries) == len(rows)
    assert result.mrr == 1.0
    assert result.verdict == "green"


@pytest.mark.asyncio
async def test_search_is_cut_to_the_limit() -> None:
    await _seed_corpus_operations()

    found = await search_corpus_operations(
        tenant_id=_TENANT_A, connector_id=_CORPUS_CONNECTOR, query="list", limit=2
    )

    assert len(found.op_ids) == 2
