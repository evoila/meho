# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The page import of ``POST /api/v1/kb/import`` (#5728), against the test database.

Covers the rules of :mod:`meho_backplane.kb.page_import`: update by slug
and safe to run twice, the dry run, the conflict report, the
``source: meho://kb/<slug>`` link back to an entry, empty header values,
and every refusal (no header, candidate, customer fact in a shared
tenant, secret, bad path, bad slug, a shared slug of another tenant, a
failed write).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.kb.page_import import (
    KbImportPage,
    KbImportRequest,
    KbImportResult,
    import_pages,
)
from meho_backplane.kb.service import KbService
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

SHARED_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
IMPORTER = "service-account-kb-import"

CONFIRMED = "type: lesson\napplies_to: product-and-version\nstatus: confirmed\n"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(SHARED_TENANT))
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


def _page(path: str, header: str = CONFIRMED, body: str = "Some text.") -> KbImportPage:
    return KbImportPage(path=path, content=f"---\n{header}---\n{body}\n")


async def _run(
    *pages: KbImportPage,
    tenant: uuid.UUID = TENANT,
    dry_run: bool = False,
    include_candidates: bool = False,
) -> KbImportResult:
    request = KbImportRequest(
        pages=list(pages), dry_run=dry_run, include_candidates=include_candidates
    )
    return await import_pages(request, tenant_id=tenant, actor_sub=IMPORTER)


def _rows(result: KbImportResult) -> dict[str, tuple[str, str | None, str | None]]:
    return {row.path: (row.action, row.slug, row.reason) for row in result.pages}


# --- write, update, safe to run twice -----------------------------------------


@pytest.mark.asyncio
async def test_a_page_is_created_with_import_provenance() -> None:
    result = await _run(_page("products/vcenter/snapshot-limits.md"))
    assert _rows(result) == {
        "products/vcenter/snapshot-limits.md": ("create", "snapshot-limits", None)
    }
    assert result.created_count == 1
    entry = await KbService().get_entry(TENANT, "snapshot-limits")
    assert entry is not None
    assert entry.body.strip() == "Some text."
    assert entry.metadata["status"] == "confirmed"
    assert entry.provenance.source_kind == "import"
    assert entry.provenance.relative_path == "products/vcenter/snapshot-limits.md"
    assert entry.metadata["created_by_sub"] == IMPORTER


@pytest.mark.asyncio
async def test_a_second_run_of_the_same_pages_changes_nothing() -> None:
    pages = (_page("a.md"), _page("b.md", body="Other text."))
    await _run(*pages)
    first = await KbService().get_entry(TENANT, "a")
    result = await _run(*pages)
    second = await KbService().get_entry(TENANT, "a")
    assert [row.action for row in result.pages] == ["unchanged", "unchanged"]
    assert result.unchanged_count == 2
    assert first is not None and second is not None
    assert second.revision_id == first.revision_id
    assert second.updated_at == first.updated_at


@pytest.mark.asyncio
async def test_a_changed_header_or_body_is_an_update() -> None:
    await _run(_page("a.md"), _page("b.md"))
    result = await _run(
        _page("a.md", header=CONFIRMED + "summary: New summary.\n"),
        _page("b.md", body="New text."),
    )
    assert [row.action for row in result.pages] == ["update", "update"]
    a = await KbService().get_entry(TENANT, "a")
    b = await KbService().get_entry(TENANT, "b")
    assert a is not None and a.metadata["summary"] == "New summary."
    assert b is not None and b.body.strip() == "New text."


@pytest.mark.asyncio
async def test_an_update_keeps_the_owner_of_the_entry() -> None:
    await KbService().create_entry(
        TENANT, "a", "Old text.", {"status": "confirmed"}, actor_sub="person-1"
    )
    result = await _run(_page("a.md"))
    assert result.pages[0].action == "update"
    entry = await KbService().get_entry(TENANT, "a")
    assert entry is not None
    assert entry.metadata["created_by_sub"] == "person-1"
    assert entry.metadata["last_updated_by_sub"] == IMPORTER


@pytest.mark.asyncio
async def test_the_import_writes_only_into_the_callers_tenant() -> None:
    await KbService().create_entry(uuid.uuid4(), "a", "Other tenant.", None)
    result = await _run(_page("a.md"))
    assert result.pages[0].action == "create"


