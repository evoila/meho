# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.kb.schemas`.

Coverage matrix:

* :data:`SLUG_PATTERN` accepts the consumer's real dotted slugs
  (``vcenter-9.0-snapshot-revert``) -- the regression check against
  the task body's own example.
* :data:`SLUG_PATTERN` rejects mixed-case, leading separator,
  trailing separator, empty string, and Unicode -- the operator-
  facing identifier contract.
* :func:`validate_slug` returns the input on accept; raises
  :class:`InvalidKbSlugError` on reject with the bad slug + regex in the
  message.
* :class:`KbEntry`, :class:`KbEntrySearchHit`, :class:`KbIngestionResult`
  instantiate from valid input; reject attribute reassignment because
  every model is ``frozen=True``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meho_backplane.kb.provenance import read_provenance, stamp_provenance
from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_SOURCE,
    META_PROVENANCE,
    InvalidKbSlugError,
    KbEntry,
    KbEntrySearchHit,
    KbIngestConflict,
    KbIngestionResult,
    KbProvenance,
    validate_slug,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_kb_source_and_kind_are_load_bearing_strings() -> None:
    """The substrate filter values are pinned -- changing them is a data migration."""
    assert KB_SOURCE == "kb"
    assert KB_KIND_ENTRY == "kb-entry"


# ---------------------------------------------------------------------------
# Slug acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "vcenter-9.0-snapshot-revert",  # the task body's own example
        "k8s-ingress",  # plain hyphenated
        "vault-1.x-kv-v2",  # multiple version segments
        "harbor",  # single-segment
        "a",  # single-char (regex allows)
        "a1",  # short numeric
        "argocd-2.10-rolling-deploys",  # realistic dotted
    ],
)
def test_validate_slug_accepts_consumer_kb_shapes(slug: str) -> None:
    """The slug regex must accept every shape the consumer's 44-entry kb uses."""
    assert validate_slug(slug) == slug


# ---------------------------------------------------------------------------
# Slug rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "1-leading-digit",
        "-leading-hyphen",
        ".leading-dot",
        "trailing-hyphen-",
        "trailing-dot.",
        "MixedCase",
        "has_underscore",
        "has space",
        "has/slash",
        "café",
    ],
)
def test_validate_slug_rejects_out_of_shape_input(slug: str) -> None:
    """Every shape the consumer would NOT produce raises :class:`InvalidKbSlugError`."""
    with pytest.raises(InvalidKbSlugError) as exc_info:
        validate_slug(slug)
    # The bad slug surfaces in the message so an operator chasing the
    # failure has full context.
    assert repr(slug) in str(exc_info.value)


def test_invalid_kb_slug_is_a_value_error_subclass() -> None:
    """Caller can catch :class:`ValueError` without importing the kb module."""
    assert issubclass(InvalidKbSlugError, ValueError)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def _make_kb_entry() -> KbEntry:
    return KbEntry(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        slug="example-entry",
        body="Body text.",
        metadata={"author": "ops"},
        provenance=KbProvenance(state="legacy"),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_kb_entry_constructs_from_valid_inputs() -> None:
    entry = _make_kb_entry()
    assert entry.slug == "example-entry"
    assert entry.body == "Body text."
    assert entry.metadata == {"author": "ops"}


def test_kb_entry_is_frozen_against_attribute_assignment() -> None:
    """``frozen=True`` rejects ``entry.slug = ...`` post-construction."""
    entry = _make_kb_entry()
    with pytest.raises(ValidationError):
        entry.slug = "different"  # type: ignore[misc]


def test_kb_entry_search_hit_constructs_with_optional_scores() -> None:
    """Per-signal scores + ranks may be ``None`` when a hit appeared in only one signal."""
    hit = KbEntrySearchHit(
        slug="example",
        snippet="Some preview text.",
        metadata={},
        fused_score=0.42,
        bm25_score=None,
        cosine_score=0.81,
        bm25_rank=None,
        cosine_rank=1,
    )
    assert hit.slug == "example"
    assert hit.bm25_score is None
    assert hit.cosine_rank == 1


def test_kb_ingestion_result_partitions_files_into_five_buckets() -> None:
    """The five counters cover every file; errors and conflicts carry the detail."""
    result = KbIngestionResult(
        inserted_count=2,
        updated_count=1,
        skipped_count=38,
        error_count=1,
        conflict_count=2,
        errors=["bad.md: front-matter parse error"],
        conflicts=[KbIngestConflict(slug="dup", relative_paths=["a/dup.md", "b/dup.md"])],
    )
    total = (
        result.inserted_count
        + result.updated_count
        + result.skipped_count
        + result.error_count
        + result.conflict_count
    )
    assert total == 44
    assert len(result.errors) == result.error_count
    assert sum(len(c.relative_paths) for c in result.conflicts) == result.conflict_count


def test_kb_ingestion_result_requires_conflict_fields() -> None:
    """The conflict fields are part of the contract, not optional extras."""
    with pytest.raises(ValidationError):
        KbIngestionResult.model_validate(
            {
                "inserted_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 0,
                "errors": [],
            }
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

_INGESTED_AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_stamp_then_read_round_trips_directory_provenance() -> None:
    stamped = stamp_provenance(
        {"tags": ["ops"]},
        source_kind="directory",
        relative_path="runbooks/vm-restart.md",
        ingested_at=_INGESTED_AT,
    )
    assert stamped["tags"] == ["ops"]
    assert stamped[META_PROVENANCE] == {
        "source_kind": "directory",
        "relative_path": "runbooks/vm-restart.md",
        "ingested_at": "2026-03-04T05:06:07Z",
    }
    assert read_provenance(stamped) == KbProvenance(
        state="recorded",
        source_kind="directory",
        relative_path="runbooks/vm-restart.md",
        ingested_at=_INGESTED_AT,
    )


def test_stamp_replaces_a_caller_supplied_provenance_value() -> None:
    stamped = stamp_provenance(
        {META_PROVENANCE: {"source_kind": "directory", "relative_path": "forged.md"}},
        source_kind="direct",
        relative_path=None,
        ingested_at=_INGESTED_AT,
    )
    provenance = read_provenance(stamped)
    assert provenance.source_kind == "direct"
    assert provenance.relative_path is None


@pytest.mark.parametrize(
    "relative_path",
    ["/srv/kb/a.md", "C:\\kb\\a.md", "../outside.md", "a/../../b.md"],
)
def test_stamp_rejects_absolute_or_escaping_paths(relative_path: str) -> None:
    with pytest.raises(ValueError, match="relative_path"):
        stamp_provenance(
            {},
            source_kind="directory",
            relative_path=relative_path,
            ingested_at=_INGESTED_AT,
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"source_path": "/old/host/path/entry.md"},
        {META_PROVENANCE: "hand-written note"},
        {META_PROVENANCE: {"source_kind": "directory"}},
        {
            META_PROVENANCE: {
                "source_kind": "directory",
                "relative_path": "/abs/entry.md",
                "ingested_at": "2026-03-04T05:06:07Z",
            }
        },
    ],
    ids=["empty", "legacy-source-path", "string", "partial", "absolute-path"],
)
def test_read_provenance_reports_legacy_for_missing_or_unknown_shapes(
    metadata: dict[str, object],
) -> None:
    assert read_provenance(metadata) == KbProvenance(state="legacy")
