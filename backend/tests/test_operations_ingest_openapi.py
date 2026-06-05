# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.operations.ingest.openapi`.

Fixture specs live next to this file at ``tests/fixtures/openapi/``.
Each fixture exercises one or two parser concerns so the failures
point at a specific contract violation rather than a soup of issues.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from pydantic import ValidationError

from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    InvalidSchemaError,
    InvalidSpecError,
    UnsupportedSpecError,
    UpstreamNotSpecError,
    detect_spec_format,
    parse_openapi,
    read_spec_info_version,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
PETSTORE_30 = FIXTURES / "petstore_30.yaml"
PETSTORE_31_YAML = FIXTURES / "petstore_31.yaml"
PETSTORE_31_JSON = FIXTURES / "petstore_31.json"
PARAMETER_REFS_30 = FIXTURES / "parameter_refs_30.yaml"
RESPONSE_REFS_30 = FIXTURES / "response_refs_30.yaml"
REQUEST_BODY_REFS_30 = FIXTURES / "request_body_refs_30.yaml"


def _by_op_id(rows: list[EndpointDescriptorProto]) -> dict[str, EndpointDescriptorProto]:
    return {r.op_id: r for r in rows}


# -- detect_spec_format -----------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"{", "json"),
        (b"  \n {", "json"),
        (b'["x"]', "json"),
        (b"openapi: 3.0.3", "yaml"),
        (b"# leading comment\nopenapi: 3.1.0", "yaml"),
        (b"\xef\xbb\xbf{", "json"),  # UTF-8 BOM
        (b"", "yaml"),
        (b"   \n\t  ", "yaml"),
    ],
)
def test_detect_spec_format(content: bytes, expected: str) -> None:
    assert detect_spec_format(content) == expected


# -- parse_openapi: happy paths --------------------------------------------


def test_parse_petstore_30_yields_expected_ops() -> None:
    rows = parse_openapi(str(PETSTORE_30), spec_source="spec:petstore_30.yaml")
    ops = _by_op_id(rows)
    expected_ids = {
        "GET:/pets",
        "POST:/pets",
        "GET:/pets/{petId}",
        "DELETE:/pets/{petId}",
        "PUT:/pets/{petId}/photos",
        "HEAD:/pets/{petId}/photos",
    }
    assert set(ops) == expected_ids


def test_parse_petstore_30_safety_heuristic() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    ops = _by_op_id(rows)
    assert ops["GET:/pets"].safety_level == "safe"
    assert ops["POST:/pets"].safety_level == "caution"
    assert ops["DELETE:/pets/{petId}"].safety_level == "dangerous"
    assert ops["PUT:/pets/{petId}/photos"].safety_level == "caution"
    assert ops["HEAD:/pets/{petId}/photos"].safety_level == "safe"


def test_parse_petstore_30_path_and_query_param_locations() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    list_pets = _by_op_id(rows)["GET:/pets"]
    props = list_pets.parameter_schema["properties"]
    assert isinstance(props, dict)
    # Path-level X-Tenant header propagates onto the GET.
    assert props["X-Tenant"]["x-meho-param-loc"] == "header"
    # Operation-level limit query param.
    assert props["limit"]["x-meho-param-loc"] == "query"
    # No body on a list endpoint.
    assert "body" not in props
    # X-Tenant is optional so it must NOT be required; limit is also optional.
    assert (
        "required" not in list_pets.parameter_schema or list_pets.parameter_schema["required"] == []
    )


def test_parse_petstore_30_path_param_is_required() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    get_pet = _by_op_id(rows)["GET:/pets/{petId}"]
    props = get_pet.parameter_schema["properties"]
    assert props["petId"]["x-meho-param-loc"] == "path"
    assert "petId" in get_pet.parameter_schema["required"]


def test_parse_petstore_30_request_body_inlined_with_param_loc() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    post = _by_op_id(rows)["POST:/pets"]
    props = post.parameter_schema["properties"]
    body = props["body"]
    assert body["x-meho-param-loc"] == "body"
    # $ref to #/components/schemas/Pet got inlined one level deep.
    assert body["type"] == "object"
    assert set(body["properties"].keys()) == {"id", "name", "tag"}
    # Nested $ref to Tag is preserved verbatim.
    assert body["properties"]["tag"] == {"$ref": "#/components/schemas/Tag"}
    # Body required because requestBody.required == true.
    assert "body" in post.parameter_schema["required"]


