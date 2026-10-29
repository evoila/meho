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


@pytest.mark.parametrize(
    "sample",
    [
        '{"a": {"password": "' + _SECRET + '"}, "a": {}}',
        '{"b": {"a": {"password": "' + _SECRET + '"}, "a": {}}}',
        '[{"a": {"password": "' + _SECRET + '"}, "a": {}}]',
    ],
    ids=["top-level", "nested", "in-a-list"],
)
def test_json_text_value_that_is_json_with_a_repeated_key_is_withheld(sample: str) -> None:
    """The parser would keep one value and drop the other, so the whole value is withheld."""
    text, count = _check(json.dumps({"description": sample}), _JSON)
    assert json.loads(text) == {"description": REDACTED}
    assert count == 1
    assert _SECRET not in text


def test_json_text_value_holding_json_whose_own_text_value_repeats_a_key_is_withheld() -> None:
    """The rule holds at every depth: only the value with the repeated key is withheld."""
    inner = '{"a": {"password": "' + _SECRET + '"}, "a": {}}'
    text, count = _check(json.dumps({"description": json.dumps({"c": inner})}), _JSON)
    assert json.loads(json.loads(text)["description"]) == {"c": REDACTED}
    assert count == 1
    assert _SECRET not in text


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


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            f'<pre>{{<span class="k">"password"</span>: <span class="s">"{_SECRET}"</span>}}</pre>',
            f"<pre>{REDACTED}</pre>",
        ),
        (
            f'<pre><code class="json">{{\n  <span>"user"</span>: <span>"admin"</span>,\n'
            f'  <span>"secret"</span>: <span>"{_SECRET}"</span>\n}}</code></pre>',
            f"<pre>{REDACTED}</pre>",
        ),
        (
            f'<p>Body:</p><code>[<span>{{"token": "{_SECRET}"}}</span>]</code>',
            f"<p>Body:</p><code>{REDACTED}</code>",
        ),
        (
            f'<pre><span class="k">password</span>: <span class="s">{_SECRET}</span></pre>',
            f"<pre>{REDACTED}</pre>",
        ),
        (
            f'<pre>{{"<span>pass</span>word": "{_SECRET}"}}</pre>',
            f"<pre>{REDACTED}</pre>",
        ),
    ],
    ids=[
        "pre-with-spans",
        "pre-code-with-spans",
        "code-list-with-a-span",
        "yaml-with-spans",
        "secret-name-split-over-a-span",
    ],
)
def test_html_highlighted_block_with_a_secret_is_withheld(document: str, expected: str) -> None:
    """The text joined over the children is the document; a secret in it withholds the block."""
    text, count = _check(document, _HTML)
    assert text == expected
    assert count == 1


@pytest.mark.parametrize(
    "document",
    [
        '<pre>{<span class="k">"user"</span>: <span class="s">"admin"</span>}</pre>',
        '<pre><code class="json">{\n  <span>"user"</span>: <span>"admin"</span>\n}</code></pre>',
        '<p>Body:</p><code>[<span>{"user":"admin"}</span>]</code>',
        '<pre>{<span>"example"</span>:"{\\"x\\":{\\"y\\":1},\\"z\\":2}"}</pre>',
        "<pre><span>[</span>1<span>]</span></pre>",
        '<pre><span class="k">user</span>: <span class="s">admin</span>\nroles: [reader]</pre>',
        "<pre><br></pre>",
    ],
    ids=[
        "json-with-spans",
        "pre-code-with-spans",
        "code-list-with-a-span",
        "json-with-an-embedded-json-string",
        "json-list-of-numbers",
        "yaml",
        "no-text",
    ],
)
def test_html_highlighted_block_without_a_secret_comes_back_as_it_was(document: str) -> None:
    text, count = _check(document, _HTML)
    assert text == document
    assert count == 0


@pytest.mark.parametrize(
    "document",
    [
        '<pre><span class="p">$</span> meho connector list</pre>',
        '<pre><span class="kw">GET</span> /api/v2/hosts\nAccept: application/json</pre>',
        '<pre><span class="n">1</span>, 2, 3</pre>',
    ],
    ids=["shell-line", "request-with-a-header", "list-of-words"],
)
def test_html_highlighted_block_that_is_no_document_is_withheld(document: str) -> None:
    """A block split over child elements must be a JSON or YAML object or list."""
    text, count = _check(document, _HTML)
    assert text == f"<pre>{REDACTED}</pre>"
    assert count == 1


