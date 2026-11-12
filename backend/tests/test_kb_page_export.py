# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The page export of ``POST /api/v1/kb/export`` (#5730), against the test database.

Covers the rules of :mod:`meho_backplane.kb.page_export`: only confirmed
or candidate generic entries that an operator wrote give a page (a
candidate keeps ``status: candidate``); retired and replaced
entries give a page to remove; imported entries are left out; the page
has the lesson header and a stable file name; an entry without the
complete header is skipped; the secret check; the caller's tenant only;
and that the import takes the exported page back to the same entry and
keeps it shared.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.file_walker import parse_front_matter
from meho_backplane.kb.page_export import KbExportResult, export_pages, render_page
from meho_backplane.kb.page_import import KbImportPage, KbImportRequest, import_pages
from meho_backplane.kb.provenance import stamp_provenance
from meho_backplane.kb.schemas import KB_SOURCE
from meho_backplane.kb.service import KbService
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
GENERIC = {"type": "lesson", "applies_to": "product-and-version", "status": "confirmed"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def stub_embedding() -> Iterator[AsyncMock]:
    fake_service = fake_embedding_service()
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_service,
    ):
        yield fake_service.encode


async def _add(
    slug: str, body: str = "Some text.", tenant: uuid.UUID = TENANT, **header: Any
) -> None:
    await KbService().create_entry(tenant, slug, body, {**GENERIC, **header}, actor_sub="person-1")


async def _set_provenance(slug: str, provenance: dict[str, Any] | None) -> None:
    """Replace the stored provenance block of *slug* (``None`` drops it)."""
    async with get_sessionmaker()() as session, session.begin():
        doc = (
            await session.execute(
                Document.__table__.select().where(
                    Document.tenant_id == TENANT,
                    Document.source == KB_SOURCE,
                    Document.source_id == slug,
                )
            )
        ).one()
        metadata = {k: v for k, v in dict(doc.metadata).items() if k != "provenance"}
        if provenance is not None:
            metadata["provenance"] = provenance
        await session.execute(
            update(Document).where(Document.id == doc.id).values(doc_metadata=metadata)
        )


def _pages(result: KbExportResult) -> dict[str, str]:
    return {page.path: page.content for page in result.pages}


# --- what is exported --------------------------------------------------------


@pytest.mark.asyncio
async def test_only_confirmed_or_candidate_generic_entries_give_a_page() -> None:
    await _add("snapshot-limits", product="vcenter", version="8.0")
    await _add("a-candidate", status="candidate")
    await _add("a-dispute", status="disputed")
    await _add("a-customer", applies_to="customer")
    await _add("a-target", applies_to="target")
    await KbService().create_entry(TENANT, "no-header", "Text.", None)
    result = await export_pages(TENANT)
    assert [page.path for page in result.pages] == ["a-candidate.md", "snapshot-limits.md"]
    assert result.pages[1].slug == "snapshot-limits"
    assert (result.page_count, result.remove_count, result.skipped_count) == (2, 0, 0)


@pytest.mark.asyncio
async def test_a_candidate_keeps_status_candidate_for_review() -> None:
    """A lesson that is not confirmed yet reaches the wiki as a candidate (#5976)."""
    await _add("new-lesson", status="candidate")
    result = await export_pages(TENANT)
    header, _body = parse_front_matter(result.pages[0].content, "new-lesson.md")
    assert header["status"] == "candidate"
    assert "expires_at" in header


@pytest.mark.asyncio
async def test_an_imported_candidate_is_not_exported() -> None:
    await _add("from-wiki", status="candidate")
    await _set_provenance(
        "from-wiki",
        stamp_provenance(
            {},
            source_kind="import",
            relative_path="products/x/from-wiki.md",
            ingested_at=datetime(2026, 11, 8, tzinfo=UTC),
        )["provenance"],
    )
    result = await export_pages(TENANT)
    assert (result.page_count, result.remove_count, result.skipped_count) == (0, 0, 0)


