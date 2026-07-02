# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the citation source_url resolver (#1919).

Covers the declarative source-kind mapping in
:mod:`meho_backplane.docs_search.citation_links`: a Broadcom KB ``gs://``
object path resolves to a clickable ``knowledge.broadcom.com`` URL, a
community ``gs://`` path degrades to a non-clickable label, an unrecognised
``gs://`` path never returns a broken ``gs://`` href, an already-canonical
``http(s)`` source passes through, and the human label is chosen
title-first. The same resolver backs the REST/MCP ``ask_docs`` payload and
the ``/ui/corpus`` render (exercised in ``test_mcp_tools_docs_ask`` and
``test_ui_corpus``).
"""

from __future__ import annotations

import pytest

from meho_backplane.docs_search import (
    citation_link_payload,
    normalize_source_ref,
    resolve_citation_link,
)

# Concrete object paths from the issue body (#1919), so the test pins the
# exact shapes a consumer reported.
_KB_GS = "gs://meho-knowledge-vmware-corpus/kb/broadcom-kb/articles/41/414551.html"
_COMMUNITY_GS = (
    "gs://meho-knowledge-vmware-corpus/community/williamlam/blog/2023/05/quiesce-snapshots.md"
)


# ---------------------------------------------------------------------------
# Broadcom KB -> canonical knowledge.broadcom.com link
# ---------------------------------------------------------------------------


def test_broadcom_kb_resolves_to_canonical_article_url() -> None:
    """A KB ``gs://`` object path resolves to a clickable portal article URL."""
    link = resolve_citation_link(_KB_GS, title="vCenter scaling maximums")

    assert link.kind == "broadcom_kb"
    assert link.clickable is True
    assert link.href == "https://knowledge.broadcom.com/external/article/414551"
    # gs:// is never the href.
    assert link.href is not None
    assert not link.href.startswith("gs://")
    # The human title is the link text.
    assert link.label == "vCenter scaling maximums"


def test_broadcom_kb_with_non_numeric_stem_degrades_not_clickable() -> None:
    """A KB path whose filename is not a numeric id never mints a 404 URL."""
    link = resolve_citation_link(
        "gs://bucket/kb/broadcom-kb/articles/index.html",
        document_id="kb-index",
    )

    assert link.kind == "broadcom_kb"
    assert link.clickable is False
    assert link.href is None
    assert link.label == "kb-index"


# ---------------------------------------------------------------------------
# Community mirror -> title + non-clickable path (no recoverable original URL)
# ---------------------------------------------------------------------------


def test_community_gs_degrades_to_non_clickable_label() -> None:
    """A community ``gs://`` mirror path is classified but not clickable.

    The mirror path does not carry the original post URL, so the resolver
    degrades to a label (title / humanised filename) rather than minting a
    broken ``gs://`` href.
    """
    link = resolve_citation_link(_COMMUNITY_GS)

    assert link.kind == "community"
    assert link.clickable is False
    assert link.href is None
    # No explicit title / document id -> humanised filename stem.
    assert link.label == "quiesce snapshots"


# ---------------------------------------------------------------------------
# Already-canonical http(s) source -> pass-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_source_passes_through_as_href(scheme: str) -> None:
    """An already-navigable web URL is returned verbatim as the href."""
    url = f"{scheme}://docs.example.com/nsx/9.0/maximums"
    link = resolve_citation_link(url, title="NSX maximums")

    assert link.kind == "external"
    assert link.clickable is True
    assert link.href == url
    assert link.label == "NSX maximums"


def test_canonical_https_kb_link_passes_through_unchanged() -> None:
    """A KB source the corpus already canonicalised is not re-derived."""
    url = "https://knowledge.broadcom.com/external/article/390098"
    link = resolve_citation_link(url)

    # Pass-through arm wins for an http(s) URL (no gs:// rewrite needed).
    assert link.kind == "external"
    assert link.href == url


# ---------------------------------------------------------------------------
# Unrecognised source kind -> never a broken gs:// href (#1919 AC 2)
# ---------------------------------------------------------------------------


def test_unrecognised_gs_path_never_returns_gs_href() -> None:
    """An unrecognised ``gs://`` object degrades to a non-clickable label."""
    link = resolve_citation_link("gs://bucket/some/unknown/object.txt")

    assert link.kind == "unknown"
    assert link.clickable is False
    assert link.href is None
    assert link.label == "object"


def test_product_pdf_gs_path_degrades_not_a_broken_link() -> None:
    """A product-PDF object path degrades gracefully (no signing endpoint yet)."""
    link = resolve_citation_link(
        "gs://bucket/product-pdfs/vsphere/admin-guide.pdf",
        title="vSphere Administration Guide",
    )

    # No dedicated doc-portal mapping yet -> unknown arm, but never a gs:// href.
    assert link.clickable is False
    assert link.href is None
    assert link.label == "vSphere Administration Guide"