@pytest.mark.parametrize("tag", ["pre", "code", "span"])
def test_html_example_with_a_request_line_before_the_json_is_checked(tag: str) -> None:
    """The request line and the body are one text; the body is not JSON on its own."""
    example = f'POST /api/v2/sessions\n{{"username": "admin", "password": "{_SECRET}"}}'
    text, count = _check(f"<{tag}>{example}</{tag}>", _HTML)
    checked = f'POST /api/v2/sessions\n{{"username":"admin","password":"{REDACTED}"}}'
    assert text == f"<{tag}>{checked}</{tag}>"
    assert count == 1


def test_html_example_with_two_json_documents_checks_both() -> None:
    document = (
        f'<pre>Request:\n{{"username": "admin", "password": "{_SECRET}"}}\n'
        f'Response:\n{{"userId": "u1", "sessionId": "{_SECRET}"}}\n</pre>'
    )
    text, count = _check(document, _HTML)
    assert text == (
        f'<pre>Request:\n{{"username":"admin","password":"{REDACTED}"}}\n'
        f'Response:\n{{"userId":"u1","sessionId":"{REDACTED}"}}\n</pre>'
    )
    assert count == 2


def test_html_example_json_inside_text_that_is_not_json_as_a_whole_is_checked() -> None:
    text, count = _check(f'<pre>{{"body": {{"password": "{_SECRET}"}} ...}}</pre>', _HTML)
    assert text == f'<pre>{{"body": {{"password":"{REDACTED}"}} ...}}</pre>'
    assert count == 1


def test_html_example_with_brackets_that_open_no_json_is_unchanged() -> None:
    document = '<pre>GET /api/v2/hosts/{id}\n</pre><span>See <a href="/x">[1]</a> {{name}}</span>'
    text, count = _check(document, _HTML)
    assert text == document
    assert count == 0


@pytest.mark.parametrize(
    ("content_type", "document", "expected"),
    [
        (
            _JSON,
            '{"example":"POST /sessions\\n{\\"password\\":\\"' + _SECRET + '\\"}"}',
            '{"example":"POST /sessions\\n{\\"password\\":\\"' + REDACTED + '\\"}"}',
        ),
        (
            _XML,
            f'<doc><example>POST /sessions\n{{"password": "{_SECRET}"}}</example></doc>',
            f'<doc><example>POST /sessions\n{{"password":"{REDACTED}"}}</example></doc>',
        ),
        (
            _HTML,
            f'<p>Send POST /sessions with {{"password": "{_SECRET}"}} as the body</p>',
            f'<p>Send POST /sessions with {{"password":"{REDACTED}"}} as the body</p>',
        ),
        (
            _HTML,
            f'<pre>{{"example":"POST /sessions\\n{{\\"password\\":\\"{_SECRET}\\"}}"}}</pre>',
            f'<pre>{{"example":"POST /sessions\\n{{\\"password\\":\\"{REDACTED}\\"}}"}}</pre>',
        ),
    ],
    ids=["json-value", "xml-text", "html-prose", "html-pre-json-value"],
)
def test_json_after_a_request_line_inside_a_text_value_is_checked(
    content_type: str, document: str, expected: str
) -> None:
    """A request line before the body does not hide the body from the parser, in any text value."""
    text, count = _check(document, content_type)
    assert text == expected
    assert count == 1


def test_json_after_a_request_line_inside_a_yaml_block_value_is_checked() -> None:
    document = f'example: |\n  POST /sessions\n  {{"password": "{_SECRET}"}}\n'
    text, count = _check(document, _YAML)
    assert yaml.safe_load(text) == {"example": f'POST /sessions\n{{"password":"{REDACTED}"}}\n'}
    assert count == 1


@pytest.mark.parametrize(
    ("content_type", "document", "expected"),
    [
        (
            _JSON,
            '{"example":"POST /sessions\\n{\\"x\\":{\\"password\\":\\"'
            + _SECRET
            + '\\"},\\"x\\":1}"}',
            f'{{"example":"{REDACTED}"}}',
        ),
        (
            _HTML,
            '<pre>{<span>"example"</span>:"POST /sessions\\n{\\"password\\":\\"'
            + _SECRET
            + '\\"}"}</pre>',
            f"<pre>{REDACTED}</pre>",
        ),
        (
            _HTML,
            '<pre>{<span>"example"</span>:"POST /sessions\\n{\\"x\\":{\\"password\\":\\"'
            + _SECRET
            + '\\"},\\"x\\":1}"}</pre>',
            f"<pre>{REDACTED}</pre>",
        ),
    ],
    ids=[
        "repeated-key-after-a-request-line",
        "highlighted-block",
        "highlighted-block-repeated-key",
    ],
)
def test_json_after_a_request_line_that_cannot_come_back_is_withheld(
    content_type: str, document: str, expected: str
) -> None:
    text, count = _check(document, content_type)
    assert text == expected
    assert count == 1