# --- the dry run -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dry_run_lists_every_page_and_writes_nothing() -> None:
    await _run(_page("same.md"), _page("changed.md"))
    result = await _run(
        _page("same.md"),
        _page("changed.md", body="New text."),
        _page("new.md"),
        KbImportPage(path="no-header.md", content="Just text.\n"),
        dry_run=True,
    )
    rows = _rows(result)
    assert rows["same.md"] == ("unchanged", "same", None)
    assert rows["changed.md"] == ("update", "changed", None)
    assert rows["new.md"] == ("create", "new", None)
    assert rows["no-header.md"][0] == "skip"
    assert "no lesson header" in (rows["no-header.md"][2] or "")
    assert result.dry_run is True
    assert (result.created_count, result.updated_count) == (1, 1)
    assert (result.unchanged_count, result.skipped_count) == (1, 1)
    assert await KbService().get_entry(TENANT, "new") is None
    changed = await KbService().get_entry(TENANT, "changed")
    assert changed is not None and changed.body.strip() == "Some text."


# --- conflicts and the link back to an entry ---------------------------------


@pytest.mark.asyncio
async def test_pages_with_one_slug_are_a_conflict_and_none_is_written() -> None:
    await KbService().create_entry(TENANT, "limits", "Stored text.", {"status": "confirmed"})
    result = await _run(
        _page("vcenter/limits.md", body="One."),
        _page("nsx/limits.md", body="Two."),
        _page("other.md", header=CONFIRMED + "source: meho://kb/limits\n"),
    )
    assert [row.action for row in result.pages] == ["skip", "skip", "skip"]
    assert all("conflict" in (row.reason or "") for row in result.pages)
    assert len(result.conflicts) == 1
    assert result.conflicts[0].slug == "limits"
    assert result.conflicts[0].relative_paths == ["nsx/limits.md", "other.md", "vcenter/limits.md"]
    stored = await KbService().get_entry(TENANT, "limits")
    assert stored is not None and stored.body == "Stored text."


@pytest.mark.asyncio
async def test_a_source_that_names_an_entry_updates_that_entry() -> None:
    await KbService().create_entry(
        TENANT, "vm-clone-lesson", "Old text.", {"status": "confirmed"}, actor_sub="person-1"
    )
    page = _page(
        "products/vcenter/clones.md",
        header=CONFIRMED + "source: meho://kb/vm-clone-lesson\n",
        body="Text from the wiki.",
    )
    result = await _run(page)
    assert _rows(result) == {"products/vcenter/clones.md": ("update", "vm-clone-lesson", None)}
    assert await KbService().get_entry(TENANT, "clones") is None
    entry = await KbService().get_entry(TENANT, "vm-clone-lesson")
    assert entry is not None
    assert entry.body.strip() == "Text from the wiki."
    assert entry.metadata["source"] == "meho://kb/vm-clone-lesson"


@pytest.mark.asyncio
async def test_a_source_that_is_not_an_entry_keeps_the_file_name() -> None:
    page = _page("limits.md", header=CONFIRMED + "source: https://docs.example.com/limits\n")
    result = await _run(page)
    assert result.pages[0].slug == "limits"


# --- the header ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_header_values_are_dropped() -> None:
    header = CONFIRMED + 'source:\nsummary: ""\ntags: []\nauthor: "  "\nslug:\nlearned_at:\n'
    result = await _run(_page("empty-values.md", header=header))
    assert _rows(result) == {"empty-values.md": ("create", "empty-values", None)}
    entry = await KbService().get_entry(TENANT, "empty-values")
    assert entry is not None
    for name in ("source", "summary", "tags", "author", "slug", "learned_at"):
        assert name not in entry.metadata


@pytest.mark.asyncio
async def test_an_unquoted_date_is_kept() -> None:
    await _run(_page("dated.md", header=CONFIRMED + "confirmed_at: 2026-11-03\n"))
    entry = await KbService().get_entry(TENANT, "dated")
    assert entry is not None
    assert entry.metadata["confirmed_at"] == "2026-11-03T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("Just text, no header.\n", "missing: type, applies_to, status"),
        ("---\ntype: lesson\napplies_to: team\n---\nText.\n", "missing: status"),
        ("---\ntype: lesson\napplies_to: team\nstatus:\n---\nText.\n", "missing: status"),
        ("---\n" + CONFIRMED.replace("lesson", "gossip") + "---\nText.\n", "type must be one of"),
        ("---\n" + CONFIRMED + "slug: renamed\n---\nText.\n", "kept by MEHO"),
        ("---\n" + CONFIRMED + "shared: maybe\n---\nText.\n", "shared must be true or false"),
        ("---\n" + CONFIRMED + "a: [\n---\nText.\n", "front-matter parse error"),
        ("---\n" + CONFIRMED + "---\n   \n", "no text below the header"),
    ],
)
async def test_a_page_without_a_good_header_is_skipped(content: str, reason: str) -> None:
    result = await _run(KbImportPage(path="page.md", content=content))
    row = result.pages[0]
    assert row.action == "skip"
    assert reason in (row.reason or "")
    assert await KbService().get_entry(TENANT, "page") is None