def test_parse_petstore_30_optional_body_not_required() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    put = _by_op_id(rows)["PUT:/pets/{petId}/photos"]
    assert put.parameter_schema["properties"]["body"]["x-meho-param-loc"] == "body"
    # requestBody.required omitted → not required.
    assert "body" not in put.parameter_schema.get("required", [])


def test_parse_petstore_30_response_schema_extracted() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    list_pets = _by_op_id(rows)["GET:/pets"]
    # PetPage was inlined; nested $ref to Pet preserved.
    assert list_pets.response_schema is not None
    assert list_pets.response_schema["type"] == "object"
    items_schema = list_pets.response_schema["properties"]["items"]
    assert items_schema["items"] == {"$ref": "#/components/schemas/Pet"}


def test_parse_petstore_30_204_response_yields_none() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    delete = _by_op_id(rows)["DELETE:/pets/{petId}"]
    assert delete.response_schema is None


def test_parse_petstore_30_tags_include_spec_source() -> None:
    rows = parse_openapi(str(PETSTORE_30), spec_source="spec:petstore_30.yaml")
    list_pets = _by_op_id(rows)["GET:/pets"]
    assert "pets" in list_pets.tags
    assert "spec:petstore_30.yaml" in list_pets.tags


def test_parse_petstore_30_without_spec_source_omits_tag() -> None:
    rows = parse_openapi(str(PETSTORE_30))
    list_pets = _by_op_id(rows)["GET:/pets"]
    assert list_pets.tags == ["pets"]
    # No accidental injection of an empty string or None.
    assert "" not in list_pets.tags


def test_parse_petstore_31_yaml_wildcard_2xx_response() -> None:
    rows = parse_openapi(str(PETSTORE_31_YAML))
    ops = _by_op_id(rows)
    list_vets = ops["GET:/vets"]
    assert list_vets.response_schema is not None
    # Wildcard 2XX response picked up.
    assert list_vets.response_schema["type"] == "array"


def test_parse_petstore_31_json_route() -> None:
    rows = parse_openapi(str(PETSTORE_31_JSON))
    assert {r.op_id for r in rows} == {"GET:/zoos"}
    assert rows[0].response_schema == {"type": "array", "items": {"type": "string"}}


# -- spec_source merging ---------------------------------------------------


def test_spec_source_threads_distinctly_across_two_specs(tmp_path: Path) -> None:
    """vCenter ingests vcenter.yaml + vi-json.yaml under one connector;
    rows from each spec must carry distinguishable ``spec:<source>`` tags."""
    rows_a = parse_openapi(str(PETSTORE_30), spec_source="spec:vcenter.yaml")
    rows_b = parse_openapi(str(PETSTORE_31_YAML), spec_source="spec:vi-json.yaml")
    all_tags_a = {tag for row in rows_a for tag in row.tags}
    all_tags_b = {tag for row in rows_b for tag in row.tags}
    assert "spec:vcenter.yaml" in all_tags_a
    assert "spec:vi-json.yaml" in all_tags_b
    assert "spec:vcenter.yaml" not in all_tags_b


# -- failure modes ---------------------------------------------------------


def test_unsupported_swagger_2_raises(tmp_path: Path) -> None:
    spec = tmp_path / "swagger.yaml"
    spec.write_text("swagger: '2.0'\ninfo: {title: x, version: '1'}\npaths: {}\n")
    with pytest.raises(UnsupportedSpecError, match=r"Swagger 2\.0") as excinfo:
        parse_openapi(str(spec))
    # The rejection must be *actionable* — it names the conversion path
    # (swagger2openapi / converter.swagger.io) so a 2.0-only surface
    # such as Harbor 2.x can be onboarded without an opaque dead end
    # (#1532). The detected version is echoed back verbatim.
    message = str(excinfo.value)
    assert "swagger2openapi" in message
    assert "converter.swagger.io" in message
    assert "'2.0'" in message


