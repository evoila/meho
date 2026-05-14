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
    detect_spec_format,
    parse_openapi,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
PETSTORE_30 = FIXTURES / "petstore_30.yaml"
PETSTORE_31_YAML = FIXTURES / "petstore_31.yaml"
PETSTORE_31_JSON = FIXTURES / "petstore_31.json"


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
    with pytest.raises(UnsupportedSpecError, match=r"Swagger 2\.0"):
        parse_openapi(str(spec))


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


def test_non_schema_local_ref_raises(tmp_path: Path) -> None:
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
      schema:
        type: string
        """.lstrip()
    )
    with pytest.raises(UnsupportedSpecError, match="non-schema component"):
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
