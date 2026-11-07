# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :mod:`meho_backplane.kb.similar`: similar entries are shown at write time.

The search itself (``retrieve``) needs PostgreSQL, so it is mocked here
with hits that carry a cosine score. Everything around it runs for real
on the SQLite test database: the stored entries, their revision ids, the
existing-slug test, and the MCP and REST surfaces.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1 import kb as kb_api
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.kb.schemas import KB_KIND_ENTRY, KB_SOURCE, InvalidKbSlugError
from meho_backplane.kb.service import KbService
from meho_backplane.kb.similar import SIMILAR_LIMIT, check_new_entry, find_similar_entries
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.tools.knowledge import _similar_check_payload
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import BLOCK_START
from tests._kb_embedding_stub import fake_embedding_service
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

pytestmark = pytest.mark.usefixtures("real_similar_check")

#: What a search returns: slug -> cosine score, for the plain search and
#: for the search narrowed to the same product and version.
ScoreMap = dict[str, float]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_INGEST_ROOT", str(tmp_path))
    monkeypatch.delenv("KB_SIMILAR_MIN_SCORE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_embedding() -> Iterator[None]:
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        yield


def _hit(slug: str, score: float, tenant_id: uuid.UUID) -> RetrievalHit:
    now = datetime.now(UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source=KB_SOURCE,
        source_id=slug,
        kind=KB_KIND_ENTRY,
        body=f"body of {slug}",
        doc_metadata={},
        created_at=now,
        updated_at=now,
        fused_score=score / 60,
        bm25_score=None,
        cosine_score=score,
        bm25_rank=None,
        cosine_rank=1,
    )


def _fake_retrieve(
    tenant_id: uuid.UUID, close: ScoreMap, same: ScoreMap | None = None
) -> tuple[AsyncMock, Callable[[], list[dict[str, Any]]]]:
    """Return a ``retrieve`` mock and a reader of the filters it got."""

    async def _retrieve(**kwargs: Any) -> list[RetrievalHit]:
        scores = same if kwargs.get("metadata_filters") else close
        return [_hit(slug, score, tenant_id) for slug, score in (scores or {}).items()]

    mock = AsyncMock(side_effect=_retrieve)
    return mock, lambda: [call.kwargs.get("metadata_filters") for call in mock.await_args_list]


async def _seed(
    tenant_id: uuid.UUID, slug: str, metadata: dict[str, object] | None = None, *, by: str = "a"
) -> None:
    await KbService().create_entry(
        tenant_id, slug, f"Stored text of {slug}.", metadata, actor_sub=by
    )


# ---------------------------------------------------------------------------
# The search and the answer
# ---------------------------------------------------------------------------


def test_default_min_score_is_pinned() -> None:
    """The threshold is a setting; its default is 0.85."""
    assert get_settings().kb_similar_min_score == 0.85


def test_min_score_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SIMILAR_MIN_SCORE", "0.9")
    get_settings.cache_clear()
    assert get_settings().kb_similar_min_score == 0.9


async def test_same_product_and_version_comes_before_close_meaning() -> None:
    """An entry with the same product and version is listed first, even with a lower score."""
    tenant = uuid.uuid4()
    await _seed(tenant, "vcenter-snapshot-revert", {"product": "vcenter", "version": "8.0"})
    await _seed(tenant, "esxi-snapshot-notes")
    mock, filters = _fake_retrieve(
        tenant,
        close={"esxi-snapshot-notes": 0.97, "vcenter-snapshot-revert": 0.88},
        same={"vcenter-snapshot-revert": 0.88},
    )
    with patch("meho_backplane.kb.similar.retrieve", mock):
        similar = await find_similar_entries(
            tenant,
            "How to revert a snapshot.",
            {"product": "vcenter", "version": "8.0"},
            actor_sub="a",
            may_change_any=False,
        )

    assert [entry.slug for entry in similar] == ["vcenter-snapshot-revert", "esxi-snapshot-notes"]
    assert [entry.match for entry in similar] == ["same_product_version", "close_meaning"]
    assert similar[0].product == "vcenter"
    assert similar[0].version == "8.0"
    assert filters() == [{"product": "vcenter", "version": "8.0"}, None]


async def test_entries_below_the_threshold_are_not_listed() -> None:
    tenant = uuid.uuid4()
    await _seed(tenant, "near-copy")
    await _seed(tenant, "other-topic")
    mock, _ = _fake_retrieve(tenant, close={"near-copy": 0.85, "other-topic": 0.84})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        similar = await find_similar_entries(
            tenant, "text", {}, actor_sub="a", may_change_any=False
        )
    assert [entry.slug for entry in similar] == ["near-copy"]


async def test_at_most_the_limit_and_best_first() -> None:
    tenant = uuid.uuid4()
    scores = {f"entry-{i}": 0.86 + i / 100 for i in range(SIMILAR_LIMIT + 2)}
    for slug in scores:
        await _seed(tenant, slug)
    mock, _ = _fake_retrieve(tenant, close=scores)
    with patch("meho_backplane.kb.similar.retrieve", mock):
        similar = await find_similar_entries(
            tenant, "text", {}, actor_sub="a", may_change_any=False
        )
    assert len(similar) == SIMILAR_LIMIT
    assert [entry.score for entry in similar] == sorted(
        (entry.score for entry in similar), reverse=True
    )


async def test_answer_names_the_current_revision_and_header_fields() -> None:
    tenant = uuid.uuid4()
    metadata: dict[str, object] = {
        "summary": "Revert needs a powered-off VM",
        "status": "confirmed",
    }
    await _seed(tenant, "snapshot-revert", metadata)
    stored = await KbService().get_entry(tenant, "snapshot-revert")
    assert stored is not None
    mock, _ = _fake_retrieve(tenant, close={"snapshot-revert": 0.93})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        [entry] = await find_similar_entries(
            tenant, "text", {}, actor_sub="a", may_change_any=False
        )
    assert entry.entry_id == stored.id
    assert entry.revision_id == stored.revision_id
    assert entry.revision_id is not None
    assert entry.summary == "Revert needs a powered-off VM"
    assert entry.status == "confirmed"
    assert entry.snippet == "Stored text of snapshot-revert."
    assert entry.score == 0.93


async def test_may_change_follows_the_owner_rule() -> None:
    """Without the right to change any entry, the caller may change only its own."""
    tenant = uuid.uuid4()
    await _seed(tenant, "mine", by="me")
    await _seed(tenant, "theirs", by="someone-else")
    mock, _ = _fake_retrieve(tenant, close={"mine": 0.9, "theirs": 0.95})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        own_only = await find_similar_entries(
            tenant, "text", {}, actor_sub="me", may_change_any=False
        )
        any_entry = await find_similar_entries(
            tenant, "text", {}, actor_sub="me", may_change_any=True
        )
    assert {e.slug: e.may_change for e in own_only} == {"mine": True, "theirs": False}
    assert {e.slug: e.may_change for e in any_entry} == {"mine": True, "theirs": True}


async def test_shared_entry_of_another_tenant_is_listed_but_not_changeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, reader = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(owner))
    get_settings.cache_clear()
    await KbService().create_entry(
        owner, "company-rule", "Shared text.", {"shared": True}, actor_sub="me"
    )

    async def _retrieve(**kwargs: Any) -> list[RetrievalHit]:
        if kwargs["tenant_id"] == owner:
            return [_hit("company-rule", 0.95, owner)]
        return []

    mock = AsyncMock(side_effect=_retrieve)
    with (
        patch("meho_backplane.kb.similar.retrieve", mock),
        patch("meho_backplane.kb.service.retrieve", mock),
    ):
        [entry] = await find_similar_entries(
            reader, "text", {}, actor_sub="me", may_change_any=True
        )
    assert entry.slug == "company-rule"
    assert entry.may_change is False


