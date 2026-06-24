# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.operations.ingest.openapi`.

Fixture specs live next to this file at ``tests/fixtures/openapi/``.
Each fixture exercises one or two parser concerns so the failures
point at a specific contract violation rather than a soup of issues.

Since G0.16-T8 (#95) the spec fetcher only accepts ``https://`` URIs
on the network-facing path. Tests that formerly exercised the parser
via local file paths now serve fixture content through respx-mocked
HTTPS endpoints so the same parsing assertions remain valid.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

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
from meho_backplane.operations.ingest.openapi import _MAX_REDIRECTS, _assert_fetchable_remote_url

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
PETSTORE_30 = FIXTURES / "petstore_30.yaml"
PETSTORE_31_YAML = FIXTURES / "petstore_31.yaml"
PETSTORE_31_JSON = FIXTURES / "petstore_31.json"
PARAMETER_REFS_30 = FIXTURES / "parameter_refs_30.yaml"
RESPONSE_REFS_30 = FIXTURES / "response_refs_30.yaml"
REQUEST_BODY_REFS_30 = FIXTURES / "request_body_refs_30.yaml"
HANDAUTHORED_MINIMAL_30 = FIXTURES / "handauthored_minimal_30.yaml"

# Stable HTTPS URL prefix used by tests that serve fixture content through
# respx. Using a dedicated subdomain makes it easy to route all fixture
# fetches in a single mock block when tests need multiple specs.
_FIXTURE_HOST = "https://specs.example.test"

# A stable public IP returned by the ``getaddrinfo`` patch for every test
# domain used in this module. ``93.184.216.34`` is IANA's example.com
# address — public and non-special per RFC 5737 / the ipaddress module.
_PUBLIC_TEST_IP = "93.184.216.34"

# Hostnames that appear in test HTTPS URLs and need a mocked getaddrinfo
# response so the SSRF destination guard passes without real DNS lookups.
_TEST_HOSTS = frozenset(
    {
        "specs.example.test",
        "example.test",
        "developer.broadcom.com",
        "public.example.test",
        "cdn.example.test",
        "public-looking.example.test",
    }
)


def _getaddrinfo_for_tests(
    host: str, port: object, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Mock ``socket.getaddrinfo`` for the SSRF guard used in test calls.

    Returns a public IP for all known test hostnames so the destination
    guard passes; delegates to the real ``getaddrinfo`` for everything
    else (private/metadata IPs tested via their own per-test patches).
    """
    if host in _TEST_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_PUBLIC_TEST_IP, 443))]
    return socket.getaddrinfo(host, port, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _mock_getaddrinfo_for_test_hosts() -> Generator[None, None, None]:
    """Patch ``socket.getaddrinfo`` for the SSRF guard so test HTTPS URLs
    resolve to a public IP without real DNS, and the guard passes.

    Tests that need specific IP outcomes (private/metadata ranges) apply
    their own ``patch`` context inside the test body, which overrides
    this fixture's patch for that block.
    """
    with patch(
        "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
        side_effect=_getaddrinfo_for_tests,
    ):
        yield


def _by_op_id(rows: list[EndpointDescriptorProto]) -> dict[str, EndpointDescriptorProto]:
    return {r.op_id: r for r in rows}


def _mock_yaml_spec(
    router: respx.MockRouter,
    path: str,
    content: bytes,
) -> str:
    """Register ``content`` at ``_FIXTURE_HOST/<path>`` and return the URL."""
    url = f"{_FIXTURE_HOST}/{path}"
    router.get(url).mock(
        return_value=httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/yaml"},
        )
    )
    return url


def _mock_json_spec(
    router: respx.MockRouter,
    path: str,
    content: bytes,
) -> str:
    """Register ``content`` at ``_FIXTURE_HOST/<path>`` and return the URL."""
    url = f"{_FIXTURE_HOST}/{path}"
    router.get(url).mock(
        return_value=httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
        )
    )
    return url


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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url, spec_source="spec:petstore_30.yaml")
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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
    ops = _by_op_id(rows)
    assert ops["GET:/pets"].safety_level == "safe"
    assert ops["POST:/pets"].safety_level == "caution"
    assert ops["DELETE:/pets/{petId}"].safety_level == "dangerous"
    assert ops["PUT:/pets/{petId}/photos"].safety_level == "caution"
    assert ops["HEAD:/pets/{petId}/photos"].safety_level == "safe"


def test_parse_petstore_30_path_and_query_param_locations() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
    get_pet = _by_op_id(rows)["GET:/pets/{petId}"]
    props = get_pet.parameter_schema["properties"]
    assert props["petId"]["x-meho-param-loc"] == "path"
    assert "petId" in get_pet.parameter_schema["required"]


# -- RFC6570 operator-bearing path-param names (#2003 / #2066) --------------

# A minimal spec whose path parameter declares a leading RFC6570 reserved-
# expansion operator in its ``name`` -- the shape VCF Operations-for-Logs
# uses for ``/events/{+path}``. Built inline (not a fixture file) so the
# operator-bearing name is right next to the assertion that the parser keys
# the property on the *bare* name.
_PLUS_PATH_SPEC = yaml.safe_dump(
    {
        "openapi": "3.0.3",
        "info": {"title": "plus-path", "version": "1.0.0"},
        "paths": {
            "/v1/{+path}": {
                "get": {
                    "operationId": "readPath",
                    "parameters": [
                        {
                            "name": "+path",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
).encode()


def test_parse_operator_bearing_path_param_keyed_on_bare_name() -> None:
    """A ``{"name": "+path", "in": "path"}`` param is keyed on the bare ``path``.

    The ingest half of #2003 (#2066): the renderer strips the RFC6570
    operator from the ``{+path}`` template and looks the value up by ``path``,
    so the parser must register the JSON-Schema property under the same bare
    name -- not ``+path`` -- or the op is undispatchable.
    """
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "plus_path.yaml", _PLUS_PATH_SPEC)
        rows = parse_openapi(url)
    op = _by_op_id(rows)["GET:/v1/{+path}"]
    props = op.parameter_schema["properties"]
    assert isinstance(props, dict)
    # Keyed on the bare name -- the operator is gone from the property key.
    assert "path" in props
    assert "+path" not in props
    assert props["path"]["x-meho-param-loc"] == "path"
    # Required under the bare name too.
    assert "path" in op.parameter_schema["required"]
    assert "+path" not in op.parameter_schema["required"]
    # The template keeps the operator verbatim -- the renderer needs it to
    # select reserved expansion so the value's ``/`` stays literal.
    assert op.path == "/v1/{+path}"


def test_parse_path_param_without_operator_unchanged() -> None:
    """A plain ``{"name": "cluster"}`` path param keeps its name verbatim."""
    spec = yaml.safe_dump(
        {
            "openapi": "3.0.3",
            "info": {"title": "plain-path", "version": "1.0.0"},
            "paths": {
                "/v1/{cluster}": {
                    "get": {
                        "operationId": "readCluster",
                        "parameters": [
                            {
                                "name": "cluster",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    ).encode()
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "plain_path.yaml", spec)
        rows = parse_openapi(url)
    op = _by_op_id(rows)["GET:/v1/{cluster}"]
    props = op.parameter_schema["properties"]
    assert props["cluster"]["x-meho-param-loc"] == "path"
    assert "cluster" in op.parameter_schema["required"]


def test_parse_bare_and_operator_path_param_collision_raises() -> None:
    """A spec declaring both ``path`` and ``+path`` path params fails loudly.

    Both collapse onto the bare property key ``path``; silently keeping one
    would drop a parameter the renderer still needs, so ingest of that op
    raises :class:`InvalidSchemaError` rather than half-building it.
    """
    spec = yaml.safe_dump(
        {
            "openapi": "3.0.3",
            "info": {"title": "collision", "version": "1.0.0"},
            "paths": {
                "/v1/{path}/{+path}": {
                    "get": {
                        "operationId": "collide",
                        "parameters": [
                            {
                                "name": "path",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "+path",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    ).encode()
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "collision.yaml", spec)
        with pytest.raises(InvalidSchemaError, match="normalises to property key 'path'"):
            parse_openapi(url)


def test_parse_petstore_30_request_body_inlined_with_param_loc() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
    put = _by_op_id(rows)["PUT:/pets/{petId}/photos"]
    assert put.parameter_schema["properties"]["body"]["x-meho-param-loc"] == "body"
    # requestBody.required omitted → not required.
    assert "body" not in put.parameter_schema.get("required", [])


def test_parse_petstore_30_response_schema_extracted() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
    list_pets = _by_op_id(rows)["GET:/pets"]
    # PetPage was inlined; nested $ref to Pet preserved.
    assert list_pets.response_schema is not None
    assert list_pets.response_schema["type"] == "object"
    items_schema = list_pets.response_schema["properties"]["items"]
    assert items_schema["items"] == {"$ref": "#/components/schemas/Pet"}


def test_parse_petstore_30_204_response_yields_none() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
    delete = _by_op_id(rows)["DELETE:/pets/{petId}"]
    assert delete.response_schema is None


def test_parse_petstore_30_tags_include_spec_source() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url, spec_source="spec:petstore_30.yaml")
    list_pets = _by_op_id(rows)["GET:/pets"]
    assert "pets" in list_pets.tags
    assert "spec:petstore_30.yaml" in list_pets.tags


def test_parse_petstore_30_without_spec_source_omits_tag() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_30.yaml", PETSTORE_30.read_bytes())
        rows = parse_openapi(url)
    list_pets = _by_op_id(rows)["GET:/pets"]
    assert list_pets.tags == ["pets"]
    # No accidental injection of an empty string or None.
    assert "" not in list_pets.tags


def test_parse_petstore_31_yaml_wildcard_2xx_response() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "petstore_31.yaml", PETSTORE_31_YAML.read_bytes())
        rows = parse_openapi(url)
    ops = _by_op_id(rows)
    list_vets = ops["GET:/vets"]
    assert list_vets.response_schema is not None
    # Wildcard 2XX response picked up.
    assert list_vets.response_schema["type"] == "array"


def test_parse_petstore_31_json_route() -> None:
    with respx.mock(assert_all_called=False) as router:
        url = _mock_json_spec(router, "petstore_31.json", PETSTORE_31_JSON.read_bytes())
        rows = parse_openapi(url)
    assert {r.op_id for r in rows} == {"GET:/zoos"}
    assert rows[0].response_schema == {"type": "array", "items": {"type": "string"}}


# -- hand-authored minimal spec (#1533 / ci-07 on-ramp) --------------------


def test_parse_handauthored_minimal_spec_via_https() -> None:
    """A hand-authored minimal OpenAPI 3.x ingests via an ``https://`` URL.

    #1533 / ci-07: for a product whose vendor publishes no OpenAPI doc
    at all (VCF Fleet / vRSLCM, Hetzner Robot), the supported on-ramp is
    to author a minimal OpenAPI 3.x covering just the needed ops and
    serve it over https to ``meho connector ingest``. A hand-authored
    spec is identical to a downloaded one once it reaches the parser;
    this test is the worked example proving the end-to-end parse,
    matching the workflow documented in
    ``docs/cross-repo/connector-ingestion.md`` §"Product publishes no
    OpenAPI spec".

    The G0.16-T8 (#95) SSRF guard makes the backend https-only, so the
    hand-authored spec reaches the parser over the same vetted transport
    a downloaded one does.
    """
    content = HANDAUTHORED_MINIMAL_30.read_bytes()
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "handauthored_minimal.yaml", content)
        rows = parse_openapi(url, spec_source="spec:hetzner-robot.yaml")
    ops = _by_op_id(rows)

    assert set(ops) == {"GET:/server", "POST:/reset/{server_ip}"}

    list_servers = ops["GET:/server"]
    assert list_servers.summary == "List dedicated servers"
    assert list_servers.safety_level == "safe"
    # The hand-authored source tag threads through unchanged so an
    # operator can audit which spec a row came from in review.
    assert "spec:hetzner-robot.yaml" in list_servers.tags

    reset = ops["POST:/reset/{server_ip}"]
    # POST is a write → "caution" under the same heuristic a downloaded
    # spec gets; the path param is required.
    assert reset.safety_level == "caution"
    assert reset.parameter_schema["properties"]["server_ip"]["x-meho-param-loc"] == "path"
    assert "server_ip" in reset.parameter_schema["required"]


# -- spec_source merging ---------------------------------------------------


def test_spec_source_threads_distinctly_across_two_specs() -> None:
    """vCenter ingests vcenter.yaml + vi-json.yaml under one connector;
    rows from each spec must carry distinguishable ``spec:<source>`` tags."""
    with respx.mock(assert_all_called=False) as router:
        url_a = _mock_yaml_spec(router, "vcenter.yaml", PETSTORE_30.read_bytes())
        url_b = _mock_yaml_spec(router, "vi-json.yaml", PETSTORE_31_YAML.read_bytes())
        rows_a = parse_openapi(url_a, spec_source="spec:vcenter.yaml")
        rows_b = parse_openapi(url_b, spec_source="spec:vi-json.yaml")
    all_tags_a = {tag for row in rows_a for tag in row.tags}
    all_tags_b = {tag for row in rows_b for tag in row.tags}
    assert "spec:vcenter.yaml" in all_tags_a
    assert "spec:vi-json.yaml" in all_tags_b
    assert "spec:vcenter.yaml" not in all_tags_b


# -- failure modes ---------------------------------------------------------


def test_unsupported_swagger_2_raises() -> None:
    content = b"swagger: '2.0'\ninfo: {title: x, version: '1'}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "swagger.yaml", content)
        with pytest.raises(UnsupportedSpecError, match=r"Swagger 2\.0") as excinfo:
            parse_openapi(url)
    # The rejection must be *actionable* -- it names the conversion path
    # (swagger2openapi / converter.swagger.io) so a 2.0-only surface such
    # as Harbor 2.x can be onboarded without an opaque dead end (#1532).
    message = str(excinfo.value)
    assert "swagger2openapi" in message
    assert "converter.swagger.io" in message
    assert "'2.0'" in message


def test_unsupported_openapi_4_raises() -> None:
    content = b"openapi: '4.0.0'\ninfo: {title: x, version: '1'}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "v4.yaml", content)
        with pytest.raises(UnsupportedSpecError, match=r"4\.0\.0"):
            parse_openapi(url)


def test_invalid_spec_missing_paths_raises() -> None:
    content = b"openapi: '3.1.0'\ninfo: {title: x, version: '1'}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "broken.yaml", content)
        with pytest.raises(InvalidSpecError, match="no 'paths' key"):
            parse_openapi(url)


def test_invalid_spec_non_mapping_root_raises() -> None:
    content = b"- not\n- a\n- mapping\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "list.yaml", content)
        with pytest.raises(InvalidSpecError, match="must parse to a mapping"):
            parse_openapi(url)


def test_non_https_scheme_raises_invalid_spec() -> None:
    """Any non-https scheme (http, file://, bare path) raises InvalidSpecError.

    This is the top-level smoke test for the G0.16-T8 (#95) SSRF guard:
    even a scheme that looks harmless (``http``) is rejected because the
    guard cannot protect the transport and ``http`` is indistinguishable
    from a redirect-bypass target after a single 30x hop.
    """
    with pytest.raises(InvalidSpecError, match="https"):
        parse_openapi("http://example.test/spec.yaml")


def test_spec_response_over_size_cap_streams_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx body larger than ``_MAX_SPEC_BYTES`` raises ``InvalidSpecError``
    naming the size limit.

    Regression guard for the M1 streaming fix: the cap is enforced while the
    body streams in (``client.stream`` + ``iter_bytes``), so an oversized
    response aborts the read instead of being fully buffered into pod memory
    first.
    """
    monkeypatch.setattr("meho_backplane.operations.ingest.openapi._MAX_SPEC_BYTES", 1024)
    oversized = b"x" * 4096
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "oversized.yaml", oversized)
        with pytest.raises(InvalidSpecError, match="size limit"):
            parse_openapi(url)


def test_redirect_chain_over_cap_raises_invalid_spec() -> None:
    """A redirect chain longer than ``_MAX_REDIRECTS`` raises
    ``InvalidSpecError`` naming the redirect cap.

    Each hop's destination is re-validated by the SSRF guard (the autouse
    ``getaddrinfo`` patch keeps the test hosts public); the loop exhausts its
    hop budget before any 2xx body is served.
    """
    with respx.mock(assert_all_called=False) as router:
        for i in range(_MAX_REDIRECTS + 1):
            router.get(f"{_FIXTURE_HOST}/r{i}").mock(
                return_value=httpx.Response(302, headers={"location": f"{_FIXTURE_HOST}/r{i + 1}"})
            )
        with pytest.raises(InvalidSpecError, match="followed more than"):
            parse_openapi(f"{_FIXTURE_HOST}/r0")


def test_uploaded_content_skips_fetch_and_scheme_guard() -> None:
    """When the CLI uploads inline spec content for a docs:/file:// source,
    ``parse_openapi`` uses it verbatim -- no fetch, no scheme guard -- so a
    ``docs:`` / ``file://`` uri label parses without the https-only guard
    firing (#102: this is what keeps the local-spec on-ramp working under
    the #95 guard).
    """
    spec_text = HANDAUTHORED_MINIMAL_30.read_text()
    rows = parse_openapi("docs:hetzner-robot/spec.yaml", content=spec_text)
    assert rows, "uploaded content should parse to operations"
    assert read_spec_info_version("file:///irrelevant.yaml", content=spec_text) == "1.0"


def test_uploaded_content_over_size_cap_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uploaded content larger than ``_MAX_SPEC_BYTES`` raises
    ``InvalidSpecError`` naming the size limit -- the cap applies to the
    content channel exactly as it does to a fetched body.
    """
    monkeypatch.setattr("meho_backplane.operations.ingest.openapi._MAX_SPEC_BYTES", 16)
    with pytest.raises(InvalidSpecError, match="size limit"):
        parse_openapi("file:///x.yaml", content="x" * 64)


def test_relative_redirect_location_resolved_and_followed() -> None:
    """A 30x with a relative ``Location`` is resolved against the current URL
    (``urljoin``) so the https-only guard does not wrongly reject a valid
    relative redirect (M1).
    """
    spec = (
        b"openapi: 3.0.3\n"
        b"info: {title: t, version: '1'}\n"
        b"paths:\n  /x:\n    get:\n      responses:\n"
        b"        '200':\n          description: ok\n"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{_FIXTURE_HOST}/redir.yaml").mock(
            return_value=httpx.Response(302, headers={"location": "/real.yaml"})
        )
        router.get(f"{_FIXTURE_HOST}/real.yaml").mock(
            return_value=httpx.Response(
                200, content=spec, headers={"content-type": "application/yaml"}
            )
        )
        rows = parse_openapi(f"{_FIXTURE_HOST}/redir.yaml")
    assert rows, "relative redirect should be resolved and followed"


def test_malformed_yaml_bubbles_up() -> None:
    content = b"openapi: '3.0.3'\npaths:\n  /x:\n   get:\n   summary: oops\n  : bad\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "bad.yaml", content)
        with pytest.raises(yaml.YAMLError):
            parse_openapi(url)


def test_docs_scheme_rejected_unsupported_naming_scheme() -> None:
    """A bare ``docs:`` URI is a CLI-side shorthand the backend never
    resolves; it must reject with :exc:`UnsupportedSpecError` that names
    the scheme + the ``$CLAUDE_RDC_DOCS`` remedy, not fall through to a
    generic ``InvalidSpecError`` that reads like a missing file (#1535).

    The typed class matters beyond the message: the MCP ingest envelope
    (#1534) maps every ``SpecError`` sibling onto ``-32602`` with
    structured detail, so reusing ``UnsupportedSpecError`` here makes the
    docs-scheme rejection a clean agent-facing error instead of a bare
    ``-32603``.
    """
    with pytest.raises(UnsupportedSpecError, match=r"docs:") as excinfo:
        parse_openapi("docs:vcenter-9.0/vcenter.yaml")
    # The remedy must name the env var the CLI resolves against.
    assert "CLAUDE_RDC_DOCS" in str(excinfo.value)
    # Regression guard: it must NOT be the opaque file-read error.
    assert "could not read spec" not in str(excinfo.value)


def test_malformed_json_bubbles_up() -> None:
    content = b'{"openapi": "3.0.3", broken'
    with respx.mock(assert_all_called=False) as router:
        url = _mock_json_spec(router, "bad.json", content)
        with pytest.raises(json.JSONDecodeError):
            parse_openapi(url)


def test_external_ref_raises() -> None:
    content = b"""openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      parameters:
        - $ref: 'other.yaml#/components/schemas/X'
      responses:
        "200":
          description: ok
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "ext.yaml", content)
        with pytest.raises(UnsupportedSpecError, match="cross-document"):
            parse_openapi(url)


def test_component_parameter_ref_resolves() -> None:
    """``$ref: '#/components/parameters/<name>'`` resolves to the shared parameter.

    The pre-T11 contract rejected this with ``UnsupportedSpecError`` —
    every vi-json.yaml operation uses this shape so the rejection
    blocked the entire ~2,195-op spec. The new contract inlines the
    referenced parameter the same way it inlines schema refs.
    """
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "param_ref.yaml", content)
        rows = parse_openapi(url)
    assert len(rows) == 1
    properties = rows[0].parameter_schema["properties"]
    assert isinstance(properties, dict)
    assert "shared" in properties
    assert properties["shared"]["x-meho-param-loc"] == "query"
    assert properties["shared"]["type"] == "string"
    assert rows[0].parameter_schema["required"] == ["shared"]


def test_component_parameter_ref_missing_name_raises() -> None:
    """A ref to an unknown parameter component raises ``InvalidSchemaError``.

    Mirrors the existing schema-bucket missing-component behaviour;
    the symmetry keeps the parser failure mode predictable across
    both component kinds.
    """
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "param_ref_dangling.yaml", content)
        with pytest.raises(InvalidSchemaError, match="missing component"):
            parse_openapi(url)


def test_component_parameter_ref_drilldown_raises() -> None:
    """A ref into a parameter component's subpath raises ``InvalidSchemaError``.

    Symmetric with the schema-bucket drill-down rejection. The
    OpenAPI spec only declares fragment refs to *named* components.
    """
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "param_ref_drilldown.yaml", content)
        with pytest.raises(InvalidSchemaError, match="drill-down"):
            parse_openapi(url)


def test_unsupported_component_bucket_ref_raises() -> None:
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
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "headers_ref.yaml", content)
        with pytest.raises(UnsupportedSpecError, match="unsupported component bucket"):
            parse_openapi(url)


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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "parameter_refs_30.yaml", PARAMETER_REFS_30.read_bytes())
        rows = parse_openapi(url)
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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "response_refs_30.yaml", RESPONSE_REFS_30.read_bytes())
        rows = parse_openapi(url)
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
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(
            router, "request_body_refs_30.yaml", REQUEST_BODY_REFS_30.read_bytes()
        )
        rows = parse_openapi(url)
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