@pytest.mark.parametrize("tag", ["pre", "code"])
@pytest.mark.parametrize(
    ("example", "expected", "replaced"),
    [
        (f"sessionId: {_SECRET}", f"sessionId: '{REDACTED}'", 1),
        (f"licenseKey: {_SECRET}", f"licenseKey: '{REDACTED}'", 1),
        (f"user: admin\npassword: {_SECRET}\n", f"user: admin\npassword: '{REDACTED}'\n", 1),
        (f"# login\nsessionId: {_SECRET}\n", f"sessionId: '{REDACTED}'\n", 1),
        (f"x:\n  sessionId: {_SECRET}\nx: 1", "x: 1", 1),
        (f"sessionId: &s {_SECRET}\nother: *s", f"sessionId: '{REDACTED}'\nother: '{REDACTED}'", 2),
        (
            f'user: admin\nbody: {{"sessionId": "{_SECRET}"}}',
            f"user: admin\nbody:\n  sessionId: '{REDACTED}'",
            1,
        ),
        (f"- user: admin\n  password: {_SECRET}", f"- user: admin\n  password: '{REDACTED}'", 1),
    ],
    ids=[
        "session-id",
        "licence-key",
        "two-keys",
        "comment",
        "repeated-key",
        "alias",
        "flow-mapping-value",
        "list",
    ],
)
def test_html_example_in_yaml_is_checked_like_a_yaml_file(
    tag: str, example: str, expected: str, replaced: int
) -> None:
    """A text-only block that is a YAML mapping or list comes back from the parsed copy.

    A comment is not in the copy. A key named twice keeps the last value;
    the dropped value was marked on the node graph and is counted, and it
    does not come back. An alias to a secret value is replaced too.
    """
    text, count = _check(f"<{tag}>{example}</{tag}>", _HTML)
    assert text == f"<{tag}>{expected}</{tag}>"
    assert count == replaced


def test_html_example_in_yaml_without_a_secret_comes_back_from_the_parsed_copy() -> None:
    text, count = _check("<pre>user: admin\nroles: [reader]\n</pre>", _HTML)
    assert text == "<pre>user: admin\nroles:\n- reader\n</pre>"
    assert count == 0


@pytest.mark.parametrize(
    "document",
    [
        "<pre>GET /api/v2/hosts\nAccept: application/json</pre>",
        "<pre>$ meho connector list</pre>",
        "<code>true</code>",
        "<span>user: admin</span>",
        "<p>sessionTimeout: 1800</p>",
    ],
    ids=["request-with-a-header", "shell-line", "scalar", "span-is-page-text", "prose"],
)
def test_html_text_that_is_no_yaml_example_stays_as_written(document: str) -> None:
    """Only a ``<pre>`` or ``<code>`` block that is a YAML mapping or list is an example in YAML."""
    text, count = _check(document, _HTML)
    assert text == document
    assert count == 0


@pytest.mark.parametrize(
    "document",
    [
        f'<pre><span id="password">{_SECRET}</span>{{"ok": true}}</pre>',
        f'<pre><span id="password">{{"password": "{_SECRET}"}}</span></pre>',
        f'<pre>{{"<span id="password">password</span>": "{_SECRET}"}}</pre>',
        f'<pre>{{"<span id="password">password</span>": "{_SECRET}", "ok": true}}</pre>',
        f'<pre>Token:\n<code name="token">{_SECRET}</code>\n{{"ok": true}}</pre>',
        f'<pre><b><password>{_SECRET}</password></b>\n[{{"ok": true}}]</pre>',
        f'<pre><span id="password">{_SECRET}</span> is the password</pre>',
        f'<pre><span data-password="{_SECRET}">{{"ok": true}}</span></pre>',
        f'<pre><script>var password = "{_SECRET}";</script>\n{{"ok": true}}</pre>',
        f'<pre><script type="application/json">{{"password": "{_SECRET}"}}</script></pre>',
    ],
    ids=[
        "secret-span-before-json",
        "secret-span-holding-json",
        "secret-span-as-the-json-key",
        "secret-span-as-one-json-key-of-two",
        "code-named-token",
        "secret-named-element-nested",
        "secret-span-in-prose",
        "secret-named-attribute",
        "nested-script-not-json",
        "nested-script-with-a-secret",
    ],
)
def test_html_block_with_a_secret_child_element_is_withheld(document: str) -> None:
    """A child whose own element check would replace anything withholds the block."""
    text, count = _check(document, _HTML)
    assert text == f"<pre>{REDACTED}</pre>"
    assert count == 1