async def test_entry_deleted_after_the_search_is_left_out() -> None:
    tenant = uuid.uuid4()
    mock, _ = _fake_retrieve(tenant, close={"gone": 0.99})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        similar = await find_similar_entries(
            tenant, "text", {}, actor_sub="a", may_change_any=False
        )
    assert similar == []


# ---------------------------------------------------------------------------
# check_new_entry: when to write
# ---------------------------------------------------------------------------


async def test_new_slug_with_similar_entries_is_not_written() -> None:
    tenant = uuid.uuid4()
    await _seed(tenant, "snapshot-revert")
    mock, _ = _fake_retrieve(tenant, close={"snapshot-revert": 0.92})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        check = await check_new_entry(
            tenant, "snapshot-revert-again", "text", None, actor_sub="a", may_change_any=True
        )
    assert check is not None
    assert check.written is False
    assert check.reason == "similar_entries"
    assert check.would_write is False
    assert [entry.slug for entry in check.similar] == ["snapshot-revert"]
    assert "create_anyway" in check.next_step


async def test_new_slug_with_nothing_similar_goes_on() -> None:
    tenant = uuid.uuid4()
    mock, _ = _fake_retrieve(tenant, close={})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        check = await check_new_entry(
            tenant, "fresh-topic", "text", None, actor_sub="a", may_change_any=True
        )
    assert check is None