def test_parse_github_shaped_response_ref_with_inline_schema() -> None:
    """Tightest reproduction of the GitHub spec's `responses/accepted` shape.

    GitHub's spec defines ``components.responses.accepted`` with a
    bare ``schema: {type: object}`` inside ``content.application/json``
    — no nested ``$ref``. Locks the simplest case so future
    refactors don't lose it.
    """
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "gh_accepted.yaml", content)
        rows = parse_openapi(url)
    assert len(rows) == 1
    assert rows[0].response_schema == {"type": "object"}


def test_missing_response_ref_raises() -> None:
    """A ref to an unknown response component raises ``InvalidSchemaError``."""
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "dangling_response.yaml", content)
        with pytest.raises(InvalidSchemaError, match="missing component"):
            parse_openapi(url)


def test_missing_request_body_ref_raises() -> None:
    """A ref to an unknown requestBody component raises ``InvalidSchemaError``."""
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "dangling_requestbody.yaml", content)
        with pytest.raises(InvalidSchemaError, match="missing component"):
            parse_openapi(url)


def test_missing_schema_ref_raises() -> None:
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "dangling.yaml", content)
        with pytest.raises(InvalidSchemaError, match="missing component"):
            parse_openapi(url)


def test_non_list_tags_raises() -> None:
    """A spec with ``tags: "admin"`` would otherwise iterate as characters."""
    content = b"""openapi: '3.0.3'
info: {title: x, version: '1'}
paths:
  /x:
    get:
      tags: admin
      responses:
        "200":
          description: ok
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "bad_tags.yaml", content)
        with pytest.raises(InvalidSchemaError, match=r"tags must be a list"):
            parse_openapi(url)


def test_openapi_31_boolean_parameter_schema() -> None:
    """OpenAPI 3.1 allows ``schema: true`` / ``schema: false`` on parameters."""
    content = b"""openapi: '3.1.0'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "bool_param.yaml", content)
        rows = parse_openapi(url)
    props = rows[0].parameter_schema["properties"]
    assert isinstance(props, dict)
    # true → {} (matches anything)
    assert props["anything"] == {"x-meho-param-loc": "query"}
    # false → {"not": {}} (matches nothing) + the loc keyword
    assert props["nothing"] == {"not": {}, "x-meho-param-loc": "query"}


