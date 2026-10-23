# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared API file secret check (``_shared/api_file_secrets.py``).

Every trick the code reviews of the first API file reads found is a test
here. Each one shows that the secret does not come back: the placeholder
stands in its place, or the file is refused. All values are made up.
"""

from __future__ import annotations

import json

import pytest
import yaml

from meho_backplane.connectors._shared.api_file_secrets import (
    REDACTED,
    ApiFileFormatError,
    check_api_file,
    detect_format,
    scrub_query_string,
)

_SECRET = "madeup-value-1234"
_JSON = "application/json"
_YAML = "application/yaml"
_XML = "application/xml"
_HTML = "text/html"


def _pem(kind: str = "", indent: str = "") -> str:
    """A made-up private key in PEM form.

    The header is built from parts so that secret scanners do not flag
    this test file as a key.
    """
    label = f"{kind} PRIVATE KEY-----" if kind else "PRIVATE KEY-----"
    return f"-----BEGIN {label}\n{indent}{_SECRET}\n{indent}-----END {label}"


def _check(text: str, content_type: str | None = None, path: str = "/x") -> tuple[str, int]:
    checked = check_api_file(text, content_type=content_type, path=path)
    assert _SECRET not in checked.text
    return checked.text, checked.secrets_replaced


# ---------------------------------------------------------------------------
# Picking the parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "path", "expected"),
    [
        ("application/json", "/x", "json"),
        ("application/vnd.oai.openapi+json; charset=utf-8", "/x", "json"),
        ("application/yaml", "/x", "yaml"),
        ("application/x-yaml", "/x", "yaml"),
        ("text/yaml", "/x", "yaml"),
        ("application/vnd.oai.openapi+yaml", "/x", "yaml"),
        ("text/xml; charset=UTF-8", "/x", "xml"),
        ("application/xml", "/x", "xml"),
        ("application/wsdl+xml", "/x", "xml"),
        ("TEXT/HTML; charset=utf-8", "/x", "html"),
        ("text/plain", "/api/openapi.json?group=a", "json"),
        ("text/plain", "/api/spec.yaml", "yaml"),
        ("text/plain", "/api/spec.YML", "yaml"),
        (None, "/sso/admin.wsdl", "xml"),
        ("", "/sso/types.xsd", "xml"),
        (None, "/docs/index.html", "html"),
        (None, "/docs/index.htm", "html"),
        ("text/plain", "/docs", None),
        ("text/plain", "/docs/readme.txt", None),
        ("application/javascript", "/docs/app.js", None),
        ("text/css", "/docs/app.css", None),
        ("application/octet-stream", "/docs/openapi.json", None),
    ],
)
def test_detect_format_uses_the_content_type_then_the_file_ending(
    content_type: str | None, path: str, expected: str | None
) -> None:
    assert detect_format(content_type, path) == expected


def test_javascript_and_css_files_are_refused() -> None:
    for content_type, path in (("application/javascript", "/app.js"), ("text/css", "/a.css")):
        with pytest.raises(ApiFileFormatError, match="no known format"):
            check_api_file(f'var password = "{_SECRET}";', content_type=content_type, path=path)


def test_plain_text_without_a_known_ending_is_never_read_as_yaml() -> None:
    with pytest.raises(ApiFileFormatError, match="no known format"):
        check_api_file(f"password: {_SECRET}", content_type="text/plain", path="/docs")


def test_a_file_that_does_not_parse_is_refused_without_its_text() -> None:
    cases = [
        ('{"password": "' + _SECRET, _JSON, "not well-formed JSON"),
        (f"password: [{_SECRET}\n", _YAML, "not well-formed YAML"),
        (f"x: !{_SECRET} 1\n", _YAML, r"YAML \(ConstructorError at line 1, column 4\)"),
        (f"x: !!int {_SECRET}\n", _YAML, r"YAML \(ValueError\)"),
        (f"x: !!float {_SECRET}\n", _YAML, r"YAML \(ValueError\)"),
        (f"x: !!timestamp {_SECRET}\n", _YAML, r"YAML \(\w+Error\)"),
        (f"x: !!bool {_SECRET}\n", _YAML, r"YAML \(KeyError\)"),
        (f"<a><password>{_SECRET}</a>", _XML, "not well-formed XML"),
        (f'<!DOCTYPE a [<!ENTITY x "{_SECRET}">]><a>&x;</a>', _XML, "refuses to parse"),
    ]
    for text, content_type, reason in cases:
        with pytest.raises(ApiFileFormatError, match=reason) as excinfo:
            check_api_file(text, content_type=content_type, path="/x")
        assert _SECRET not in str(excinfo.value)


def test_an_empty_file_comes_back_empty_whatever_its_type() -> None:
    assert _check("", "application/javascript") == ("", 0)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_clean_openapi_json_comes_back_with_zero_replacements() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1.0"},
        "paths": {"/items": {"get": {"summary": "List items", "responses": {"200": {}}}}},
    }
    text, count = _check(json.dumps(spec, indent=2), _JSON)
    assert count == 0
    assert json.loads(text) == spec


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"credentials": {"value": _SECRET}}, {"credentials": REDACTED}),
        ({"credentials": {"default": _SECRET}}, {"credentials": REDACTED}),
        ({"licenseKey": [_SECRET]}, {"licenseKey": REDACTED}),
        ({"example": {"password": "abc"}}, {"example": {"password": REDACTED}}),
        ({"items": [{"a": 1}, {"token": _SECRET}]}, {"items": [{"a": 1}, {"token": REDACTED}]}),
        ({"password": "ab"}, {"password": REDACTED}),
        ({"password": "a b c"}, {"password": REDACTED}),
        ({"password": "it's"}, {"password": REDACTED}),
        ({"password": 12345678}, {"password": REDACTED}),
        ({"password": None}, {"password": REDACTED}),
        ({"properties": {"password": {"type": "string"}}}, {"properties": {"password": REDACTED}}),
    ],
)
def test_json_value_under_a_secret_name_is_replaced_at_any_depth(
    document: dict[str, object], expected: dict[str, object]
) -> None:
    text, count = _check(json.dumps(document), _JSON)
    assert json.loads(text) == expected
    assert count == 1


def test_json_escaped_key_is_read_as_the_key_it_spells() -> None:
    text, count = _check('{"p\\u0061ssword": "' + _SECRET + '"}', _JSON)
    assert json.loads(text) == {"password": REDACTED}
    assert count == 1


def test_json_text_value_that_is_itself_json_is_checked() -> None:
    sample = json.dumps({"user": "admin", "password": _SECRET})
    text, count = _check(json.dumps({"description": sample}), _JSON)
    assert json.loads(json.loads(text)["description"]) == {"user": "admin", "password": REDACTED}
    assert count == 1


def test_json_text_value_that_is_json_with_a_repeated_key_comes_back_parsed() -> None:
    sample = '{"a": {"password": "' + _SECRET + '"}, "a": {}}'
    text, count = _check(json.dumps({"description": sample}), _JSON)
    assert json.loads(text) == {"description": '{"a":{}}'}
    assert count == 0


def test_json_pem_private_key_under_any_name_is_replaced() -> None:
    pem = _pem("RSA")
    text, count = _check(json.dumps({"example": pem}), _JSON)
    assert json.loads(text) == {"example": REDACTED}
    assert count == 1


def test_json_repeated_keys_and_formatting_do_not_come_back() -> None:
    text, count = _check('{"a": 1, "a": 2,\n\n "b": [1,\n 2]}', _JSON)
    assert text == '{"a":2,"b":[1,2]}'
    assert count == 0


def test_json_count_adds_up_over_the_whole_file() -> None:
    document = {"a": {"password": "x"}, "b": [{"token": "y"}, {"apiKey": "z"}], "c": {"d": 1}}
    _, count = _check(json.dumps(document), _JSON)
    assert count == 3


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


def test_clean_openapi_yaml_comes_back_with_zero_replacements() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1.0"},
        "paths": {"/items": {"get": {"summary": "List items"}}},
    }
    text, count = _check(yaml.safe_dump(spec, sort_keys=False), _YAML)
    assert count == 0
    assert yaml.safe_load(text) == spec


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(f"password: # note\n  {_SECRET}\n", id="value-on-the-next-line"),
        pytest.param(f"password:\n  # one\n  # two\n  {_SECRET}\n", id="after-two-comment-lines"),
        pytest.param(f"password: |\n  {_SECRET}\n", id="block-scalar"),
        pytest.param(f"password: >\n  {_SECRET}\n", id="folded-scalar"),
        pytest.param(f"password: !!str {_SECRET}\n", id="tag"),
        pytest.param(f"password: made\n  {_SECRET}\n", id="value-over-two-lines"),
        pytest.param(f"password: null {_SECRET}\n", id="null-then-value"),
        pytest.param(f"password: made,{_SECRET}\n", id="comma"),
        pytest.param(f'password: "{_SECRET}"\n', id="double-quoted"),
        pytest.param(f"password: '{_SECRET}'\n", id="single-quoted"),
        pytest.param(f"password: ' {_SECRET} '\n", id="spaces-inside"),
        pytest.param(f"password: it's-{_SECRET}\n", id="apostrophe"),
        pytest.param("password: ab\n", id="short"),
        pytest.param(f"{{password: {_SECRET}}}\n", id="flow-mapping"),
    ],
)
def test_yaml_value_under_a_secret_name_is_replaced_however_written(document: str) -> None:
    text, count = _check(document, _YAML)
    assert yaml.safe_load(text) == {"password": REDACTED}
    assert count == 1


def test_yaml_list_with_the_dash_at_the_key_indentation_is_replaced() -> None:
    text, count = _check(f"licenseKey:\n- {_SECRET}\n- other\n", _YAML)
    assert yaml.safe_load(text) == {"licenseKey": REDACTED}
    assert count == 1


def test_yaml_secret_inside_an_object_or_a_list_is_replaced() -> None:
    document = (
        f"example: {{password: {_SECRET}}}\n"
        f"credentials:\n  value: {_SECRET}\n"
        f"servers:\n  - url: https://example.invalid\n    token: {_SECRET}\n"
    )
    text, count = _check(document, _YAML)
    assert yaml.safe_load(text) == {
        "example": {"password": REDACTED},
        "credentials": REDACTED,
        "servers": [{"url": "https://example.invalid", "token": REDACTED}],
    }
    assert count == 3


def test_yaml_alias_to_a_secret_value_is_replaced_at_the_anchor_too() -> None:
    text, count = _check(f"defaults: &pw {_SECRET}\npassword: *pw\n", _YAML)
    assert yaml.safe_load(text) == {"defaults": REDACTED, "password": REDACTED}
    assert count == 2


def test_yaml_anchor_on_the_secret_value_is_replaced_at_every_alias() -> None:
    text, count = _check(f"password: &pw {_SECRET}\nhint: *pw\nnested:\n  again: *pw\n", _YAML)
    assert yaml.safe_load(text) == {
        "password": REDACTED,
        "hint": REDACTED,
        "nested": {"again": REDACTED},
    }
    assert count == 3


def test_yaml_merge_key_cannot_bring_the_secret_back() -> None:
    text, count = _check(f"base: &b\n  password: {_SECRET}\nderived:\n  <<: *b\n", _YAML)
    assert yaml.safe_load(text) == {
        "base": {"password": REDACTED},
        "derived": {"password": REDACTED},
    }
    assert count == 1


def test_yaml_comment_does_not_come_back() -> None:
    text, count = _check(f"a: 1 # the password is {_SECRET}\nb: 2\n", _YAML)
    assert text == "a: 1\nb: 2\n"
    assert count == 0


def test_yaml_text_value_that_is_json_and_pem_key_are_checked() -> None:
    document = 'sample: \'{"password": "' + _SECRET + f"\"}}'\ncert: |\n  {_pem(indent='  ')}\n"
    text, count = _check(document, _YAML)
    loaded = yaml.safe_load(text)
    assert json.loads(loaded["sample"]) == {"password": REDACTED}
    assert loaded["cert"] == REDACTED
    assert count == 2


def test_yaml_empty_document_comes_back_empty() -> None:
    assert _check("# only a comment\n", _YAML) == ("", 0)


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (f"<r><password>{_SECRET}</password></r>", f"<r><password>{REDACTED}</password></r>"),
        (
            f"<r><password>made<!-- note -->{_SECRET}</password></r>",
            f"<r><password>{REDACTED}</password></r>",
        ),
        (
            f"<r><password>made<b>{_SECRET}</b>up</password></r>",
            f"<r><password>{REDACTED}</password></r>",
        ),
        (f'<r><a password="{_SECRET}"/></r>', f'<r><a password="{REDACTED}"/></r>'),
        (
            f'<r><property name="password" value="{_SECRET}"/></r>',
            f'<r><property name="password" value="{REDACTED}"/></r>',
        ),
        (
            f'<r><entry key="licenseKey">{_SECRET}</entry></r>',
            f'<r><entry key="licenseKey">{REDACTED}</entry></r>',
        ),
        (f"<r><token>{_SECRET}</token></r>", f"<r><token>{REDACTED}</token></r>"),
    ],
)
def test_xml_secret_element_or_attribute_is_replaced(document: str, expected: str) -> None:
    text, count = _check(document, _XML)
    assert text == expected
    assert count == 1


def test_xml_comment_and_cdata_markers_do_not_come_back() -> None:
    text, count = _check(f"<r><!-- password {_SECRET} --><a><![CDATA[1 < 2]]></a></r>", _XML)
    assert text == "<r><a>1 &lt; 2</a></r>"
    assert count == 0


def test_xml_text_that_is_json_or_a_pem_key_is_checked() -> None:
    document = f'<r><sample>{{"password": "{_SECRET}"}}</sample><cert>{_pem("EC")}</cert></r>'
    text, count = _check(document, _XML)
    assert text == f'<r><sample>{{"password":"{REDACTED}"}}</sample><cert>{REDACTED}</cert></r>'
    assert count == 2


_WSDL = """<?xml version="1.0" encoding="UTF-8"?>
<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:tns="urn:example:admin" targetNamespace="urn:example:admin">
  <wsdl:import namespace="urn:example:admin:messages" location="admin-messages.wsdl"/>
  <wsdl:types>
    <xsd:schema targetNamespace="urn:example:admin">
      <xsd:element name="password" type="xsd:string"/>
      <xsd:element name="user" type="tns:User" minOccurs="0"/>
    </xsd:schema>
  </wsdl:types>