@pytest.mark.asyncio
async def test_a_candidate_needs_include_candidates() -> None:
    page = _page("cand.md", header=CONFIRMED.replace("confirmed", "candidate"))
    refused = await _run(page)
    assert refused.pages[0].action == "skip"
    assert "include_candidates" in (refused.pages[0].reason or "")
    assert await KbService().get_entry(TENANT, "cand") is None
    accepted = await _run(page, include_candidates=True)
    assert accepted.pages[0].action == "create"
    entry = await KbService().get_entry(TENANT, "cand")
    assert entry is not None and "expires_at" in entry.metadata


@pytest.mark.asyncio
async def test_a_customer_fact_is_refused_in_a_shared_tenant() -> None:
    customer = CONFIRMED.replace("product-and-version", "customer")
    result = await _run(_page("customer-fact.md", header=customer), tenant=SHARED_TENANT)
    assert result.pages[0].action == "skip"
    assert "project folder" in (result.pages[0].reason or "")
    shared_page = await _run(
        _page("customer-shared.md", header=customer + "shared: true\n"), tenant=SHARED_TENANT
    )
    assert shared_page.pages[0].action == "skip"


@pytest.mark.asyncio
async def test_a_customer_fact_is_accepted_in_a_tenant_that_is_not_shared() -> None:
    customer = CONFIRMED.replace("product-and-version", "customer")
    result = await _run(_page("customer-fact.md", header=customer))
    assert result.pages[0].action == "create"


# --- the secret check, paths and slugs ---------------------------------------


@pytest.mark.asyncio
async def test_a_page_with_a_secret_is_skipped_and_the_reason_hides_it() -> None:
    made_up = "planted-secret-Xy7pQ2wLm9"
    pages = (
        _page("in-body.md", body=f"Log in with\npassword={made_up}"),
        _page("in-header.md", header=CONFIRMED + f"summary: token={made_up}\n"),
    )
    result = await _run(*pages)
    assert [row.action for row in result.pages] == ["skip", "skip"]
    for row in result.pages:
        assert "looks like a secret (api_key on line" in (row.reason or "")
        assert made_up not in result.model_dump_json()
    assert await KbService().get_entry(TENANT, "in-body") is None


@pytest.mark.asyncio
async def test_a_placeholder_is_not_a_secret() -> None:
    result = await _run(_page("how-to.md", body="Set password: <password> and Bearer ${TOKEN}."))
    assert result.pages[0].action == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["../escape.md", "/abs/page.md", "notes.txt", "a\\b.md", "dir/../x.md"],
)
async def test_a_bad_path_is_skipped(path: str) -> None:
    result = await _run(_page(path))
    assert result.pages[0].action == "skip"
    assert "relative path to a .md file" in (result.pages[0].reason or "")


@pytest.mark.asyncio
async def test_a_file_name_that_is_not_a_slug_is_skipped() -> None:
    result = await _run(_page("Upper Case.md"), _page("good.md"))
    rows = _rows(result)
    assert rows["Upper Case.md"][0] == "skip"
    assert rows["good.md"][0] == "create"


@pytest.mark.asyncio
async def test_a_shared_slug_of_another_tenant_is_skipped() -> None:
    await KbService().create_entry(SHARED_TENANT, "team-rule", "Shared.", {"shared": True})
    result = await _run(_page("team-rule.md"))
    assert result.pages[0].action == "skip"
    assert "team-rule" in (result.pages[0].reason or "")


@pytest.mark.asyncio
async def test_a_failed_write_skips_that_page_and_keeps_the_others() -> None:
    from meho_backplane.kb import page_import

    real_write = page_import.write_entry

    async def flaky_write(**kwargs: Any) -> Any:
        if kwargs["slug"] == "broken":
            raise RuntimeError("database gone")
        return await real_write(**kwargs)

    with patch("meho_backplane.kb.page_import.write_entry", side_effect=flaky_write):
        result = await _run(_page("broken.md"), _page("fine.md"))
    rows = _rows(result)
    assert rows["broken.md"][0] == "skip"
    assert "the write failed (RuntimeError)" in (rows["broken.md"][2] or "")
    assert "database gone" not in (rows["broken.md"][2] or "")
    assert rows["fine.md"][0] == "create"
    assert await KbService().get_entry(TENANT, "fine") is not None