def test_openapi_31_boolean_body_and_response_schema() -> None:
    """Boolean body / response schemas survive the parser pipeline."""
    content = b"""openapi: '3.1.0'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "bool_body.yaml", content)
        rows = parse_openapi(url)
    body = rows[0].parameter_schema["properties"]["body"]
    assert body == {"x-meho-param-loc": "body"}  # true → {} merged with loc keyword
    assert rows[0].response_schema == {"not": {}}  # false → {"not": {}}


def test_drilldown_schema_ref_raises() -> None:
    content = b"""openapi: '3.0.3'
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
"""
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "drilldown.yaml", content)
        with pytest.raises(InvalidSchemaError, match="drill-down"):
            parse_openapi(url)


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
    """``https://`` URLs are fetched via httpx."""
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


def test_http_fetch_application_yaml_content_type_accepted() -> None:
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


# -- SSRF / local-file guard (G0.16-T8, #95) --------------------------------


def test_ssrf_file_uri_rejected_before_any_read() -> None:
    """AC-3: ``file:///etc/passwd`` raises ``InvalidSpecError`` before any file read.

    The guard fires at the scheme check in ``_assert_fetchable_remote_url``
    before any network or filesystem activity. The exception message must
    not contain the path string so the response is not a filesystem oracle.
    """
    with pytest.raises(InvalidSpecError) as exc_info:
        parse_openapi("file:///etc/passwd")
    msg = str(exc_info.value)
    assert "/etc/passwd" not in msg
    assert "OSError" not in msg and "FileNotFoundError" not in msg