@pytest.mark.parametrize(
    "document",
    [
        f'<pre>{{"x":{{"password":"{_SECRET}"}},"x":<span>1</span>}}</pre>',
        f'<pre>{{"a":{{"x":{{"password":"{_SECRET}"}},"x":<span>1</span>}}}}</pre>',
        f'<pre>[{{"x":{{"password":"{_SECRET}"}},"x":<span>1</span>}}]</pre>',
        f'<pre>{{"x":{{"password":"{_SECRET}"}},<span>"x"</span>:1}}</pre>',
        f"<pre><span>x</span>:\n  password: {_SECRET}\nx: 1</pre>",
        f"<pre><span>a</span>:\n  x:\n    password: {_SECRET}\n  x: 1</pre>",
        f"<pre><span>a</span>: {{x: {{password: {_SECRET}}}, x: 1}}</pre>",
        f"<pre><span>yes</span>:\n  password: {_SECRET}\ntrue: 1</pre>",
        f"<pre><span>user</span>: admin\n# password: {_SECRET}</pre>",
        '<pre>{"example":"{\\"x\\":{\\"password\\":\\"'
        + _SECRET
        + '\\"},\\"x\\":<span>1</span>}"}</pre>',
        '<pre>{<span>"a"</span>:'
        + json.dumps(json.dumps({"b": '{"x":{"password":"' + _SECRET + '"},"x":1}'}))
        + "}</pre>",
        f'<pre><span>a</span>: \'{{"x":{{"password":"{_SECRET}"}},"x":1}}\'</pre>',
    ],
    ids=[
        "json-repeated-key",
        "json-repeated-key-nested",
        "json-repeated-key-in-a-list",
        "json-repeated-key-in-a-span",
        "yaml-repeated-key",
        "yaml-repeated-key-nested",
        "yaml-repeated-key-in-a-flow-mapping",
        "yaml-repeated-key-spelled-two-ways",
        "yaml-comment",
        "json-repeated-key-in-an-embedded-json-string",
        "json-repeated-key-in-a-string-two-levels-down",
        "yaml-string-holding-json-with-a-repeated-key",
    ],
)
def test_html_block_whose_text_the_parser_keeps_only_in_part_is_withheld(document: str) -> None:
    """The block comes back as the page wrote it, so the parsed copy must hold all of its text.

    A key named twice keeps the last value only: the first value is in the
    text but not in the checked copy. A YAML comment is not in the copy at
    all. Such text is no document; the block is withheld. A text value in
    the document that is itself JSON with a key named twice is withheld
    and counted by the value check, so the block is withheld as well.
    """
    assert _SECRET in document
    text, count = _check(document, _HTML)
    assert text == f"<pre>{REDACTED}</pre>"
    assert count == 1


@pytest.mark.parametrize(
    "document",
    [
        '<pre>{"x":{"y":1},"z":{<span>"y"</span>:2}}</pre>',
        "<pre><span>x</span>:\n  y: 1\nz:\n  y: 2</pre>",
        "<pre><span>a</span>: {y: 1}\nb: {y: 2}</pre>",
    ],
    ids=["json", "yaml", "yaml-flow-mappings"],
)
def test_html_block_with_the_same_key_under_two_parents_comes_back_as_it_was(
    document: str,
) -> None:
    text, count = _check(document, _HTML)
    assert text == document
    assert count == 0


def test_html_example_the_parser_cannot_finish_is_withheld() -> None:
    """Brackets that open over and over and never close would cost a read per bracket."""
    text, count = _check("<pre>" + "[1,1,1,1," * 2000 + "</pre>", _HTML)
    assert text == f"<pre>{REDACTED}</pre>"
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