def test_unsupported_openapi_4_raises(tmp_path: Path) -> None:
    spec = tmp_path / "v4.yaml"
    spec.write_text("openapi: '4.0.0'\ninfo: {title: x, version: '1'}\npaths: {}\n")
    with pytest.raises(UnsupportedSpecError, match=r"4\.0\.0"):
        parse_openapi(str(spec))


def test_invalid_spec_missing_paths_raises(tmp_path: Path) -> None:
    spec = tmp_path / "broken.yaml"
    spec.write_text("openapi: '3.1.0'\ninfo: {title: x, version: '1'}\n")
    with pytest.raises(InvalidSpecError, match="no 'paths' key"):
        parse_openapi(str(spec))


def test_invalid_spec_non_mapping_root_raises(tmp_path: Path) -> None:
    spec = tmp_path / "list.yaml"
    spec.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(InvalidSpecError, match="must parse to a mapping"):
        parse_openapi(str(spec))


def test_missing_local_file_raises_invalid_spec(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(InvalidSpecError, match="could not read spec"):
        parse_openapi(str(missing))


def test_directory_path_raises_invalid_spec(tmp_path: Path) -> None:
    with pytest.raises(InvalidSpecError, match="could not read spec"):
        parse_openapi(str(tmp_path))


def test_malformed_yaml_bubbles_up(tmp_path: Path) -> None:
    spec = tmp_path / "bad.yaml"
    spec.write_text("openapi: '3.0.3'\npaths:\n  /x:\n   get:\n   summary: oops\n  : bad\n")
    with pytest.raises(yaml.YAMLError):
        parse_openapi(str(spec))


def test_malformed_json_bubbles_up(tmp_path: Path) -> None:
    spec = tmp_path / "bad.json"
    spec.write_text('{"openapi": "3.0.3", broken')
    with pytest.raises(json.JSONDecodeError):
        parse_openapi(str(spec))


def test_external_ref_raises(tmp_path: Path) -> None:
    spec = tmp_path / "ext.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      parameters:
        - $ref: 'other.yaml#/components/schemas/X'
      responses:
        "200":
          description: ok
        """.lstrip()
    )
    with pytest.raises(UnsupportedSpecError, match="cross-document"):
        parse_openapi(str(spec))


def test_component_parameter_ref_resolves(tmp_path: Path) -> None:
    """``$ref: '#/components/parameters/<name>'`` resolves to the shared parameter.

    The pre-T11 contract rejected this with ``UnsupportedSpecError`` —
    every vi-json.yaml operation uses this shape so the rejection
    blocked the entire ~2,195-op spec. The new contract inlines the
    referenced parameter the same way it inlines schema refs.
    """
    spec = tmp_path / "param_ref.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      parameters:
        - $ref: '#/components/parameters/Shared'
      responses:
        "200":
          description: ok
components:
  parameters:
    Shared:
      name: shared
      in: query
      required: true
      schema:
        type: string
        """.lstrip()
    )
    rows = parse_openapi(str(spec))
    assert len(rows) == 1
    properties = rows[0].parameter_schema["properties"]
    assert isinstance(properties, dict)
    assert "shared" in properties
    assert properties["shared"]["x-meho-param-loc"] == "query"
    assert properties["shared"]["type"] == "string"
    assert rows[0].parameter_schema["required"] == ["shared"]


def test_component_parameter_ref_missing_name_raises(tmp_path: Path) -> None:
    """A ref to an unknown parameter component raises ``InvalidSchemaError``.

    Mirrors the existing schema-bucket missing-component behaviour;
    the symmetry keeps the parser failure mode predictable across
    both component kinds.
    """
    spec = tmp_path / "param_ref_dangling.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      parameters:
        - $ref: '#/components/parameters/Missing'
      responses:
        "200":
          description: ok
components:
  parameters:
    Other:
      name: other
      in: query
      schema:
        type: string
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match="missing component"):
        parse_openapi(str(spec))