async def test_create_anyway_skips_the_search() -> None:
    tenant = uuid.uuid4()
    mock, _ = _fake_retrieve(tenant, close={"x": 0.99})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        check = await check_new_entry(
            tenant,
            "fresh-topic",
            "text",
            None,
            actor_sub="a",
            may_change_any=True,
            create_anyway=True,
        )
    assert check is None
    mock.assert_not_awaited()


async def test_update_of_an_existing_slug_is_not_checked() -> None:
    tenant = uuid.uuid4()
    await _seed(tenant, "snapshot-revert")
    mock, _ = _fake_retrieve(tenant, close={"snapshot-revert": 0.99})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        assert (
            await check_new_entry(
                tenant, "snapshot-revert", "new text", None, actor_sub="a", may_change_any=True
            )
            is None
        )
        dry = await check_new_entry(
            tenant,
            "snapshot-revert",
            "new text",
            None,
            actor_sub="a",
            may_change_any=True,
            dry_run=True,
        )
    mock.assert_not_awaited()
    assert dry is not None
    assert dry.slug_exists is True
    assert dry.would_write is True
    assert dry.similar == []


async def test_dry_run_gives_the_same_list_and_says_whether_it_would_write() -> None:
    tenant = uuid.uuid4()
    await _seed(tenant, "snapshot-revert")
    mock, _ = _fake_retrieve(tenant, close={"snapshot-revert": 0.92})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        refused = await check_new_entry(
            tenant, "new-slug", "text", None, actor_sub="a", may_change_any=True
        )
        dry = await check_new_entry(
            tenant, "new-slug", "text", None, actor_sub="a", may_change_any=True, dry_run=True
        )
        dry_anyway = await check_new_entry(
            tenant,
            "new-slug",
            "text",
            None,
            actor_sub="a",
            may_change_any=True,
            dry_run=True,
            create_anyway=True,
        )
    assert refused is not None
    assert dry is not None
    assert dry_anyway is not None
    assert dry.reason == "dry_run"
    assert dry.would_write is False
    assert dry.similar == refused.similar
    assert dry_anyway.would_write is True
    assert dry_anyway.similar == refused.similar
    assert await KbService().get_entry(tenant, "new-slug") is None


async def test_dry_run_with_nothing_similar_says_the_create_would_go_through() -> None:
    tenant = uuid.uuid4()
    mock, _ = _fake_retrieve(tenant, close={})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        dry = await check_new_entry(
            tenant, "fresh-topic", "text", None, actor_sub="a", may_change_any=True, dry_run=True
        )
    assert dry is not None
    assert dry.would_write is True
    assert dry.similar == []
    assert "would go through" in dry.next_step
    assert await KbService().get_entry(tenant, "fresh-topic") is None


