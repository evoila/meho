# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Endpoint-surface embedding helpers for the G0.6 operation substrate.

Two small responsibilities so the registration helper
:func:`~meho_backplane.operations.typed_register.register_typed_operation`
stays readable:

* :func:`build_embedding_text` -- canonicalises the text the embedding
  is computed from. Every consumer that needs the change-detection
  hash recomputes it from the same composer, so a future tweak to
  what counts as "embeddable surface" (e.g. adding ``group_key``
  prose) lands in exactly one place.
* :func:`compute_embedding_text_hash` -- thin SHA-256 wrapper around
  the composed text. Reuses the existing G0.4-T3 ``compute_body_hash``
  contract (UTF-8 encoded, 64-char hex) so the substrate doesn't
  fork the change-detection algorithm. The function is kept here
  rather than re-exporting the indexer's helper directly so the
  endpoint surface owns its own load-bearing import path -- callers
  in T5 / T8 / G0.7 can swap in a different hash algorithm for this
  surface without touching the retrieval substrate.
* :func:`encode_endpoint_text` -- ``asyncio.to_thread``-wrapped
  ``encode_one`` against the process-wide :class:`EmbeddingService`.
  Returns the 384-dim ``list[float]`` matching
  :attr:`~meho_backplane.db.models.EndpointDescriptor.embedding` /
  :data:`~meho_backplane.retrieval.embedding.EMBEDDING_DIMENSION`.

The split between text composition and embedding compute lets the
register helper short-circuit on body-hash match (recompute the hash
of the persisted row's text, compare to the incoming hash) without
calling the embedding service at all -- the dominant connector-init
shape on restart.
"""

from __future__ import annotations

from collections.abc import Iterable

from meho_backplane.retrieval.embedding import EmbeddingService, get_embedding_service
from meho_backplane.retrieval.indexer import compute_body_hash

__all__ = [
    "build_embedding_text",
    "compute_embedding_text_hash",
    "encode_endpoint_text",
]


def build_embedding_text(
    *,
    summary: str,
    description: str,
    custom_description: str | None,
    tags: Iterable[str] | None,
) -> str:
    """Compose the canonical embeddable text for an endpoint descriptor.

    Layout (locked by Task #395 contract):

    ``summary + "\\n\\n" + description + "\\n\\n" + (custom_description or "")``
    ``+ "\\n" + " ".join(tags or [])``

    The blank-line separator between ``summary`` / ``description`` /
    ``custom_description`` keeps the tokenisation paragraph-aware --
    the BGE-small embedding model the retrieval service uses gives
    paragraph-bounded text noticeably better cluster geometry than a
    single space-joined run, per BGE's own evaluation card. The tags
    join is a single space because each tag is a short keyword token
    where paragraph semantics would dilute the signal.

    ``tags`` defaults to ``[]`` when ``None`` so callers can pass
    ``None`` without a defensive shim at every call site; the trailing
    ``"\\n"`` separator stays in either case so the composed shape is
    stable across "no tags" and "empty tag list" inputs (both produce
    a trailing ``"\\n"`` followed by an empty join).
    """
    tag_list = list(tags) if tags is not None else []
    return (
        summary
        + "\n\n"
        + description
        + "\n\n"
        + (custom_description or "")
        + "\n"
        + " ".join(tag_list)
    )


def compute_embedding_text_hash(text: str) -> str:
    """SHA-256 hex digest of *text* (UTF-8 encoded).

    Delegates to :func:`~meho_backplane.retrieval.indexer.compute_body_hash`
    so the operation substrate inherits the retrieval substrate's
    change-detection contract verbatim (UTF-8 encoding, 64-char
    hex, deterministic). A future hash-algorithm swap for the
    endpoint surface specifically -- e.g. switching to BLAKE2b for
    performance -- lands here without disturbing the retrieval
    pipeline.
    """
    return compute_body_hash(text)


async def encode_endpoint_text(
    text: str,
    *,
    service: EmbeddingService | None = None,
) -> list[float]:
    """Embed *text* into the 384-dim dense vector used by ``endpoint_descriptor``.

    Thin wrapper around :meth:`EmbeddingService.encode_one` so the
    registration helper has a single call shape and tests can inject
    a mock service. ``service=None`` resolves the process-wide
    singleton via :func:`get_embedding_service`; passing an explicit
    service is the supported test seam (production callers never
    bother).
    """
    if service is None:
        service = get_embedding_service()
    return await service.encode_one(text)