def test_component_parameter_ref_drilldown_raises(tmp_path: Path) -> None:
    """A ref into a parameter component's subpath raises ``InvalidSchemaError``.

    Symmetric with the schema-bucket drill-down rejection. The
    OpenAPI spec only declares fragment refs to *named* components.
    """
    spec = tmp_path / "param_ref_drilldown.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      parameters:
        - $ref: '#/components/parameters/Shared/schema'
      responses:
        "200":
          description: ok
components:
  parameters:
    Shared:
      name: shared
      in: query
      schema:
        type: string
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match="drill-down"):
        parse_openapi(str(spec))


def test_unsupported_component_bucket_ref_raises(tmp_path: Path) -> None:
    """Refs to component buckets the parser doesn't inline stay rejected.

    Headers refs are the canonical example: OpenAPI 3.x defines them
    as a component bucket, but the parser pipeline doesn't traverse
    ``responses.<code>.headers.<Name>`` slots (the slot's payload is
    metadata about the response envelope, not the operation's
    schema). A spec that puts a headers ref in a position the parser
    *does* traverse — e.g. inside the response slot itself, which is
    a malformed shape per OpenAPI 3.0 §4.7.16 (the response slot
    accepts only Response Object or a ref to one) — surfaces the
    residual ``UnsupportedSpecError`` envelope. The envelope shape is
    asserted here so future bucket gaps stay diagnosable.
    """
    spec = tmp_path / "headers_ref.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      responses:
        "200":
          $ref: '#/components/headers/X-Rate-Limit'