async def test_bad_slug_fails_before_the_search() -> None:
    mock = AsyncMock()
    with (
        patch("meho_backplane.kb.similar.retrieve", mock),
        pytest.raises(InvalidKbSlugError),
    ):
        await check_new_entry(
            uuid.uuid4(), "1-bad", "text", None, actor_sub="a", may_change_any=True
        )
    mock.assert_not_awaited()


async def test_header_version_number_is_searched_as_text() -> None:
    """A version sent as a number is matched as the text the store keeps."""
    tenant = uuid.uuid4()
    mock, filters = _fake_retrieve(tenant, close={}, same={})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        await check_new_entry(
            tenant,
            "fresh-topic",
            "text",
            {"product": "vcenter", "version": 9.0},
            actor_sub="a",
            may_change_any=True,
        )
    assert filters()[0] == {"product": "vcenter", "version": "9.0"}


# ---------------------------------------------------------------------------
# MCP add_to_knowledge
# ---------------------------------------------------------------------------


def _add(client: TestClient, arguments: dict[str, object]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add_to_knowledge", "arguments": arguments},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False, body
    result: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return result


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_mcp_create_next_to_a_similar_entry_is_not_written(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, human = client_with_operator
    agent = human.model_copy(update={"principal_kind": PrincipalKind.AGENT})

    async def _verify_agent() -> Operator:
        return agent

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify_agent
    tenant = agent.tenant_id
    await _seed(tenant, "snapshot-revert", {"summary": "Power off first"}, by="someone-else")
    await _seed(tenant, "my-snapshot-note", by=agent.sub)
    mock, _ = _fake_retrieve(tenant, close={"snapshot-revert": 0.95, "my-snapshot-note": 0.9})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        refused = _add(client, {"slug": "snapshot-revert-2", "body": "Power the VM off."})
        dry = _add(
            client, {"slug": "snapshot-revert-2", "body": "Power the VM off.", "dry_run": True}
        )

    assert refused["written"] is False
    assert refused["reason"] == "similar_entries"
    by_slug = {entry["slug"]: entry for entry in refused["similar"]}
    assert by_slug["snapshot-revert"]["may_change"] is False
    assert by_slug["my-snapshot-note"]["may_change"] is True
    assert by_slug["snapshot-revert"]["revision_id"]
    # Stored text is marked as data, not instructions.
    assert by_slug["snapshot-revert"]["summary"].startswith(BLOCK_START)
    assert by_slug["snapshot-revert"]["snippet"].startswith(BLOCK_START)
    assert dry["reason"] == "dry_run"
    assert dry["similar"] == refused["similar"]
    assert await KbService().get_entry(tenant, "snapshot-revert-2") is None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_mcp_create_anyway_writes_the_entry(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, op = client_with_operator
    await _seed(op.tenant_id, "snapshot-revert")
    mock, _ = _fake_retrieve(op.tenant_id, close={"snapshot-revert": 0.95})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        created = _add(
            client,
            {"slug": "snapshot-revert-2", "body": "Power the VM off.", "create_anyway": True},
        )
    assert created["slug"] == "snapshot-revert-2"
    assert "written" not in created
    assert await KbService().get_entry(op.tenant_id, "snapshot-revert-2") is not None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_mcp_create_with_nothing_similar_works_as_before(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, op = client_with_operator
    mock, _ = _fake_retrieve(op.tenant_id, close={})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        created = _add(client, {"slug": "fresh-topic", "body": "New knowledge."})
    assert created["slug"] == "fresh-topic"
    assert created["body"] == "New knowledge."


def test_mcp_answer_with_long_texts_stays_small() -> None:
    """The answer with the most entries and the longest texts stays far inside the size limit."""
    tenant = uuid.uuid4()
    long_text = "x" * 50_000
    from meho_backplane.kb.similar import KbSimilarCheck, KbSimilarEntry, _short

    entries = [
        KbSimilarEntry(
            slug=f"entry-{i}",
            entry_id=uuid.uuid4(),
            revision_id=uuid.uuid4(),
            summary=_short(long_text),
            product=_short(long_text),
            version=_short(long_text),
            status=_short(long_text),
            snippet=_short(long_text) or "",
            score=0.9,
            match="close_meaning",
            may_change=True,
        )
        for i in range(SIMILAR_LIMIT)
    ]
    check = KbSimilarCheck(
        slug=f"new-{tenant.hex[:6]}",
        reason="similar_entries",
        would_write=False,
        slug_exists=False,
        similar=entries,
        min_score=0.85,
        next_step="n",
    )
    size = measure_delivered_wire_bytes(_similar_check_payload(check), structured=False)
    assert size < 32 * 1024
    assert size < get_settings().result_complete_response_wire_max_bytes


# ---------------------------------------------------------------------------
# REST POST /api/v1/kb
# ---------------------------------------------------------------------------


@pytest.fixture
def rest_client() -> Iterator[tuple[TestClient, Operator]]:
    operator = build_operator(TenantRole.TENANT_ADMIN)
    rest_app = FastAPI()
    rest_app.include_router(kb_api.router)

    async def _admin() -> Operator:
        return operator

    rest_app.dependency_overrides[kb_api._require_admin.dependency] = _admin  # type: ignore[index]
    with TestClient(rest_app) as client:
        yield client, operator


async def test_rest_create_next_to_a_similar_entry_is_409(
    rest_client: tuple[TestClient, Operator],
) -> None:
    client, op = rest_client
    await _seed(op.tenant_id, "snapshot-revert", by="someone-else")
    mock, _ = _fake_retrieve(op.tenant_id, close={"snapshot-revert": 0.95})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        refused = client.post("/api/v1/kb", json={"slug": "snapshot-revert-2", "body": "b"})
        dry = client.post(
            "/api/v1/kb", json={"slug": "snapshot-revert-2", "body": "b", "dry_run": True}
        )
        created = client.post(
            "/api/v1/kb",
            json={"slug": "snapshot-revert-2", "body": "b", "create_anyway": True},
        )

    assert refused.status_code == 409
    answer = refused.json()
    assert answer["written"] is False
    assert [entry["slug"] for entry in answer["similar"]] == ["snapshot-revert"]
    # A tenant admin may change any entry of its tenant.
    assert answer["similar"][0]["may_change"] is True
    assert dry.status_code == 200
    assert dry.json()["similar"] == answer["similar"]
    assert dry.json()["would_write"] is False
    assert created.status_code == 201
    assert created.json()["slug"] == "snapshot-revert-2"


async def test_rest_create_with_nothing_similar_is_201(
    rest_client: tuple[TestClient, Operator],
) -> None:
    client, op = rest_client
    mock, _ = _fake_retrieve(op.tenant_id, close={})
    with patch("meho_backplane.kb.similar.retrieve", mock):
        dry = client.post("/api/v1/kb", json={"slug": "fresh-topic", "body": "b", "dry_run": True})
        created = client.post("/api/v1/kb", json={"slug": "fresh-topic", "body": "b"})
    assert dry.status_code == 200
    assert dry.json()["would_write"] is True
    assert created.status_code == 201


async def test_directory_ingest_does_not_run_the_check(tmp_path: Path) -> None:
    """Directory ingest writes as before, even when the search would find a match."""
    (tmp_path / "snapshot-revert.md").write_text("Power the VM off first.", encoding="utf-8")
    tenant = uuid.uuid4()
    mock = AsyncMock(side_effect=AssertionError("ingest must not search"))
    with patch("meho_backplane.kb.similar.retrieve", mock):
        result = await KbService().ingest_directory(tmp_path, tenant)
    assert result.inserted_count == 1
    mock.assert_not_awaited()