def test_no_source_url_yields_non_clickable_labelled_link() -> None:
    """A citation with no source URL still carries a human label."""
    link = resolve_citation_link(None, document_id="nsx-overview")

    assert link.clickable is False
    assert link.href is None
    assert link.label == "nsx-overview"


def test_blank_source_url_falls_back_to_placeholder_label() -> None:
    """A blank source URL with no title/id yields the placeholder label."""
    link = resolve_citation_link("   ")

    assert link.clickable is False
    assert link.label == "(no source)"


# ---------------------------------------------------------------------------
# Label precedence: title -> document_id -> humanised filename -> raw
# ---------------------------------------------------------------------------


def test_label_prefers_title_over_document_id() -> None:
    """An explicit title wins over the document id for the link text."""
    link = resolve_citation_link(_KB_GS, title="Friendly title", document_id="doc-1")
    assert link.label == "Friendly title"


def test_label_falls_back_to_document_id_when_no_title() -> None:
    """With no title, the document id is the link text."""
    link = resolve_citation_link(_KB_GS, document_id="doc-1")
    assert link.label == "doc-1"


def test_label_falls_back_to_humanised_filename_when_no_title_or_id() -> None:
    """With neither title nor document id, the humanised filename is used."""
    link = resolve_citation_link(_COMMUNITY_GS)
    assert link.label == "quiesce snapshots"


# ---------------------------------------------------------------------------
# JSON payload shape (what ask_docs / REST embeds per citation)
# ---------------------------------------------------------------------------


def test_citation_link_payload_shape() -> None:
    """The payload form carries href / label / kind / clickable."""
    payload = citation_link_payload(_KB_GS, title="vCenter maximums")

    assert payload == {
        "href": "https://knowledge.broadcom.com/external/article/414551",
        "label": "vCenter maximums",
        "kind": "broadcom_kb",
        "clickable": True,
    }


def test_citation_link_payload_unknown_has_null_href() -> None:
    """An unresolvable source serialises with a ``null`` href, never gs://."""
    payload = citation_link_payload("gs://bucket/x/y.bin")

    assert payload["href"] is None
    assert payload["clickable"] is False


def test_citation_link_is_frozen() -> None:
    """:class:`CitationLink` is immutable (a defensive mutation raises)."""
    link = resolve_citation_link(_KB_GS)
    with pytest.raises(AttributeError):
        link.href = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# normalize_source_ref (#132) — the backend-agnostic wire source_url
# ---------------------------------------------------------------------------


def test_normalize_kb_gs_becomes_canonical_url_not_gs() -> None:
    """A KB ``gs://`` path normalizes to the canonical portal URL (Option A)."""
    ref = normalize_source_ref(_KB_GS, collection_key="vmware", chunk_id="kb-1")

    assert ref == "https://knowledge.broadcom.com/external/article/414551"
    assert not ref.startswith("gs://")


def test_normalize_https_passes_through() -> None:
    """An already-canonical ``https`` source is its own reference."""
    url = "https://docs.vendor.test/vsan#disk-groups"
    assert normalize_source_ref(url, collection_key="vmware", chunk_id="c1") == url


def test_normalize_community_gs_becomes_opaque_meho_ref() -> None:
    """A community ``gs://`` path (no public URL) falls back to an opaque ref (Option B)."""
    ref = normalize_source_ref(_COMMUNITY_GS, collection_key="vmware", chunk_id="c-42")

    assert ref == "meho://docs/vmware/c-42"
    assert not ref.startswith("gs://")


def test_normalize_unknown_gs_becomes_opaque_meho_ref() -> None:
    """Any unrecognised ``gs://`` object falls back to the opaque ref, never gs://."""
    ref = normalize_source_ref("gs://bucket/x/y.bin", collection_key="vmware", chunk_id="z9")

    assert ref == "meho://docs/vmware/z9"
    assert not ref.startswith("gs://")


def test_normalize_none_source_still_yields_usable_ref() -> None:
    """A chunk with no source_url still gets a non-null opaque ref (AC4)."""
    ref = normalize_source_ref(None, collection_key="vmware", chunk_id="c1")

    assert ref == "meho://docs/vmware/c1"


def test_normalize_missing_collection_key_uses_placeholder_segment() -> None:
    """A blank/absent collection key keeps the ref well-formed (no empty segment)."""
    assert normalize_source_ref(None, collection_key=None, chunk_id="c1") == "meho://docs/_/c1"
    assert normalize_source_ref(_COMMUNITY_GS, collection_key="  ", chunk_id="c1") == (
        "meho://docs/_/c1"
    )