components:
  headers:
    X-Rate-Limit:
      schema:
        type: integer
        """.lstrip()
    )
    with pytest.raises(UnsupportedSpecError, match="unsupported component bucket"):
        parse_openapi(str(spec))


def test_resolve_shallow_ref_parameter_ref_opt_in() -> None:
    """Direct-call contract: parameter refs require ``component_parameters`` to opt in.

    ``parse_openapi`` always passes the dict (even when empty), so the
    full pipeline never trips this branch. The function is exported
    so T2 / future multi-spec merge can call it directly; this test
    locks the opt-in semantics for those callers.
    """
    from meho_backplane.operations.ingest.refs import resolve_shallow_ref

    ref = {"$ref": "#/components/parameters/X"}
    parameters = {"X": {"name": "x", "in": "query", "schema": {"type": "string"}}}

    # Opt-in: resolves cleanly.
    resolved = resolve_shallow_ref(ref, {}, parameters)
    assert resolved == parameters["X"]

    # Opt-out (None): legacy behaviour, rejected.
    with pytest.raises(UnsupportedSpecError, match="component_parameters"):
        resolve_shallow_ref(ref, {}, None)


def test_parse_parameter_refs_30_fixture() -> None:
    """End-to-end: a vi-json-shaped fixture parses cleanly.

    Locks the cross-op resolution path: one shared ``moId`` parameter
    is referenced by three operations and reassembled identically each
    time, including ``x-meho-param-loc="path"`` and required-by-default
    path-parameter semantics.
    """
    rows = parse_openapi(str(PARAMETER_REFS_30))
    op_ids = {row.op_id for row in rows}
    assert op_ids == {
        "POST:/vm/{moId}/PowerOn_Task",
        "POST:/vm/{moId}/PowerOff_Task",
        "POST:/host/{moId}/EnterMaintenanceMode_Task",
    }
    for row in rows:
        properties = row.parameter_schema["properties"]
        assert isinstance(properties, dict)
        assert "moId" in properties, f"{row.op_id}: missing moId after ref resolution"
        assert properties["moId"]["x-meho-param-loc"] == "path"
        assert properties["moId"]["type"] == "string"
        assert "moId" in row.parameter_schema["required"]
    # The host op carries an inlined ``timeout`` query param alongside
    # the resolved ``moId`` — the ref doesn't clobber the inlined param.
    host_op = next(r for r in rows if r.op_id == "POST:/host/{moId}/EnterMaintenanceMode_Task")
    properties = host_op.parameter_schema["properties"]
    assert isinstance(properties, dict)
    assert "timeout" in properties
    assert properties["timeout"]["x-meho-param-loc"] == "query"


def test_resolve_shallow_ref_response_ref_opt_in() -> None:
    """Direct-call contract: response refs require ``component_responses`` to opt in.

    Mirrors :func:`test_resolve_shallow_ref_parameter_ref_opt_in` for
    the response bucket. ``parse_openapi`` always threads the dict
    (even when empty), so the full pipeline never trips the opt-out
    branch — but the function is exported and the contract must be
    explicit for direct callers.
    """
    from meho_backplane.operations.ingest.refs import resolve_shallow_ref

    ref = {"$ref": "#/components/responses/Accepted"}
    responses = {
        "Accepted": {
            "description": "Accepted",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    }

    resolved = resolve_shallow_ref(ref, {}, component_responses=responses)
    assert resolved == responses["Accepted"]

    with pytest.raises(UnsupportedSpecError, match="component_responses"):
        resolve_shallow_ref(ref, {})


def test_resolve_shallow_ref_request_body_ref_opt_in() -> None:
    """Direct-call contract: requestBody refs require ``component_request_bodies``."""
    from meho_backplane.operations.ingest.refs import resolve_shallow_ref

    ref = {"$ref": "#/components/requestBodies/CreateBody"}
    request_bodies = {
        "CreateBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    }

    resolved = resolve_shallow_ref(ref, {}, component_request_bodies=request_bodies)
    assert resolved == request_bodies["CreateBody"]

    with pytest.raises(UnsupportedSpecError, match="component_request_bodies"):
        resolve_shallow_ref(ref, {})


def test_parse_response_refs_30_fixture() -> None:
    """End-to-end: GitHub-shaped response refs splice through cleanly.

    Models the GitHub REST spec's shape: shared response envelopes
    under ``components.responses`` referenced from operations via
    ``$ref: '#/components/responses/<name>'``. The success response's
    schema must end up extracted on each row — the splice happens
    transparently, the rest of the parser sees the resolved
    Response Object.
    """
    rows = parse_openapi(str(RESPONSE_REFS_30))
    by_op = _by_op_id(rows)
    assert set(by_op) == {"GET:/widgets", "GET:/widgets/{id}"}

    # List op: response ref resolves to an array of Widget; the
    # inner ``items`` $ref stays unresolved (shallow) — the
    # dispatcher walks nested refs lazily.
    list_op = by_op["GET:/widgets"]
    list_schema = list_op.response_schema
    assert isinstance(list_schema, dict)
    assert list_schema["type"] == "array"
    assert list_schema["items"] == {"$ref": "#/components/schemas/Widget"}

    # Single op: response ref resolves through to the Widget schema
    # via two-hop ($ref → content schema $ref). The parser inlines one
    # ref level per slot; the resolved response's
    # content.<media>.schema field itself carries a ref, which the
    # media-type extractor's existing schema-ref resolution handles.
    one_op = by_op["GET:/widgets/{id}"]
    one_schema = one_op.response_schema
    assert isinstance(one_schema, dict)
    assert one_schema["type"] == "object"
    assert "id" in one_schema["properties"]
    assert "name" in one_schema["properties"]


def test_parse_request_body_refs_30_fixture() -> None:
    """End-to-end: requestBody refs splice through cleanly.

    First-class component bucket per OpenAPI 3.x §4.7.10. Resolved
    request body's ``content.<media>.schema`` becomes the operation's
    ``body`` property with ``x-meho-param-loc='body'`` and
    ``required=True`` because the resolved Request Body Object's
    ``required`` field is ``True``.
    """
    rows = parse_openapi(str(REQUEST_BODY_REFS_30))
    by_op = _by_op_id(rows)
    assert set(by_op) == {"POST:/widgets", "PUT:/widgets/{id}"}

    for op_id in ("POST:/widgets", "PUT:/widgets/{id}"):
        op = by_op[op_id]
        properties = op.parameter_schema["properties"]
        assert isinstance(properties, dict), f"{op_id}: properties must be dict"
        assert "body" in properties, f"{op_id}: body missing after requestBody ref resolution"
        body = properties["body"]
        assert body["x-meho-param-loc"] == "body"
        # Resolved body schema is the WidgetCreate envelope (name only).
        assert body["type"] == "object"
        assert "name" in body["properties"]
        required = op.parameter_schema["required"]
        assert "body" in required, f"{op_id}: body must be required (requestBody.required=True)"


def test_parse_github_shaped_response_ref_with_inline_schema(tmp_path: Path) -> None:
    """Tightest reproduction of the GitHub spec's `responses/accepted` shape.

    GitHub's spec defines ``components.responses.accepted`` with a
    bare ``schema: {type: object}`` inside ``content.application/json``
    — no nested ``$ref``. Locks the simplest case so future
    refactors don't lose it.
    """
    spec = tmp_path / "gh_accepted.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /op:
    post:
      summary: trigger an async op
      responses:
        "202":
          $ref: '#/components/responses/accepted'
components:
  responses:
    accepted:
      description: Accepted
      content:
        application/json:
          schema:
            type: object
        """.lstrip()
    )
    rows = parse_openapi(str(spec))
    assert len(rows) == 1
    assert rows[0].response_schema == {"type": "object"}


