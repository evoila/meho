# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-entry write attribution for kb rows (#1845).

kb rows are tenant-shared with no per-row ownership check: any
``tenant_admin`` may overwrite or delete any other principal's slug in
the same tenant (wiki-like, intended -- see
:mod:`meho_backplane.api.v1.kb`'s module docstring). That model is not
changed by this module. What this module adds is *attribution*: who
created a row and who last mutated it, so a kb entry is self-describing
instead of requiring an audit-log join to answer "who set this value?".

Both facts ride ``documents.doc_metadata`` (the JSONB column) rather
than new ORM columns, so they surface on every read surface that
already returns ``metadata`` -- ``GET /api/v1/kb/{slug}``, the list
preview, the ``POST /api/v1/kb`` response, and ``POST /api/v1/retrieve``
hits -- with no schema migration.

The single public function :func:`merge_attribution` folds the two
attribution keys into the metadata dict the substrate persists. It is
the trust boundary: caller-supplied attribution keys are stripped
before the verified OIDC ``sub`` is stamped, so an operator cannot
forge authorship by smuggling ``created_by_sub`` through a create body.
"""

from __future__ import annotations

from meho_backplane.kb.schemas import META_CREATED_BY_SUB, META_LAST_UPDATED_BY_SUB

__all__ = ["merge_attribution"]


def merge_attribution(
    *,
    caller_metadata: dict[str, object] | None,
    existing_metadata: dict[str, object] | None,
    actor_sub: str | None,
    created: bool,
) -> dict[str, object] | None:
    """Fold the attribution keys into the metadata persisted for a kb write.

    Rules:

    * Caller-supplied attribution keys are always stripped first --
      authorship is derived from the verified OIDC ``sub``, never from
      request JSON, so an operator cannot forge a ``created_by_sub`` by
      smuggling it through the create body.
    * ``created_by_sub`` is set to *actor_sub* on a genuine create and
      otherwise preserved from *existing_metadata* (so an overwrite --
      even cross-principal -- keeps the original author). An overwrite
      of a pre-attribution row backfills with the acting principal.
    * ``last_updated_by_sub`` is set to *actor_sub* on every attributed
      write.
    * When *actor_sub* is ``None`` (unattributed caller) and
      *caller_metadata* is ``None``, returns ``None`` so the substrate's
      "keep existing metadata on re-index" branch is preserved -- an
      unattributed re-index does not clobber a previously-attributed
      row's metadata.

    The result honours :func:`~meho_backplane.retrieval.indexer.index_document`'s
    metadata contract: ``None`` means "keep existing", a dict means
    "overwrite with this dict".
    """
    has_attribution = actor_sub is not None
    if caller_metadata is None and not has_attribution:
        # Nothing to write and no caller override -- defer to the
        # substrate's keep-existing-metadata-on-re-index branch.
        return None

    base = _base_metadata(caller_metadata, existing_metadata)

    # Strip any caller-smuggled attribution keys -- the sub is the trust
    # boundary, not request JSON.
    base.pop(META_CREATED_BY_SUB, None)
    base.pop(META_LAST_UPDATED_BY_SUB, None)

    if not has_attribution:
        # Caller passed metadata but no sub (unattended re-index with an
        # explicit metadata override). Preserve any prior
        # ``created_by_sub`` so a metadata-only update doesn't erase
        # authorship; leave ``last_updated_by_sub`` unset.
        _preserve_created(base, existing_metadata)
        return base

    base[META_LAST_UPDATED_BY_SUB] = actor_sub
    if created:
        base[META_CREATED_BY_SUB] = actor_sub
    elif not _preserve_created(base, existing_metadata):
        # Overwrite of a pre-attribution row (created before this
        # feature shipped): backfill ``created_by_sub`` with the actor.
        # Leaving it blank would make the row permanently authorless;
        # stamping the overwriter is the most truthful signal available
        # without an audit join.
        base[META_CREATED_BY_SUB] = actor_sub
    return base


def _base_metadata(
    caller_metadata: dict[str, object] | None,
    existing_metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Return the base dict the attribution keys are folded into.

    Caller's explicit metadata wins when provided; otherwise start from
    the existing row's metadata so an attributed re-index that passed
    ``metadata=None`` doesn't drop the operator's other keys (tags,
    source_path, ...).
    """
    if caller_metadata is not None:
        return dict(caller_metadata)
    if existing_metadata is not None:
        return dict(existing_metadata)
    return {}


def _preserve_created(
    base: dict[str, object],
    existing_metadata: dict[str, object] | None,
) -> bool:
    """Copy a prior ``created_by_sub`` from *existing_metadata* into *base*.

    Returns ``True`` when a prior value existed and was copied, ``False``
    otherwise -- the caller uses the boolean to decide whether a
    backfill is needed.
    """
    if existing_metadata is not None and META_CREATED_BY_SUB in existing_metadata:
        base[META_CREATED_BY_SUB] = existing_metadata[META_CREATED_BY_SUB]
        return True
    return False
