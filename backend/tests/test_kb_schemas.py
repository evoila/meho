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

from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_SOURCE,
    InvalidKbSlugError,
    KbEntry,
    KbEntrySearchHit,
    KbIngestionResult,
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


def test_kb_ingestion_result_partitions_files_into_four_buckets() -> None:
    """The four counters cover every file; the errors list carries the messages."""
    result = KbIngestionResult(
        inserted_count=2,
        updated_count=1,
        skipped_count=40,
        error_count=1,
        errors=["/path/to/bad.md: front-matter parse error"],
    )
    total = result.inserted_count + result.updated_count + result.skipped_count + result.error_count
    assert total == 44
    assert len(result.errors) == result.error_count