def test_missing_response_ref_raises(tmp_path: Path) -> None:
    """A ref to an unknown response component raises ``InvalidSchemaError``."""
    spec = tmp_path / "dangling_response.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      responses:
        "200":
          $ref: '#/components/responses/Missing'
components:
  responses:
    Present:
      description: Present
      content:
        application/json:
          schema:
            type: string
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match="missing component"):
        parse_openapi(str(spec))


def test_missing_request_body_ref_raises(tmp_path: Path) -> None:
    """A ref to an unknown requestBody component raises ``InvalidSchemaError``."""
    spec = tmp_path / "dangling_requestbody.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    post:
      requestBody:
        $ref: '#/components/requestBodies/Missing'
      responses:
        "201":
          description: ok
components:
  requestBodies:
    Present:
      content:
        application/json:
          schema:
            type: string
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match="missing component"):
        parse_openapi(str(spec))


def test_missing_schema_ref_raises(tmp_path: Path) -> None:
    spec = tmp_path / "dangling.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    post:
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Missing'
      responses:
        "200":
          description: ok
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match="missing component"):
        parse_openapi(str(spec))


def test_non_list_tags_raises(tmp_path: Path) -> None:
    """A spec with ``tags: "admin"`` would otherwise iterate as characters."""
    spec = tmp_path / "bad_tags.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      tags: admin
      responses:
        "200":
          description: ok
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match=r"tags must be a list"):
        parse_openapi(str(spec))


def test_openapi_31_boolean_parameter_schema(tmp_path: Path) -> None:
    """OpenAPI 3.1 allows ``schema: true`` / ``schema: false`` on parameters."""
    spec = tmp_path / "bool_param.yaml"
    spec.write_text(
        """
openapi: '3.1.0'
info: {title: x, version: '1'}
paths:
  /any:
    get:
      parameters:
        - {name: anything, in: query, schema: true}
        - {name: nothing, in: query, schema: false}
      responses:
        "200":
          description: ok
        """.lstrip()
    )
    rows = parse_openapi(str(spec))
    props = rows[0].parameter_schema["properties"]
    assert isinstance(props, dict)
    # true → {} (matches anything)
    assert props["anything"] == {"x-meho-param-loc": "query"}
    # false → {"not": {}} (matches nothing) + the loc keyword
    assert props["nothing"] == {"not": {}, "x-meho-param-loc": "query"}


def test_openapi_31_boolean_body_and_response_schema(tmp_path: Path) -> None:
    """Boolean body / response schemas survive the parser pipeline."""
    spec = tmp_path / "bool_body.yaml"
    spec.write_text(
        """
openapi: '3.1.0'
info: {title: x, version: '1'}
paths:
  /any:
    post:
      requestBody:
        required: true
        content:
          application/json:
            schema: true
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: false
        """.lstrip()
    )
    rows = parse_openapi(str(spec))
    body = rows[0].parameter_schema["properties"]["body"]
    assert body == {"x-meho-param-loc": "body"}  # true → {} merged with loc keyword
    assert rows[0].response_schema == {"not": {}}  # false → {"not": {}}


