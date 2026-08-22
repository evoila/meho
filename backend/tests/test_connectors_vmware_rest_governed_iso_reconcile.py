# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for the governed content-library ISO recipe (#3086).

The recipe (``docs/codebase/vmware-rest-governed-iso-path.md``) names
raw ingested ops; this lane grounds that op list against the pinned
``vcenter-9.0/vcenter.yaml`` per the spec-reconcile standard
(``docs/decisions/spec-reconcile-guards-standard.md``):

* every recipe op_id is **served** by the pinned spec, byte-for-byte
  the string an operator's ingest would emit (path lane), and
* every bodied write op's ``requestBody`` is the **flat ``/api``
  shape** — its top-level properties are the model/spec's own fields,
  never the legacy ``/rest``-style single-``spec`` wrapper (body lane,
  the #2973/#3071 envelope class the generic dispatcher forwards
  verbatim from the caller's ``body`` param).

Division of labour (mirrors
``test_connectors_vmware_rest_composites_write_body_reconcile.py``):
this lane grounds the contract against the pinned vendor spec and
skips uniformly where the spec shelf is unprovisioned (the #2980
harness contract); the **connector-side** proof — that the generic
dispatch path puts those flat bodies on the wire — is the byte-for-byte
respx assertion set in
``test_connectors_vmware_rest_governed_iso_dispatch.py``, which runs
everywhere (including public CI, where the shelf is absent).
"""

from __future__ import annotations

from meho_backplane.operations.ingest import parse_openapi
from tests._governed_iso_recipe import RECIPE_OPS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_LABEL = "vcenter-9.0/vcenter.yaml"


def test_recipe_table_is_internally_consistent() -> None:
    """Always-on wiring guard: the table is non-empty and well-formed.

    A vacuously empty table would let both lanes go green while
    guarding nothing; a malformed op_id (no ``METHOD:`` prefix, or a
    body contract on a bodyless op) would ground the reconcile lane on
    the wrong strings.
    """
    assert RECIPE_OPS, "recipe op table is empty — the wiring broke"
    for op_id, op in RECIPE_OPS.items():
        assert op.op_id == op_id
        method, sep, path = op_id.partition(":")
        assert sep and path.startswith("/"), f"malformed op_id {op_id!r}"
        assert method in {"GET", "POST"}, f"unexpected recipe verb on {op_id!r}"
        expected_safety = "safe" if method == "GET" else "caution"
        assert op.safety_level == expected_safety, (
            f"{op_id}: recipe table says {op.safety_level!r} but the ingest "
            f"heuristic assigns {expected_safety!r} for {method}"
        )
        declares_body = "body" in (op.parameter_schema.get("properties") or {})
        assert declares_body == (op.body_props is not None), (
            f"{op_id}: body contract and seeded body param must agree"
        )


def test_every_recipe_op_is_served_by_the_pinned_spec() -> None:
    """Path lane: each recipe op_id exists in the pinned vcenter.yaml.

    Skips uniformly without the spec shelf; where the shelf is wired a
    red run is the guard surfacing a real finding (renamed endpoint,
    wrong-API-version path), never harness noise.
    """
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(RECIPE_OPS.keys(), served, spec_label=_SPEC_LABEL)


def test_recipe_write_bodies_are_flat_api_shapes() -> None:
    """Body lane: every bodied recipe write is served flat, never ``{"spec": ...}``.

    Checks two strengths per the table's contract:

    * ``body_props_exact`` ops (the iso.image pair) must match the
      pinned body properties **exactly** — a new required field there
      is a recipe-breaking change this lane must surface.
    * model-shaped bodies (item create / update-session create / file
      add) must serve at least the fields the recipe sends, with the
      spec-declared required set unchanged.
    """
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    body_props = openapi_request_body_props(spec_path)
    checked = 0
    for op_id, op in RECIPE_OPS.items():
        if op.body_props is None:
            continue
        served_props = body_props.get(op_id)
        assert served_props is not None, (
            f"{op_id}: the pinned {_SPEC_LABEL} serves no JSON request body for it — "
            "the recipe's body contract has no grounding (path moved or body dropped)."
        )
        assert served_props != {"spec"}, (
            f"{op_id}: single-`spec` request-body wrapper in the pinned spec — the "
            "/api surface takes the model at the top level (#2973). "
            "Protocol: docs/decisions/spec-reconcile-guards-standard.md."
        )
        if op.body_props_exact:
            assert served_props == set(op.body_props), (
                f"{op_id}: pinned body properties {sorted(served_props)} != the "
                f"recipe's {sorted(op.body_props)} — update the recipe doc + table."
            )
        else:
            assert set(op.body_props) <= served_props, (
                f"{op_id}: recipe sends {sorted(set(op.body_props) - served_props)} "
                f"which the pinned spec does not declare."
            )
        checked += 1
    assert checked, "no recipe request bodies were checked — table or parser wiring broke"


def test_recipe_required_body_fields_match_the_pinned_spec() -> None:
    """The spec's required-field sets for the recipe's bodied writes are pinned.

    A vendor bump that adds a required field to the mount body (or to
    the file-add AddSpec) silently breaks every recipe consumer; this
    lane turns that into a red diff against the pinned spec. Uses the
    raw parsed body schema (``required`` is not part of
    :func:`openapi_request_body_props`' property-name map).
    """
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source=f"spec:{spec_path.name}",
        content=spec_path.read_text(encoding="utf-8"),
    )
    body_required_by_op = {}
    for row in rows:
        schema = row.parameter_schema if isinstance(row.parameter_schema, dict) else {}
        body = (schema.get("properties") or {}).get("body")
        if isinstance(body, dict):
            body_required_by_op[row.op_id] = set(body.get("required") or [])
    checked = 0
    for op_id, op in RECIPE_OPS.items():
        if op.body_required is None:
            continue
        assert op_id in body_required_by_op, f"{op_id}: no parsed body schema"
        assert body_required_by_op[op_id] == set(op.body_required), (
            f"{op_id}: pinned required body fields "
            f"{sorted(body_required_by_op[op_id])} != recipe's "
            f"{sorted(op.body_required)} — a required-field drift breaks callers."
        )
        checked += 1
    assert checked, "no required-field sets were checked — table or parser wiring broke"
