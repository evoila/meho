# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``$ref`` resolution + media-type selection helpers.

Lives in its own module so the resolver behaviour can be unit-tested
without spinning up the full parser, and so T2 (#403) can reuse the
helpers if multi-spec merge needs to re-resolve component references
across specs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from meho_backplane.operations.ingest.exceptions import (
    InvalidSchemaError,
    UnsupportedSpecError,
)

__all__ = [
    "PREFERRED_MEDIA_TYPES",
    "collect_component_schema_closure",
    "find_unresolvable_local_refs",
    "iter_schema_refs",
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


#: The one component bucket a ``$ref`` nested *inside* a JSON Schema can
#: legally point at. Parameter / response / requestBody refs only occur at
#: the OpenAPI-object level and are resolved before schema extraction.
_SCHEMA_COMPONENT_REF_PREFIX = "#/components/schemas/"

#: JSON Schema keywords whose values are **named maps of subschemas** —
#: the child *keys* are arbitrary names (property names, definition
#: names), and every child *value* is itself a schema. The ref walk must
#: descend through the values without applying the data-key skip list to
#: the names (a property literally named ``enum`` still carries a schema).
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}
)

#: JSON Schema keywords whose values are **data payloads**, not
#: subschemas. A ``$ref``-shaped object inside an ``example`` or ``enum``
#: member is literal data the validator never resolves, so the ref walk
#: must not collect it (a false positive here would fail an ingest over
#: a ref the dispatcher would never dereference).
_DATA_VALUE_KEYWORDS = frozenset({"enum", "const", "default", "example", "examples"})


def iter_schema_refs(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` string in *node* that sits in a schema position.

    Walks the schema the way a JSON Schema 2020-12 validator does:
    generic keywords (``allOf`` / ``items`` / ``additionalProperties`` /
    ...) recurse structurally, named-map keywords
    (:data:`_SCHEMA_MAP_KEYWORDS`) recurse through their *values*, and
    data-valued keywords (:data:`_DATA_VALUE_KEYWORDS`) plus vendor
    ``x-*`` extensions are skipped entirely — refs inside them are
    literal data the validator never dereferences. A bundled
    ``components.schemas`` map (the shape
    :func:`collect_component_schema_closure` produces) is walked as a
    named map so lint passes over a stored descriptor cover the bundle.
    """
    if isinstance(node, list):
        for item in node:
            yield from iter_schema_refs(item)
        return
    if not isinstance(node, dict):
        return
    ref = node.get("$ref")
    if isinstance(ref, str):
        yield ref
    for key, value in node.items():
        if key == "$ref":
            continue
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            for subschema in value.values():
                yield from iter_schema_refs(subschema)
            continue
        if key == "components" and isinstance(value, dict):
            schemas_bucket = value.get("schemas")
            if isinstance(schemas_bucket, dict):
                for subschema in schemas_bucket.values():
                    yield from iter_schema_refs(subschema)
            continue
        if key in _DATA_VALUE_KEYWORDS or key.startswith("x-"):
            continue
        yield from iter_schema_refs(value)


def _unescape_pointer_segment(segment: str) -> str:
    """Apply RFC 6901 unescaping (``~1`` → ``/``, then ``~0`` → ``~``)."""
    return segment.replace("~1", "/").replace("~0", "~")


def collect_component_schema_closure(
    schema: Any,
    component_schemas: dict[str, Any],
) -> dict[str, Any]:
    """Return the transitive ``#/components/schemas/*`` closure *schema* needs.

    Walks every schema-position ``$ref`` in *schema* (via
    :func:`iter_schema_refs`), resolves each
    ``#/components/schemas/<name>`` against *component_schemas*, and
    recurses into the referenced components until the set is closed.
    Cycles (``A`` → ``B`` → ``A``, or self-referential components) are
    handled by the seen-set — each component is bundled exactly once,
    never expanded inline, so recursive vendor types terminate.

    The returned dict is sorted by component name so the persisted
    descriptor bytes are deterministic across ingests of the same spec.
    Component values are the spec's schema objects verbatim (no copy) —
    callers persist them via JSON serialization, never mutate them.

    Refs of any other shape (``#/$defs/...``, anchors, ``#``) are left
    for :func:`find_unresolvable_local_refs` to police against the final
    stored document.

    Raises:
        InvalidSchemaError: A ref drills into a component subpath, or
            names a component absent from *component_schemas* — same
            posture as the top-level resolver
            (:func:`resolve_shallow_ref`): a descriptor whose schema
            references a missing component could never validate any
            input, so the spec defect is surfaced at ingest time.
    """
    closure: dict[str, Any] = {}
    pending = [
        ref for ref in iter_schema_refs(schema) if ref.startswith(_SCHEMA_COMPONENT_REF_PREFIX)
    ]
    while pending:
        ref = pending.pop()
        segment = ref[len(_SCHEMA_COMPONENT_REF_PREFIX) :]
        if "/" in segment:
            raise InvalidSchemaError(
                f"$ref drill-down into component subpaths is not supported (got {ref!r})"
            )
        name = _unescape_pointer_segment(segment)
        if name in closure:
            continue
        if name not in component_schemas:
            raise InvalidSchemaError(f"$ref points at missing component (got {ref!r})")
        component = component_schemas[name]
        closure[name] = component
        pending.extend(
            nested
            for nested in iter_schema_refs(component)
            if nested.startswith(_SCHEMA_COMPONENT_REF_PREFIX)
        )
    return dict(sorted(closure.items()))


def find_unresolvable_local_refs(schema: Any) -> list[str]:
    """Return every schema-position ``$ref`` in *schema* that does not resolve.

    Resolution is what the dispatcher's jsonschema validator will do at
    call time: each ``#/<json-pointer>`` fragment is walked against
    *schema* itself (the stored document is the whole resolution
    universe — no registry, no retrieval). ``#`` (whole-document
    self-ref) resolves trivially; any non-fragment ref (cross-document)
    and any ``#<anchor>`` plain-name fragment is reported as
    unresolvable — the parser never stores either shape, so their
    presence means the descriptor would fail at dispatch.

    Returns a sorted, de-duplicated list; empty means the stored schema
    validates standalone.
    """
    unresolvable: set[str] = set()
    for ref in iter_schema_refs(schema):
        if ref == "#":
            continue
        if not ref.startswith("#/"):
            unresolvable.add(ref)
            continue
        node: Any = schema
        for raw_segment in ref[2:].split("/"):
            segment = _unescape_pointer_segment(raw_segment)
            if isinstance(node, dict) and segment in node:
                node = node[segment]
                continue
            # RFC 6901 array indices are ASCII digits only. ``str.isdigit``
            # alone also accepts non-ASCII digit codepoints ("²") that
            # ``int()`` then rejects with ValueError -- guard with
            # ``isascii`` so a pathological ref is flagged, not crashed on.
            if (
                isinstance(node, list)
                and segment.isascii()
                and segment.isdigit()
                and int(segment) < len(node)
            ):
                node = node[int(segment)]
                continue
            unresolvable.add(ref)
            break
    return sorted(unresolvable)


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