def test_ssrf_file_uri_rejected_via_read_spec_info_version() -> None:
    """AC-3 end-to-end: same guard fires through ``read_spec_info_version``."""
    with pytest.raises(InvalidSpecError) as exc_info:
        read_spec_info_version("file:///etc/passwd")
    msg = str(exc_info.value)
    assert "/etc/passwd" not in msg


def test_ssrf_bare_path_rejected() -> None:
    """A bare filesystem path (no scheme) is rejected as non-https."""
    with pytest.raises(InvalidSpecError, match="https"):
        parse_openapi("/etc/passwd")


def test_ssrf_http_scheme_rejected() -> None:
    """``http://`` is rejected; only ``https://`` is permitted on the ingest path."""
    with pytest.raises(InvalidSpecError, match="https"):
        parse_openapi("http://example.com/spec.yaml")


def test_ssrf_metadata_ip_rejected_before_connect() -> None:
    """AC-4: ``http://169.254.169.254/...`` is rejected before any socket connect.

    The guard resolves the hostname and checks every returned address
    against the private/link-local/reserved ranges before opening a
    connection. With the scheme check firing first (``http`` is not
    ``https``), the rejection happens even before DNS is attempted.
    This test also exercises the HTTPS variant by patching ``getaddrinfo``
    to return the metadata IP directly, proving the IP check fires.
    """
    # Scheme check fires first for ``http``.
    with pytest.raises(InvalidSpecError, match="https"):
        parse_openapi("http://169.254.169.254/latest/meta-data/")

    # For the HTTPS variant, mock getaddrinfo to return the metadata IP
    # so the IP-range check fires without a real DNS lookup.
    import socket

    with (
        patch(
            "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 443))],
        ),
        respx.mock(assert_all_called=False) as router,
    ):
        # Register a route but assert it is never called — the guard
        # must fire before the transport opens a connection.
        router.get("https://169.254.169.254/latest/meta-data/").mock(
            return_value=httpx.Response(200, content=b"", headers={"content-type": "text/plain"})
        )
        with pytest.raises(InvalidSpecError, match="non-public"):
            parse_openapi("https://169.254.169.254/latest/meta-data/")
        assert router.calls.call_count == 0