@pytest.mark.asyncio
async def test_the_page_has_the_lesson_header_and_the_text() -> None:
    await _add(
        "snapshot-limits",
        body="Keep at most 32 snapshots.\n",
        product="vcenter",
        version="8.0",
        summary="Snapshot limit: 32",
        tags=["snapshots", "limits"],
        confirmed_at="2026-11-03",
    )
    result = await export_pages(TENANT)
    header, body = parse_front_matter(result.pages[0].content, "snapshot-limits.md")
    assert list(header) == [
        "type",
        "applies_to",
        "status",
        "summary",
        "product",
        "version",
        "source",
        "confirmed_at",
        "tags",
    ]
    assert header["version"] == "8.0"
    assert header["summary"] == "Snapshot limit: 32"
    assert header["source"] == "meho://kb/snapshot-limits"
    assert header["confirmed_at"] == "2026-11-03T00:00:00+00:00"
    assert header["tags"] == ["snapshots", "limits"]
    assert body == "Keep at most 32 snapshots."


@pytest.mark.asyncio
async def test_meho_keys_stay_in_meho() -> None:
    await _add("a", owner_note="x")
    content = (await export_pages(TENANT)).pages[0].content
    for key in ("provenance", "created_by_sub", "last_updated_by_sub", "person-1", "owner_note"):
        assert key not in content


@pytest.mark.asyncio
async def test_a_source_of_the_entry_is_kept() -> None:
    await _add("a", source="https://docs.example.com/limits")
    header, _ = parse_front_matter((await export_pages(TENANT)).pages[0].content, "a.md")
    assert header["source"] == "https://docs.example.com/limits"


@pytest.mark.asyncio
async def test_a_second_export_gives_the_same_bytes() -> None:
    await _add("b", body="B text.", version="9.0")
    await _add("a", body="A text.")
    first = await export_pages(TENANT)
    second = await export_pages(TENANT)
    assert [page.slug for page in first.pages] == ["a", "b"]
    assert first == second


@pytest.mark.asyncio
async def test_an_empty_tenant_gives_an_empty_export() -> None:
    result = await export_pages(TENANT)
    assert result == KbExportResult(
        page_count=0, remove_count=0, skipped_count=0, pages=[], remove=[], skipped=[]
    )


@pytest.mark.asyncio
async def test_a_confirmed_entry_without_the_complete_header_is_skipped() -> None:
    no_type = {"applies_to": "product-and-version", "status": "confirmed"}
    await KbService().create_entry(TENANT, "no-type", "Text.", no_type, actor_sub="person-1")
    await KbService().create_entry(
        TENANT, "no-type-retired", "Text.", {**no_type, "status": "retired"}, actor_sub="person-1"
    )
    await _add("complete")
    result = await export_pages(TENANT)
    assert [page.slug for page in result.pages] == ["complete"]
    assert [(row.slug, row.status) for row in result.remove] == [("no-type-retired", "retired")]
    assert [row.slug for row in result.skipped] == ["no-type"]
    assert "no complete lesson header (missing: type)" in result.skipped[0].reason


# --- pages to remove ---------------------------------------------------------


@pytest.mark.asyncio
async def test_retired_and_replaced_entries_are_pages_to_remove() -> None:
    await _add("old", status="retired")
    await _add("older", status="replaced")
    await _add("old-customer-fact", status="retired", applies_to="customer")
    result = await export_pages(TENANT)
    assert result.pages == []
    assert [(row.path, row.slug, row.status) for row in result.remove] == [
        ("old.md", "old", "retired"),
        ("older.md", "older", "replaced"),
    ]
    assert result.remove_count == 2


# --- where the entry came from -----------------------------------------------


@pytest.mark.asyncio
async def test_imported_entries_are_left_out() -> None:
    content = "---\ntype: fact\napplies_to: product-and-version\nstatus: confirmed\n---\nText.\n"
    retired = content.replace("confirmed", "retired")
    await import_pages(
        KbImportRequest(
            pages=[
                KbImportPage(path="products/x/from-wiki.md", content=content),
                KbImportPage(path="products/x/gone-in-wiki.md", content=retired),
            ]
        ),
        tenant_id=TENANT,
        actor_sub="service-account-kb-import",
    )
    await _add("from-meho")
    result = await export_pages(TENANT)
    assert [page.slug for page in result.pages] == ["from-meho"]
    assert result.remove == []
    assert result.skipped == []