</wsdl:definitions>
"""


def test_wsdl_keeps_its_namespace_prefixes_and_schema_elements() -> None:
    text, count = _check(_WSDL, "text/xml")
    assert count == 1
    assert text.startswith(
        '<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:tns="urn:example:admin" '
        'targetNamespace="urn:example:admin">'
    )
    assert f'<xsd:element name="password" type="{REDACTED}"/>' in text
    assert '<xsd:element name="user" type="tns:User" minOccurs="0"/>' in text
    assert "<?xml" not in text


def test_xml_default_namespace_is_declared_where_it_starts_and_ends() -> None:
    document = '<a xmlns="urn:one"><b xmlns=""><c/></b><d xml:lang="en">t</d></a>'
    text, count = _check(document, _XML)
    assert text == document
    assert count == 0


def test_xml_prefix_bound_again_in_a_child_keeps_its_meaning_there() -> None:
    document = '<a xmlns:p="urn:one"><p:x/><b xmlns:p="urn:two"><p:y type="p:t"/></b><p:z/></a>'
    text, count = _check(document, _XML)
    assert text == document
    assert count == 0


def test_xml_attribute_prefix_declared_on_a_child_is_declared_there() -> None:
    document = '<root xmlns="urn:one"><child xmlns:p="urn:one" p:attr="1"/></root>'
    text, count = _check(document, _XML)
    assert text == document
    assert count == 0


def test_xml_element_named_secret_by_its_type_attribute_loses_its_other_values() -> None:
    document = f'<r><field type="password" label="Secret" value="{_SECRET}">{_SECRET}</field></r>'
    text, count = _check(document, _XML)
    assert text == (
        f'<r><field type="password" label="{REDACTED}" value="{REDACTED}">{REDACTED}</field></r>'
    )
    assert count == 3


@pytest.mark.parametrize(
    ("document", "expected", "count"),
    [
        pytest.param(
            f'<r><field name="password" id="abc" value="{_SECRET}"/></r>',
            f'<r><field name="password" id="{REDACTED}" value="{REDACTED}"/></r>',
            2,
            id="id-after-name",
        ),
        pytest.param(
            f'<r><field id="{_SECRET}" name="password"/></r>',
            f'<r><field id="{REDACTED}" name="password"/></r>',
            1,
            id="id-before-name",
        ),
        pytest.param(
            f'<r><field type="password" name="api_key" key="{_SECRET}"/></r>',
            f'<r><field type="password" name="api_key" key="{REDACTED}"/></r>',
            1,
            id="two-labels",
        ),
        pytest.param(
            f'<r><password name="{_SECRET}" type="string"/></r>',
            f'<r><password name="{REDACTED}" type="{REDACTED}"/></r>',
            2,
            id="secret-named-element",
        ),
    ],
)
def test_xml_only_the_label_of_a_secret_element_stays(
    document: str, expected: str, count: int
) -> None:
    text, replaced = _check(document, _XML)
    assert text == expected
    assert replaced == count


def test_xml_special_characters_are_written_back_escaped() -> None:
    document = '<a title="1 &gt; 0 &amp; &quot;q&quot;">a &lt; b &amp; c</a>'
    text, count = _check(document, _XML)
    assert text == '<a title="1 &gt; 0 &amp; &quot;q&quot;">a &lt; b &amp; c</a>'
    assert count == 0


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(f'<input name="password" value="{_SECRET}">', id="quoted"),
        pytest.param(f"<input name=password value={_SECRET}>", id="unquoted"),
        pytest.param(f'<INPUT NAME="Password" VALUE="{_SECRET}">', id="upper-case"),
        pytest.param(f'<input name="password" value="{_SECRET}" />', id="self-closing"),
        pytest.param(f'<form><input type="password" value="{_SECRET}"></form>', id="type"),
        pytest.param(f'<div password="{_SECRET}"></div>', id="attribute"),
        pytest.param(f'<meta name="csrf-token" content="{_SECRET}">', id="meta"),
        pytest.param(f'<a licenseKey="{_SECRET}"></a>', id="camel-case-attribute"),
        pytest.param(f'<input name="licenseKey" value="{_SECRET}">', id="camel-case-name"),
    ],
)
def test_html_secret_attribute_value_is_replaced(document: str) -> None:
    text, count = _check(document, _HTML)
    assert f'="{REDACTED}"' in text
    assert count == 1


@pytest.mark.parametrize(
    ("document", "expected", "count"),
    [
        pytest.param(
            f'<input id="login-field" name="password" value="{_SECRET}">',
            f'<input id="{REDACTED}" name="password" value="{REDACTED}">',
            2,
            id="after-id",
        ),
        pytest.param(
            f'<input name="password" id="{_SECRET}">',
            f'<input name="password" id="{REDACTED}">',
            1,
            id="only-id",
        ),
        pytest.param(
            f'<input name="password" name="normal" value="{_SECRET}">',
            f'<input name="password" name="{REDACTED}" value="{REDACTED}">',
            2,
            id="repeated",
        ),
        pytest.param(
            f'<input value="{_SECRET}" name="normal" name="password">',
            f'<input value="{REDACTED}" name="{REDACTED}" name="password">',
            2,
            id="repeated-last",
        ),
        pytest.param(
            f'<input type="password" name="api_key" id="{_SECRET}" disabled>',
            f'<input type="password" name="api_key" id="{REDACTED}" disabled>',
            1,
            id="two-labels",
        ),
        pytest.param(
            f'<password name="{_SECRET}" type="text">x</password>',
            f'<password name="{REDACTED}" type="{REDACTED}">{REDACTED}</password>',
            3,
            id="secret-named-element",
        ),
    ],
)
def test_html_only_the_label_of_a_secret_element_stays(
    document: str, expected: str, count: int
) -> None:
    text, replaced = _check(document, _HTML)
    assert text == expected
    assert replaced == count


def test_html_camel_case_element_is_secret_although_the_parser_lowercases_it() -> None:
    text, count = _check(
        f"<licenseKey>{_SECRET}</licenseKey><clientSecret>{_SECRET}</clientSecret>", _HTML
    )
    assert text == f"<licensekey>{REDACTED}</licensekey><clientsecret>{REDACTED}</clientsecret>"
    assert count == 2


def test_html_gt_inside_a_quoted_attribute_does_not_end_the_tag() -> None:
    text, count = _check(f'<input title="1 > 0" name="password" value="{_SECRET}">', _HTML)
    assert text == f'<input title="{REDACTED}" name="password" value="{REDACTED}">'
    assert count == 2


def test_html_secret_element_content_is_replaced() -> None:
    text, count = _check(f"<p>x</p><password>made <b>{_SECRET}</b></password>", _HTML)
    assert text == f"<p>x</p><password>{REDACTED}</password>"
    assert count == 1


def test_html_script_with_json_on_the_next_line_is_checked_as_json() -> None:
    document = f'<script type="application/json">\n{{"password":\n"{_SECRET}"}}\n</script>'
    text, count = _check(document, _HTML)
    assert text == f'<script type="application/json">{{"password":"{REDACTED}"}}</script>'
    assert count == 1


def test_html_script_that_is_not_json_does_not_come_back() -> None:
    document = f'<script>var password = "{_SECRET}";</script><script src="app.js"></script>'
    text, count = _check(document, _HTML)
    assert text == f'<script>{REDACTED}</script><script src="app.js"></script>'
    assert count == 1


def test_html_pre_block_with_json_is_checked() -> None:
    text, count = _check(f'<pre>{{"password": "{_SECRET}"}}</pre>', _HTML)
    assert text == f'<pre>{{"password":"{REDACTED}"}}</pre>'
    assert count == 1


def test_html_comment_does_not_come_back() -> None:
    text, count = _check(f"<p>a<!-- password: {_SECRET} --></p>", _HTML)
    assert text == "<p>a</p>"
    assert count == 0


def test_html_free_text_stays_as_written_and_escaped() -> None:
    document = "<!DOCTYPE html><html><body><p>a &amp; b &lt; c</p><br><hr/></body></html>"
    text, count = _check(document, _HTML)
    assert text == "<!DOCTYPE html><html><body><p>a &amp; b &lt; c</p><br><hr></body></html>"
    assert count == 0


def test_html_pem_private_key_in_text_is_replaced() -> None:
    document = f"<pre>{_pem()}</pre>"
    text, count = _check(document, _HTML)
    assert text == f"<pre>{REDACTED}</pre>"
    assert count == 1


# ---------------------------------------------------------------------------
# Redirect paths
# ---------------------------------------------------------------------------


def test_query_string_secret_is_replaced_also_with_an_encoded_name() -> None:
    assert scrub_query_string(f"/x?license%4Bey={_SECRET}&group=a") == (
        f"/x?licenseKey={REDACTED}&group=a",
        1,
    )
    assert scrub_query_string(f"/x?token={_SECRET}") == (f"/x?token={REDACTED}", 1)


def test_query_string_without_a_secret_comes_back_as_written() -> None:
    assert scrub_query_string("/x?group=a%20b&apiVersion=2") == ("/x?group=a%20b&apiVersion=2", 0)
    assert scrub_query_string("/x") == ("/x", 0)