def test_ssrf_private_ip_rejected_before_connect() -> None:
    """AC-4 variant: RFC-1918 private IPs are rejected before any socket connect.

    Patches ``getaddrinfo`` to return a 10.x.x.x address so the IP
    check fires even for a hostname that looks public in the URL.
    """
    import socket

    with (
        patch(
            "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 443))],
        ),
        respx.mock(assert_all_called=False) as router,
    ):
        router.get("https://public-looking.example.test/spec.yaml").mock(
            return_value=httpx.Response(200, content=b"", headers={"content-type": "text/plain"})
        )
        with pytest.raises(InvalidSpecError, match="non-public"):
            parse_openapi("https://public-looking.example.test/spec.yaml")
        assert router.calls.call_count == 0


def test_ssrf_redirect_to_private_ip_rejected() -> None:
    """AC-5: a 30x redirect from an allowlisted public host to a private IP is rejected.

    The guard re-validates every redirect hop before following it.
    The private-target route must never be called.
    """
    import socket

    public_url = "https://public.example.test/spec.yaml"
    private_redirect_url = "https://10.0.0.5/spec.yaml"

    # First call (public host) resolves to a real public IP.
    public_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]
    # Second call (private redirect target) returns a private IP.
    private_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 443))]

    call_count = 0

    def _mock_getaddrinfo(hostname: str, port: object, **kwargs: object) -> list:
        nonlocal call_count
        call_count += 1
        if hostname == "public.example.test":
            return public_addrs
        return private_addrs

    with (
        patch(
            "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
            side_effect=_mock_getaddrinfo,
        ),
        respx.mock(assert_all_called=False) as router,
    ):
        router.get(public_url).mock(
            return_value=httpx.Response(302, headers={"location": private_redirect_url})
        )
        private_route = router.get(private_redirect_url).mock(
            return_value=httpx.Response(
                200,
                content=PETSTORE_31_YAML.read_bytes(),
                headers={"content-type": "application/yaml"},
            )
        )
        with pytest.raises(InvalidSpecError, match="non-public"):
            parse_openapi(public_url)
        # The private-target route must not have been called.
        assert not private_route.called