def test_drilldown_schema_ref_raises(tmp_path: Path) -> None:
    spec = tmp_path / "drilldown.yaml"
    spec.write_text(
        """
openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    post:
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/X/properties/y'
      responses:
        "200":
          description: ok
components:
  schemas:
    X:
      type: object
      properties:
        y: {type: string}
        """.lstrip()
    )
    with pytest.raises(InvalidSchemaError, match="drill-down"):
        parse_openapi(str(spec))


# -- model behaviour -------------------------------------------------------


def test_proto_is_frozen() -> None:
    proto = EndpointDescriptorProto(op_id="GET:/x", method="GET", path="/x")
    with pytest.raises(ValidationError):
        proto.op_id = "POST:/x"  # type: ignore[misc]


def test_proto_safety_level_literal_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        EndpointDescriptorProto(
            op_id="GET:/x",
            method="GET",
            path="/x",
            safety_level="extreme",  # type: ignore[arg-type]
        )


# -- HTTP fetch ------------------------------------------------------------


def test_http_fetch_round_trip() -> None:
    """``http(s)://`` URLs are fetched via httpx."""
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get("https://example.test/spec.yaml").mock(
            return_value=httpx.Response(
                200,
                content=PETSTORE_31_YAML.read_bytes(),
                headers={"content-type": "application/yaml"},
            )
        )
        rows = parse_openapi("https://example.test/spec.yaml", spec_source="http")
    op_ids = {r.op_id for r in rows}
    assert op_ids == {"GET:/vets", "POST:/vets"}


def test_http_fetch_404_raises() -> None:
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get("https://example.test/missing.yaml").mock(
            return_value=httpx.Response(404, content=b"not found")
        )
        with pytest.raises(httpx.HTTPStatusError):
            parse_openapi("https://example.test/missing.yaml")


def test_http_fetch_html_content_type_raises_upstream_not_spec() -> None:
    """G0.15-T2 (#1211): a 2xx response carrying ``text/html`` (Broadcom
    Developer Portal) raises :exc:`UpstreamNotSpecError` instead of
    falling through to the YAML decoder.

    The carried ``upstream_url`` + ``content_type`` attributes are what
    the route layer builds the structured 422 envelope from; they must
    survive the exception flight intact.
    """
    portal_url = "https://developer.broadcom.com/xapis/vsphere-automation-api/latest/"
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(portal_url).mock(
            return_value=httpx.Response(
                200,
                content=b"<!doctype html><html></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        with pytest.raises(UpstreamNotSpecError) as exc_info:
            parse_openapi(portal_url)
    assert exc_info.value.upstream_url == portal_url
    assert exc_info.value.content_type == "text/html; charset=utf-8"


def test_http_fetch_missing_content_type_raises_upstream_not_spec() -> None:
    """G0.15-T2 (#1211): a 2xx response that omits ``Content-Type`` is
    treated as non-spec.

    Every legitimate spec host sets the header; absence is the
    fingerprint of a misconfigured upstream (or a default file server),
    so the guard fires fail-closed rather than guessing at the body.
    """
    url = "https://example.test/no-content-type.yaml"
    with respx.mock(assert_all_called=False) as mock_router:
        # ``httpx.Response(200, content=...)`` does not synthesize a
        # ``Content-Type`` header at transport time -- the receiving
        # side sees ``None``, which is exactly the missing-header case
        # the guard fails closed on.
        mock_router.get(url).mock(
            return_value=httpx.Response(200, content=b"openapi: 3.0.3\n"),
        )
        with pytest.raises(UpstreamNotSpecError) as exc_info:
            parse_openapi(url)
    assert exc_info.value.upstream_url == url
    assert exc_info.value.content_type is None


def test_http_fetch_application_yaml_content_type_accepted(
    tmp_path: Path,
) -> None:
    """G0.15-T2 (#1211): a 2xx response with ``application/yaml`` proceeds
    through the parser unchanged.

    Regression guard against the content-type allow-list being too
    narrow. The companion happy-path test
    :func:`test_http_fetch_round_trip` already pins this for
    ``application/yaml``; the parametrised variants below pin the rest
    of the allow-list so a future tightening of the allow-list can't
    silently break a working spec host.
    """
    url = "https://example.test/spec.yaml"
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(url).mock(
            return_value=httpx.Response(
                200,
                content=PETSTORE_31_YAML.read_bytes(),
                headers={"content-type": "application/yaml; charset=utf-8"},
            )
        )
        rows = parse_openapi(url)
    assert rows  # any non-empty result is enough — happy path is covered elsewhere


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "text/x-yaml",
        "text/plain",  # raw.githubusercontent.com
        "Application/JSON",  # case-insensitivity
    ],
)
def test_http_fetch_accepted_content_types_proceed(content_type: str) -> None:
    """G0.15-T2 (#1211): every entry in the accepted content-type allow-list
    survives the guard.

    A YAML body with a varying ``Content-Type`` header proves the guard
    only branches on the header value, not on the bytes -- the existing
    sniffer in ``detect_spec_format`` handles the bytes downstream.
    """
    url = f"https://example.test/spec-{content_type.replace('/', '-')}.yaml"
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(url).mock(
            return_value=httpx.Response(
                200,
                content=PETSTORE_31_YAML.read_bytes(),
                headers={"content-type": content_type},
            )
        )
        rows = parse_openapi(url)
    assert rows


