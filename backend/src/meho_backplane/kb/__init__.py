# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Knowledge-base layer -- tenant-scoped retrieval of kb entries.

Initiative #331 (G4.1 KB migration + verbs) lands the operator-facing
knowledge surface on top of G0.4's ``documents`` substrate (#225). The
kb module owns slug-shaped entries: every row written through
:class:`~meho_backplane.kb.service.KbService` lands in the shared
``documents`` table with ``source='kb'`` and ``kind='kb-entry'``, so
hybrid BM25 + cosine retrieval (G0.4-T4) and the body-hash dedup
optimisation (G0.4-T3) both apply unchanged.

Module map (Initiative #331 wave structure):

* :mod:`meho_backplane.kb.schemas` -- Pydantic v2 frozen models the
  service hands back to callers (T1, this Task).
* :mod:`meho_backplane.kb.file_walker` -- directory walker that turns
  a kb directory into a stream of ``(slug, body, metadata)`` tuples,
  with front-matter override + hidden-file skip + ``.kb-ignore``
  glob support (T1).
* :mod:`meho_backplane.kb.service` -- :class:`KbService` with the six
  per-tenant operations every later wave consumes: ``ingest_directory``
  / ``list_entries`` / ``get_entry`` / ``create_entry`` /
  ``delete_entry`` / ``search_entries`` (T1).

The HTTP surface (5 routes under ``/api/v1/kb*``) lands in T2 #416;
the MCP meta-tools (``search_knowledge`` / ``add_to_knowledge`` +
``meho://kb/{slug}`` resource) in T3 #417; the operator-facing CLI
verbs in T4 #418. Each later wave calls into :class:`KbService` --
the service is the substrate every wave converges on.
"""

from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_SOURCE,
    SLUG_PATTERN,
    InvalidKbSlugError,
    KbEntry,
    KbEntrySearchHit,
    KbIngestionResult,
    validate_slug,
)
from meho_backplane.kb.service import KbIngestRootError, KbService

__all__ = [
    "KB_KIND_ENTRY",
    "KB_SOURCE",
    "SLUG_PATTERN",
    "InvalidKbSlugError",
    "KbEntry",
    "KbEntrySearchHit",
    "KbIngestRootError",
    "KbIngestionResult",
    "KbService",
    "validate_slug",
]