def test_ssrf_public_https_spec_fetch_succeeds() -> None:
    """AC-6: a ``https`` URL resolving to a public IP returns spec bytes unchanged.

    Proves the guard does not block legitimate catalog/CDN fetches.
    Patches ``getaddrinfo`` to return a stable public IP so the test
    is not sensitive to external DNS.
    """
    import socket

    url = "https://cdn.example.test/spec.yaml"
    with (
        patch(
            "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
        ),
        respx.mock(assert_all_called=False) as router,
    ):
        router.get(url).mock(
            return_value=httpx.Response(
                200,
                content=PETSTORE_31_YAML.read_bytes(),
                headers={"content-type": "application/yaml"},
            )
        )
        rows = parse_openapi(url)
    assert rows  # guard did not block; parsing succeeded


# -- _assert_fetchable_remote_url unit tests --------------------------------


def test_assert_fetchable_remote_url_rejects_http_scheme() -> None:
    with pytest.raises(InvalidSpecError, match="https"):
        _assert_fetchable_remote_url("http://example.com/spec.yaml")


def test_assert_fetchable_remote_url_rejects_file_scheme() -> None:
    with pytest.raises(InvalidSpecError, match="https"):
        _assert_fetchable_remote_url("file:///etc/passwd")


def test_assert_fetchable_remote_url_rejects_missing_hostname() -> None:
    with pytest.raises(InvalidSpecError, match="hostname"):
        _assert_fetchable_remote_url("https:///no-host/spec.yaml")