# -- read_spec_info_version ------------------------------------------------


def test_read_spec_info_version_returns_string(tmp_path: Path) -> None:
    """Happy path — the spec's ``info.version`` is returned verbatim."""
    spec = tmp_path / "spec.yaml"
    spec.write_text("openapi: '3.0.3'\ninfo: {title: t, version: '9.0.3'}\npaths: {}\n")
    assert read_spec_info_version(str(spec)) == "9.0.3"


def test_read_spec_info_version_missing_info_returns_none(tmp_path: Path) -> None:
    """Specs without an ``info`` block return ``None`` — the cross-check skips."""
    spec = tmp_path / "spec.yaml"
    spec.write_text("openapi: '3.0.3'\npaths: {}\n")
    assert read_spec_info_version(str(spec)) is None


def test_read_spec_info_version_missing_version_field_returns_none(tmp_path: Path) -> None:
    """``info`` present but no ``version`` field → ``None``."""
    spec = tmp_path / "spec.yaml"
    spec.write_text("openapi: '3.0.3'\ninfo: {title: t}\npaths: {}\n")
    assert read_spec_info_version(str(spec)) is None


def test_read_spec_info_version_non_string_version_returns_none(tmp_path: Path) -> None:
    """A non-string ``info.version`` (e.g. an integer) → ``None``."""
    spec = tmp_path / "spec.yaml"
    spec.write_text("openapi: '3.0.3'\ninfo: {title: t, version: 1}\npaths: {}\n")
    assert read_spec_info_version(str(spec)) is None


def test_read_spec_info_version_empty_version_returns_none(tmp_path: Path) -> None:
    """An empty ``info.version`` string is treated as missing."""
    spec = tmp_path / "spec.yaml"
    spec.write_text("openapi: '3.0.3'\ninfo: {title: t, version: ''}\npaths: {}\n")
    assert read_spec_info_version(str(spec)) is None


def test_read_spec_info_version_rejects_swagger_2(tmp_path: Path) -> None:
    """Swagger 2.0 specs surface the same gate :func:`parse_openapi` enforces.

    The cross-check helper shares ``_validate_openapi_version`` with the
    parser, so the same actionable conversion-path remedy reaches the
    operator on the spec-vs-label fast path (#1532).
    """
    spec = tmp_path / "spec.yaml"
    spec.write_text("swagger: '2.0'\ninfo: {title: t, version: '9.0.3'}\npaths: {}\n")
    with pytest.raises(UnsupportedSpecError, match=r"Swagger 2\.0") as excinfo:
        read_spec_info_version(str(spec))
    assert "swagger2openapi" in str(excinfo.value)


def test_read_spec_info_version_missing_file_raises_invalid_spec(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(InvalidSpecError, match="could not read spec"):
        read_spec_info_version(str(missing))