# A session id is a login token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "content_type", "expected"),
    [
        (
            f'{{"sessionId":"{_SECRET}","ttl":1800}}',
            _JSON,
            f'{{"sessionId":"{REDACTED}","ttl":1800}}',
        ),
        (f"session_id: {_SECRET}\n", _YAML, f"session_id: '{REDACTED}'\n"),
        (
            f"<X-Session-Id>{_SECRET}</X-Session-Id>",
            _XML,
            f"<X-Session-Id>{REDACTED}</X-Session-Id>",
        ),
        (f"<sessionId>{_SECRET}</sessionId>", _HTML, f"<sessionid>{REDACTED}</sessionid>"),
    ],
    ids=["json", "yaml", "xml", "html"],
)
def test_session_id_value_is_replaced_in_every_format(
    document: str, content_type: str, expected: str
) -> None:
    """Some appliances use the session id as the login token, so its value goes."""
    assert _check(document, content_type) == (expected, 1)
    assert scrub_query_string(f"/x?JSESSIONID={_SECRET}") == (f"/x?JSESSIONID={REDACTED}", 1)


def test_a_name_that_only_starts_with_session_is_not_a_secret() -> None:
    assert _check('{"sessionTimeout":1800,"session_id_format":"uuid"}', _JSON) == (
        '{"sessionTimeout":1800,"session_id_format":"uuid"}',
        0,
    )


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


# ---------------------------------------------------------------------------
# The caller's text rule
# ---------------------------------------------------------------------------


def _mark_rule(value: str) -> tuple[str, int]:
    """A text rule for the tests: a value that holds ``TOKEN`` becomes ``[MARK]``."""
    if "TOKEN" not in value:
        return value, 0
    return value.replace("TOKEN", "[MARK]"), value.count("TOKEN")


def test_text_rule_runs_on_parsed_values_and_the_yaml_keeps_its_types() -> None:
    document = (
        "info:\n"
        "  note: Bearer TOKEN\n"
        '  description: "line one\\nAuthorization: TOKEN"\n'
        "  count: 3\n"
        "  flag: true\n"
        "responses:\n"
        "  200:\n"
        "    description: TOKEN ok\n"
    )
    checked = check_api_file(document, content_type=_YAML, path="/x", text_rule=_mark_rule)
    assert checked.secrets_replaced == 3
    assert "TOKEN" not in checked.text
    assert yaml.safe_load(checked.text) == {
        "info": {
            "note": "Bearer [MARK]",
            "description": "line one\nAuthorization: [MARK]",
            "count": 3,
            "flag": True,
        },
        "responses": {200: {"description": "[MARK] ok"}},
    }


def test_text_rule_does_not_run_on_a_value_replaced_whole() -> None:
    document = f"password: TOKEN\nkey: |\n  {_pem(indent='  ')}\n  TOKEN\nnote: TOKEN\n"
    checked = check_api_file(document, content_type=_YAML, path="/x", text_rule=_mark_rule)
    assert yaml.safe_load(checked.text) == {
        "password": REDACTED,
        "key": REDACTED,
        "note": "[MARK]",
    }
    assert checked.secrets_replaced == 3


def test_text_rule_runs_inside_json_held_in_a_text_value() -> None:
    document = json.dumps({"example": '{"header": "Bearer TOKEN", "n": 1}', "plain": "TOKEN"})
    checked = check_api_file(document, content_type=_JSON, path="/x", text_rule=_mark_rule)
    assert json.loads(checked.text) == {
        "example": '{"header":"Bearer [MARK]","n":1}',
        "plain": "[MARK]",
    }
    assert checked.secrets_replaced == 2


def test_text_rule_runs_on_xml_and_html_text_and_attributes() -> None:
    xml = check_api_file(
        '<a note="TOKEN &amp; more"><b>Bearer TOKEN</b></a>',
        content_type=_XML,
        path="/x",
        text_rule=_mark_rule,
    )
    assert xml.text == '<a note="[MARK] &amp; more"><b>Bearer [MARK]</b></a>'
    assert xml.secrets_replaced == 2
    html = check_api_file(
        '<p title="TOKEN">Bearer TOKEN &lt; 1</p>',
        content_type=_HTML,
        path="/x",
        text_rule=_mark_rule,
    )
    assert html.text == '<p title="[MARK]">Bearer [MARK] &lt; 1</p>'
    assert html.secrets_replaced == 2


def test_without_a_text_rule_text_values_come_back_as_they_are() -> None:
    text, count = _check("note: Bearer TOKEN\n", _YAML)
    assert (text, count) == ("note: Bearer TOKEN\n", 0)
