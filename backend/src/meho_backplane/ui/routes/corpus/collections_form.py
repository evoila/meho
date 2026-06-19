# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure form-parsing + error-projection helpers for the register modal.

Initiative #1836 (G10.10 Doc Collections lifecycle UI), Task #1882 (T1).
Split out of :mod:`meho_backplane.ui.routes.corpus.collections` so the
route module stays under the chassis-wide ~600-line cap and these
``Request``-free projections stay unit-testable without a FastAPI fixture
(the same split :mod:`meho_backplane.ui.routes.connectors.forms` /
``forms_router`` use).
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from meho_backplane.db.models import DocCollection as DocCollectionORM

__all__ = [
    "dedupe_tenant_first",
    "parse_backend_ref",
    "parse_products",
    "validation_errors_by_field",
]


def dedupe_tenant_first(rows: list[DocCollectionORM]) -> list[DocCollectionORM]:
    """Collapse global+tenant rows sharing a ``collection_key``, tenant wins.

    The same order-independent dedupe the catalogue list route runs
    (:func:`meho_backplane.api.v1.doc_collections._dedupe_tenant_first`): a
    key present as both a global row (``tenant_id IS NULL``) and a
    tenant-curated row collapses to the tenant row, which overrides the
    global backend binding / metadata. Listing both would show the same key
    twice and surface the shadowed global metadata. Reproduced here rather
    than importing the REST module's ``_``-prefixed helper (the dedupe is
    shared by every collection-listing face by convention, not by import).
    """
    by_key: dict[str, DocCollectionORM] = {}
    for row in rows:
        existing = by_key.get(row.collection_key)
        if existing is None or row.tenant_id is not None:
            by_key[row.collection_key] = row
    return list(by_key.values())


def parse_products(raw: str | None) -> tuple[str, ...]:
    """Split the comma-separated ``products`` field into a tuple.

    Empty / whitespace entries are dropped and duplicates de-duplicated
    while preserving first-seen order, mirroring the connectors
    ``parse_aliases`` idiom. Returns ``()`` when *raw* is missing.
    """
    if not raw or not raw.strip():
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for part in raw.split(","):
        cleaned = part.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return tuple(result)


def parse_backend_ref(raw: str | None) -> dict[str, object]:
    """Parse the ``backend.ref`` JSON textarea into a mapping.

    An empty / whitespace value is an empty ``{}`` (a backend whose adapter
    reads no per-collection config). A non-empty value must parse to a JSON
    **object**; a malformed value or a non-object (a list, a bare string)
    raises :class:`ValueError` so the submit handler re-renders the modal
    with a ``backend_ref`` field error rather than 500-ing.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"backend.ref must be valid JSON: {exc.msg}"
        raise ValueError(msg) from exc
    if not isinstance(parsed, dict):
        msg = 'backend.ref must be a JSON object (e.g. {"endpoint": "https://..."})'
        raise ValueError(msg)
    return parsed


def validation_errors_by_field(exc: ValidationError) -> dict[str, str]:
    """Project a Pydantic :class:`ValidationError` into a field->message map.

    Keys are the first ``loc`` element (the field name); the value is the
    human ``msg`` Pydantic produced. The backend ``{type, ref}`` is built as
    a standalone :class:`DocCollectionBackend`, so a blank ``type`` surfaces
    with ``loc=('type',)``; it is re-attributed to the ``backend_type`` form
    field (and ``ref`` to ``backend_ref``) so the message lands under the
    right input. Mirrors the connectors ``_validation_errors_by_field``.
    """
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        if "type" in loc:
            field = "backend_type"
        elif "ref" in loc:
            field = "backend_ref"
        elif loc:
            field = str(loc[0])
        else:
            field = "__root__"
        errors.setdefault(field, str(err.get("msg", "invalid value")))
    return errors
