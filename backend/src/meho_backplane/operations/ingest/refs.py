# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``$ref`` resolution + media-type selection helpers.

Lives in its own module so the resolver behaviour can be unit-tested
without spinning up the full parser, and so T2 (#403) can reuse the
helpers if multi-spec merge needs to re-resolve component references
across specs.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.operations.ingest.exceptions import (
    InvalidSchemaError,
    UnsupportedSpecError,
)

__all__ = [
    "PREFERRED_MEDIA_TYPES",
    "normalize_boolean_schema",
    "resolve_shallow_ref",
    "select_media_type_schema",
    "shallow_resolve_schema_field",
]


def normalize_boolean_schema(schema: Any) -> dict[str, object] | None:
    """Map a JSON Schema 2020-12 boolean schema to its dict equivalent.

    OpenAPI 3.1 aligns with JSON Schema 2020-12, which permits a bare
    boolean wherever a schema object is expected: ``true`` accepts
    every value, ``false`` rejects every value. The rest of the parser
    pipeline (parameter flattening, body extraction, response
    extraction) treats schemas as dicts so it can attach
    ``x-meho-param-loc`` and stitch them into the parent
    ``parameter_schema`` object. Rather than pushing ``bool | dict``
    union types through every helper, callers normalise here.

    * ``True`` → ``{}`` — the empty object schema matches everything,
      same semantics as ``true``.
    * ``False`` → ``{"not": {}}`` — ``not`` of the always-matches
      schema matches nothing, same semantics as ``false``.
    * ``dict`` → returned as-is. Callers wrap in ``dict(...)`` if
      they need a mutable copy.
    * Anything else → ``None``. Callers decide whether to drop the
      schema, raise, or fall back.
    """
    if schema is True:
        return {}
    if schema is False:
        return {"not": {}}
    if isinstance(schema, dict):
        return schema
    return None


# OpenAPI request bodies and responses are keyed by media type. We
# prefer ``application/json`` (every modern vendor spec uses it for
# the structured payload); fall back to ``*/*`` (some specs bind
# everything to wildcard), then to the first declared media type.
# Order matters — most specific first.
PREFERRED_MEDIA_TYPES = ("application/json", "*/*")


def resolve_shallow_ref(
    obj: Any,
    component_schemas: dict[str, Any],
) -> Any:
    """Inline one level of ``$ref``; preserve any nested refs unchanged.

    ``$ref: "#/components/schemas/X"`` returns the resolved schema X
    verbatim (a copy is NOT taken — callers that mutate must copy
    first). Component-path drill-down refs
    (``"#/components/schemas/X/properties/Y"``) raise
    :exc:`InvalidSchemaError`. Cross-document refs raise
    :exc:`UnsupportedSpecError`.

    Non-dict input or dicts without ``$ref`` pass through unchanged.
    """
    if not isinstance(obj, dict) or "$ref" not in obj:
        return obj
    ref = obj["$ref"]
    if not isinstance(ref, str):
        raise InvalidSchemaError(f"$ref value must be a string, got {type(ref).__name__}")
    if not ref.startswith("#/components/schemas/"):
        # Anything else — external file refs, parameter refs, response refs,
        # or fragment-walk refs into other component buckets — is out of
        # scope for v0.2. The dispatcher will not validate against them.
        if not ref.startswith("#/"):
            raise UnsupportedSpecError(f"cross-document $ref is not supported (got {ref!r})")
        raise UnsupportedSpecError(
            f"$ref to non-schema component is not supported (got {ref!r}); "
            f"v0.2 only inlines #/components/schemas/* refs"
        )
    name = ref[len("#/components/schemas/") :]
    if "/" in name:
        raise InvalidSchemaError(
            f"$ref drill-down into component subpaths is not supported (got {ref!r})"
        )
    if name not in component_schemas:
        raise InvalidSchemaError(f"$ref points at missing component (got {ref!r})")
    return component_schemas[name]


def select_media_type_schema(
    content: dict[str, Any],
    component_schemas: dict[str, Any],
) -> dict[str, object] | None:
    """Pick the JSON-leaning media type out of an OpenAPI ``content`` map.

    Try each preferred media type in priority order; only return when
    its schema actually resolves. If none of the preferred types yield
    a resolvable schema, fall through to the catch-all loop over the
    remaining declared media types. The earlier shape returned ``None``
    as soon as a preferred type was present but unresolvable, silently
    dropping perfectly valid wildcard/fallback schemas.
    """
    for media_type in PREFERRED_MEDIA_TYPES:
        if media_type not in content:
            continue
        resolved = shallow_resolve_schema_field(content[media_type], component_schemas)
        if resolved is not None:
            return resolved
    for media_type, payload in content.items():
        if not isinstance(media_type, str):
            continue
        if media_type in PREFERRED_MEDIA_TYPES:
            continue
        resolved = shallow_resolve_schema_field(payload, component_schemas)
        if resolved is not None:
            return resolved
    return None


def shallow_resolve_schema_field(
    media_type_obj: Any,
    component_schemas: dict[str, Any],
) -> dict[str, object] | None:
    """Return the resolved ``schema`` field of an OpenAPI media-type object.

    A boolean schema (OpenAPI 3.1 / JSON Schema 2020-12) is normalised
    to its dict equivalent via :func:`normalize_boolean_schema` so the
    parser doesn't have to thread ``bool | dict`` through every helper.
    Refs that resolve to a boolean component schema receive the same
    treatment.
    """
    if not isinstance(media_type_obj, dict):
        return None
    schema = media_type_obj.get("schema")
    normalised = normalize_boolean_schema(schema)
    if normalised is None:
        return None
    if isinstance(schema, dict):
        resolved = resolve_shallow_ref(schema, component_schemas)
        resolved_normalised = normalize_boolean_schema(resolved)
        if resolved_normalised is None:
            return None
        return dict(resolved_normalised)
    return dict(normalised)