@pytest.mark.asyncio
async def test_directory_ingested_entries_are_left_out() -> None:
    await _add("ingested")
    stamped = stamp_provenance(
        {}, source_kind="directory", relative_path="kb/ingested.md", ingested_at=datetime.now(UTC)
    )
    await _set_provenance("ingested", stamped["provenance"])  # type: ignore[arg-type]
    result = await export_pages(TENANT)
    assert (result.pages, result.remove, result.skipped) == ([], [], [])


@pytest.mark.asyncio
async def test_an_entry_with_no_provenance_is_skipped_with_the_reason() -> None:
    await _add("old-lesson")
    await _set_provenance("old-lesson", None)
    result = await export_pages(TENANT)
    assert result.pages == []
    assert [row.slug for row in result.skipped] == ["old-lesson"]
    assert "no record of where this entry came from" in result.skipped[0].reason


@pytest.mark.asyncio
async def test_a_broken_provenance_block_counts_as_no_provenance() -> None:
    await _add("odd")
    await _set_provenance("odd", {"source_kind": "somewhere"})
    result = await export_pages(TENANT)
    assert [row.slug for row in result.skipped] == ["odd"]


# --- secret check and tenant -------------------------------------------------


@pytest.mark.asyncio
async def test_an_entry_with_a_secret_is_skipped_and_the_value_is_not_shown() -> None:
    made_up = "Zq8made-up-Pw41"
    await _add("with-secret", body=f"Log in with\npassword: {made_up}\n")
    await _add("fine", body='Set password: "<password>" in the file.')
    result = await export_pages(TENANT)
    assert [page.slug for page in result.pages] == ["fine"]
    assert [row.slug for row in result.skipped] == ["with-secret"]
    assert "api_key on line" in result.skipped[0].reason
    assert made_up not in result.model_dump_json()


@pytest.mark.asyncio
async def test_a_secret_in_the_header_is_found_too() -> None:
    await _add("with-secret", summary="token=Zq8madeupvalue41xyz")
    result = await export_pages(TENANT)
    assert result.pages == []
    assert [row.slug for row in result.skipped] == ["with-secret"]


@pytest.mark.asyncio
async def test_only_the_callers_tenant_is_exported() -> None:
    other = uuid.uuid4()
    await _add("theirs", tenant=other)
    await _add("theirs-retired", tenant=other, status="retired")
    await _add("mine")
    result = await export_pages(TENANT)
    assert [page.slug for page in result.pages] == ["mine"]
    assert result.remove == []


# --- the way back in ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_import_takes_an_exported_page_back_to_the_same_entry() -> None:
    await _add("snapshot-limits", product="vcenter", version="8.0")
    page = (await export_pages(TENANT)).pages[0]
    moved = KbImportPage(path="products/vcenter/8.0/limits-of-snapshots.md", content=page.content)
    result = await import_pages(
        KbImportRequest(pages=[moved], dry_run=True),
        tenant_id=TENANT,
        actor_sub="service-account-kb-import",
    )
    assert [(row.slug, row.action) for row in result.pages] == [("snapshot-limits", "update")]


@pytest.mark.asyncio
async def test_the_round_trip_keeps_a_shared_entry_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(TENANT))
    get_settings.cache_clear()
    other = uuid.uuid4()
    await _add("team-rule", body="Shared with every tenant.", shared=True)
    page = (await export_pages(TENANT)).pages[0]
    assert "shared" not in page.content
    result = await import_pages(
        KbImportRequest(pages=[KbImportPage(path="rules/team-rule.md", content=page.content)]),
        tenant_id=TENANT,
        actor_sub="service-account-kb-import",
    )
    assert [(row.slug, row.action) for row in result.pages] == [("team-rule", "update")]
    entry = await KbService().get_entry(TENANT, "team-rule")
    assert entry is not None
    assert entry.metadata["shared"] is True
    assert entry.metadata["source"] == "meho://kb/team-rule"
    seen_by_other = await KbService().get_entry(other, "team-rule")
    assert seen_by_other is not None
    assert seen_by_other.body.strip() == "Shared with every tenant."


def test_render_page_leaves_out_empty_values() -> None:
    content = render_page("a", "Text.\n\n", {**GENERIC, "summary": "", "tags": [], "author": None})
    assert content == (
        "---\ntype: lesson\napplies_to: product-and-version\nstatus: confirmed\n"
        "source: meho://kb/a\n---\nText.\n"
    )