@pytest.mark.parametrize(
    "private_ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # RFC-1918
        "172.16.0.1",  # RFC-1918
        "192.168.1.1",  # RFC-1918
        "169.254.169.254",  # cloud metadata (link-local)
        "::1",  # IPv6 loopback
        "fc00::1",  # ULA (unique local)
        "fe80::1",  # IPv6 link-local
    ],
)
def test_assert_fetchable_remote_url_rejects_private_ips(private_ip: str) -> None:
    """Every address family in the private/reserved ranges is blocked."""
    import socket

    with (
        patch(
            "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", (private_ip, 443))],
        ),
        pytest.raises(InvalidSpecError, match="non-public"),
    ):
        _assert_fetchable_remote_url("https://any.example.test/spec.yaml")


def test_assert_fetchable_remote_url_accepts_public_ip() -> None:
    """A hostname resolving to a public IP passes the guard without error."""
    import socket

    with patch(
        "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
    ):
        # Should not raise.
        _assert_fetchable_remote_url("https://example.com/spec.yaml")


# -- read_spec_info_version ------------------------------------------------


def test_read_spec_info_version_returns_string() -> None:
    """Happy path — the spec's ``info.version`` is returned verbatim."""
    content = b"openapi: '3.0.3'\ninfo: {title: t, version: '9.0.3'}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "spec.yaml", content)
        assert read_spec_info_version(url) == "9.0.3"


