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
    component_parameters: dict[str, Any] | None = None,
    component_responses: dict[str, Any] | None = None,
    component_request_bodies: dict[str, Any] | None = None,
) -> Any:
    """Inline one level of ``$ref``; preserve any nested refs unchanged.

    Supports refs to four OpenAPI 3.x component buckets:

    * ``$ref: "#/components/schemas/X"`` — JSON Schema components.
      The resolved schema is returned verbatim (a copy is NOT taken —
      callers that mutate must copy first).
    * ``$ref: "#/components/parameters/X"`` — Parameter Object
      components (OpenAPI 3.0 §4.7.12 / 3.1 §4.8.7). Same return
      semantics. ``vi-json.yaml`` uses this on every operation via the
      shared ``moId`` path parameter.
    * ``$ref: "#/components/responses/X"`` — Response Object
      components (OpenAPI 3.0 §4.7.7 / 3.1 §4.8.16). The GitHub REST
      API spec uses this for every shared response shape
      (``accepted``, ``not_found``, ``validation_failed`` etc) —
      1929 ref hits across the spec as of 2026-05-27.
    * ``$ref: "#/components/requestBodies/X"`` — Request Body Object
      components (OpenAPI 3.0 §4.7.10 / 3.1 §4.8.13). Not used by
      GitHub's spec, but in scope for parser parity since it's a
      first-class component bucket per the OpenAPI 3.x spec and
      future-onboarded vendors may use it.

    Each opt-in kwarg defaults to ``None`` so existing callers that
    only need schema-ref resolution don't have to thread the extra
    arg. Passing ``None`` keeps the legacy behaviour for that bucket
    (refs raise :exc:`UnsupportedSpecError`); passing a dict — even
    an empty one — opts in to the bucket branch. ``parse_openapi``
    always threads the full set (even empty dicts) so the full
    pipeline never trips the opt-out branch.

    Component-path drill-down refs into any bucket
    (``"#/components/schemas/X/properties/Y"`` or
    ``"#/components/parameters/X/schema"``) raise
    :exc:`InvalidSchemaError` — the OpenAPI spec only declares fragment
    refs to *named* components, not to subpaths within them.
    Cross-document refs raise :exc:`UnsupportedSpecError`.

    Non-dict input or dicts without ``$ref`` pass through unchanged.
    """
    if not isinstance(obj, dict) or "$ref" not in obj:
        return obj
    ref = obj["$ref"]
    if not isinstance(ref, str):
        raise InvalidSchemaError(f"$ref value must be a string, got {type(ref).__name__}")
    if ref.startswith("#/components/schemas/"):
        return _resolve_named_component(
            ref=ref,
            bucket=component_schemas,
            prefix="#/components/schemas/",
        )
    if ref.startswith("#/components/parameters/"):
        if component_parameters is None:
            # Caller did not opt in to parameter-ref resolution.
            # Legacy behaviour: reject. Threading
            # ``component_parameters`` from ``parse_openapi`` flips
            # this branch off for the full pipeline.
            raise UnsupportedSpecError(
                f"$ref to #/components/parameters/* requires the caller to pass "
                f"component_parameters (got {ref!r})"
            )
        return _resolve_named_component(
            ref=ref,
            bucket=component_parameters,
            prefix="#/components/parameters/",
        )
    if ref.startswith("#/components/responses/"):
        if component_responses is None:
            raise UnsupportedSpecError(
                f"$ref to #/components/responses/* requires the caller to pass "
                f"component_responses (got {ref!r})"
            )
        return _resolve_named_component(
            ref=ref,
            bucket=component_responses,
            prefix="#/components/responses/",
        )
    if ref.startswith("#/components/requestBodies/"):
        if component_request_bodies is None:
            raise UnsupportedSpecError(
                f"$ref to #/components/requestBodies/* requires the caller to pass "
                f"component_request_bodies (got {ref!r})"
            )
        return _resolve_named_component(
            ref=ref,
            bucket=component_request_bodies,
            prefix="#/components/requestBodies/",
        )
    # Anything else — external file refs, refs into other component
    # buckets (headers, securitySchemes, links, callbacks, examples)
    # — is out of scope. The dispatcher will not validate against
    # them.
    if not ref.startswith("#/"):
        raise UnsupportedSpecError(f"cross-document $ref is not supported (got {ref!r})")
    raise UnsupportedSpecError(
        f"$ref to unsupported component bucket (got {ref!r}); "
        f"parser inlines #/components/schemas/*, #/components/parameters/*, "
        f"#/components/responses/*, and #/components/requestBodies/* only"
    )


def _resolve_named_component(
    *,
    ref: str,
    bucket: dict[str, Any],
    prefix: str,
) -> Any:
    """Resolve a ``#/components/<kind>/<name>`` ref against ``bucket``.

    Shared between the schema-ref branch and the parameter-ref branch
    of :func:`resolve_shallow_ref` so drill-down and missing-component
    handling stay symmetric across both buckets.
    """
    name = ref[len(prefix) :]
    if "/" in name:
        raise InvalidSchemaError(
            f"$ref drill-down into component subpaths is not supported (got {ref!r})"
        )
    if name not in bucket:
        raise InvalidSchemaError(f"$ref points at missing component (got {ref!r})")
    return bucket[name]


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