def test_read_spec_info_version_missing_info_returns_none() -> None:
    """Specs without an ``info`` block return ``None`` — the cross-check skips."""
    content = b"openapi: '3.0.3'\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "spec.yaml", content)
        assert read_spec_info_version(url) is None


def test_read_spec_info_version_missing_version_field_returns_none() -> None:
    """``info`` present but no ``version`` field → ``None``."""
    content = b"openapi: '3.0.3'\ninfo: {title: t}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "spec.yaml", content)
        assert read_spec_info_version(url) is None


def test_read_spec_info_version_non_string_version_returns_none() -> None:
    """A non-string ``info.version`` (e.g. an integer) → ``None``."""
    content = b"openapi: '3.0.3'\ninfo: {title: t, version: 1}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "spec.yaml", content)
        assert read_spec_info_version(url) is None


def test_read_spec_info_version_empty_version_returns_none() -> None:
    """An empty ``info.version`` string is treated as missing."""
    content = b"openapi: '3.0.3'\ninfo: {title: t, version: ''}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "spec.yaml", content)
        assert read_spec_info_version(url) is None


def test_read_spec_info_version_rejects_swagger_2() -> None:
    """Swagger 2.0 specs surface the same gate :func:`parse_openapi` enforces.

    The cross-check helper shares ``_validate_openapi_version`` with the
    parser, so the same actionable conversion-path remedy reaches the
    operator on the spec-vs-label fast path (#1532).
    """
    content = b"swagger: '2.0'\ninfo: {title: t, version: '9.0.3'}\npaths: {}\n"
    with respx.mock(assert_all_called=False) as router:
        url = _mock_yaml_spec(router, "spec.yaml", content)
        with pytest.raises(UnsupportedSpecError, match=r"Swagger 2\.0") as excinfo:
            read_spec_info_version(url)
    assert "swagger2openapi" in str(excinfo.value)


def test_read_spec_info_version_non_https_scheme_raises_invalid_spec() -> None:
    """Non-https schemes raise ``InvalidSpecError`` from read_spec_info_version too."""
    with pytest.raises(InvalidSpecError, match="https"):
        read_spec_info_version("file:///tmp/nope.yaml")
