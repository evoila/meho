# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared whole-file secret scan of API description files.

Every secret here is made up. The refused cases are the ones the
independent reviewer found passing the earlier per-format checks of the
API file reads: comment lines before a YAML value, an alias with a
trailing comment, an XML element, an HTML attribute (quoted or bare), a
JSON value on the next line inside a script block, a value inside an
object or a list under a secret-named key, and the rest.
"""

from __future__ import annotations

import pytest

from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.connectors._shared.api_file_secrets import (
    REDACTED,
    find_api_file_secrets,
    scrub_api_file_secrets,
)

# Built in pieces so that the test file itself holds no key header.
_PEM_HEADER = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
_PEM_FOOTER = "-----END " + "RSA PRIVATE" + " KEY-----"

#: ``(text, how many values are replaced, the name of the first one)``.
#: The secret text is ``madeup`` or ``MADEUP`` in every case, so one
#: assertion proves it is gone.
_REFUSED = {
    # The reviewer's examples on the VCF Operations read.
    "json-quoted-password": ('{"openapi":"3.0.1","x-login":{"password": "madeup"}}', 1, "password"),
    "json-quoted-token": ('{"x-session":{"token":"madeup"}}', 1, "token"),
    "json-prefixed-key": ('{"x-login":{"x-auth-token": "madeup"}}', 1, "x-auth-token"),
    "json-authorization": ('{"x-headers":{"Authorization": "Basic madeup"}}', 1, "Authorization"),
    "json-licence-key": ('{"x-licence":{"licenseKey": "MADEUP-MADEUP"}}', 1, "licenseKey"),
    "yaml-licence-key": ("x-licence:\n  licence_key: MADEUP-MADEUP\n", 1, "licence_key"),
    "licence-key-in-running-text": (
        '{"info":{"description":"Send licenseKey: MADEUP-MADEUP-MADEUP"}}',
        1,
        "licenseKey",
    ),
    "short-password": ('{"password": "madeup"}', 1, "password"),
    "short-yaml-password": ("x-login:\n  password: madeup\n", 1, "password"),
    "password-of-words": ('{"password": "madeup made up of words"}', 1, "password"),
    "yaml-password-of-words-in-a-list": ("x:\n  - password: madeup of words\n", 1, "password"),
    "yaml-block-scalar": ("x-login:\n  password: |\n    madeup\n", 1, "password"),
    "yaml-folded-block-of-words": ("password: >-\n  madeup\n  words\n", 1, "password"),
    "yaml-block-with-comment": ("a:\n  password: | # keep\n    madeup\n", 1, "password"),
    "yaml-tagged": ("password: !!str madeup\n", 1, "password"),
    "yaml-tagged-quoted": ('password: !!str "madeup"\n', 1, "password"),
    "yaml-anchored": ("password: &pw madeup\n", 1, "password"),
    "yaml-alias-of-an-anchor": (
        "defaults:\n  pw: &pw madeup\nlogin:\n  password: *pw\n",
        2,
        "&pw",
    ),
    "yaml-plain-scalar-on-the-next-line": ("login:\n  password:\n    madeup\n", 1, "password"),
    "yaml-comment-then-next-line": ("password: # set\n  madeup\n", 1, "password"),
    "yaml-two-comment-lines-before-the-value": (
        "login:\n  password: # first\n  # second\n  # third\n    madeup\n",
        1,
        "password",
    ),
    "yaml-alias-with-a-trailing-comment": (
        "d: &pw madeup\npassword: *pw # use default\n",
        2,
        "&pw",
    ),
    "xml-element": ("<config><password>madeup</password></config>", 1, "password"),
    "html-attribute": ('<input type="text" password="madeup">', 1, "password"),
    "json-value-on-the-next-line-in-a-script": (
        '<html><script>var cfg = {"password":\n  "madeup"};</script></html>',
        1,
        "password",
    ),
    "pem-private-key-in-a-yaml-block": (
        "x-key: |\n  " + _PEM_HEADER + "\n  MADEUP\n  " + _PEM_FOOTER + "\n",
        1,
        "RSA PRIVATE KEY",
    ),
    "pem-private-key-in-a-json-string": (
        '{"k": "' + _PEM_HEADER + "\\nMADEUP\\n" + _PEM_FOOTER + '"}',
        1,
        "RSA PRIVATE KEY",
    ),
    # The reviewer's examples on the Aria Automation read (#4731).
    "split-json-key-in-an-html-script-block": (
        '<script type="text/javascript">\n  window.cfg = {\n    "password":\n'
        '      "madeup"\n  };\n</script>',
        1,
        "password",
    ),
    "xml-password-element": ("<settings><password>madeup</password></settings>", 1, "password"),
    "json-apostrophe-in-the-value": ('{"password": "it\'s madeup"}', 1, "password"),
    "yaml-flow-mapping": ("example: {password: madeup}\n", 1, "password"),
    "json-value-on-the-next-line": ('{"password":\n"madeup"}', 1, "password"),
    "yaml-quoted-value-over-two-lines": ('password: "madeup\n  words"\n', 1, "password"),
    "yaml-value-of-two-words": ("password: madeup words\n", 1, "password"),
    "yaml-value-continued-on-the-next-line": ("password: madeup\n  words\n", 1, "password"),
    "xml-comment-inside-the-element": ("<password>made<!-- note -->up</password>", 1, "password"),
    "xml-cdata-inside-the-element": ("<password><![CDATA[madeup]]></password>", 1, "password"),
    "xml-child-element-inside": ("<password>made<b>up</b></password>", 1, "password"),
    "json-escaped-key": ('{"pass\\u0077ord":"madeup"}', 1, "password"),
    # More shapes a served page can take.
    "xml-namespaced-element-with-attributes": (
        '<ns:password type="x">madeup</ns:password>',
        1,
        "password",
    ),
    "xml-element-value-on-the-next-line": ("<password>\n  madeup\n</password>", 1, "password"),
    "xml-element-closed-in-another-case": ("<Password>madeup</PASSWORD>", 1, "Password"),
    "xml-credentials-element-with-children": (
        "<credentials><user>a</user><password>madeup</password></credentials>",
        1,
        "credentials",
    ),
    "xml-property-name-and-value": ('<property name="password" value="madeup"/>', 1, "value"),
    "html-input-name-and-value": (
        '<form><input name="user"><input type="password" name="password" value="madeup"></form>',
        1,
        "value",
    ),
    "html-meta-name-and-content": ('<meta name="password" content="madeup">', 1, "content"),
    "xml-entry-key": ('<entry key="password">madeup</entry>', 1, "password"),
    "html-attribute-in-single-quotes": ("<x password='madeup'/>", 1, "password"),
    "html-attribute-unquoted": ("<x password=madeup>", 1, "password"),
    "script-var": ("var password = 'madeup';", 1, "password"),
    "script-object": ("const c = {user: 'a', password: 'madeup'};", 1, "password"),
    "script-object-over-lines": (
        "const cfg = {\n  user: 'a',\n  password: 'madeup',\n};",
        1,
        "password",
    ),
    "query-string": ("u=https://h/x?password=madeup&token=madeup2", 2, "password"),
    "ini-and-properties": ("[login]\npassword=madeup\ndb.password=madeup\n", 2, "password"),
    "json-space-before-the-colon": ('{"password" : "madeup"}', 1, "password"),
    "json-colon-on-the-next-line": ('{"password"\n: "madeup"}', 1, "password"),
    "json-escaped-quote-in-the-value": ('{"password":"a\\"madeup"}', 1, "password"),
    "json-unterminated-value": ('{"password": "madeup', 1, "password"),
    "json-single-quoted": ("{'password': 'madeup'}", 1, "password"),
    "yaml-single-quoted-key-and-value": ("'password': 'madeup'\n", 1, "password"),
    "yaml-doubled-quote-in-the-value": ("password: 'it''s madeup'\n", 1, "password"),
    "yaml-complex-key": ("? password\n: madeup\n", 1, "password"),
    "yaml-anchor-then-tag": ("password: &a !!str madeup\n", 1, "password"),
    "yaml-binary-block": ("password: !!binary |\n  madeup\n", 1, "password"),
    "yaml-blank-line-inside-the-block": ("password: >\n\n  madeup\n", 1, "password"),
    "yaml-comment-inside-the-block": (
        "login:\n  password:\n    # c\n    madeup\n  user: a\n",
        1,
        "password",
    ),
    "yaml-alias-in-a-flow-mapping": ("x: {password: *pw}\n", 1, "password"),
    "yaml-anchor-under-another-key": (
        "default: &pw madeup\nlogin:\n  password: *pw\n",
        2,
        "&pw",
    ),
    "yaml-nested-flow-mapping": ("a: {b: 1, c: {password: madeup, d: 2}}\n", 1, "password"),
    "yaml-secret-id-pair": ("secret_id: madeup\nsecret_name: foo\n", 1, "secret_id"),
    "yaml-token-final-only": ("tokenUrl: https://x\nx-auth-token: madeup\n", 1, "x-auth-token"),
    "json-nested-under-credentials": ('{"credentials": {"password": "madeup"}}', 1, "password"),
    "json-unicode-value": ('{"password": "mädeup"}', 1, "password"),
    "crlf-lines": ("password: # note\r\n  madeup\r\n", 1, "password"),
    "prose-with-equals": ("Use password=madeup12 to log in", 1, "password"),
    "pem-without-an-end-line": (
        "-----BEGIN EC PRIVATE" + " KEY-----\nMADEUP\n",
        1,
        "EC PRIVATE KEY",
    ),
    # The reviewer's examples on the second round: a naming attribute
    # behind another one, and block text that looks like structure.
    "html-input-id-before-name": (
        '<input id="login-field" name="password" value="madeup">',
        1,
        "value",
    ),
    "html-input-name-before-id": (
        '<input name="password" id="login-field" value="madeup">',
        1,
        "value",
    ),
    "yaml-block-text-with-a-colon": ("password: |\n  madeup: phrase\n", 1, "password"),
    "yaml-block-text-with-a-dash": ("password: |\n  - madeup\n", 1, "password"),
    "yaml-block-text-with-a-hash": ("password: |\n  # madeup\n", 1, "password"),
    "yaml-block-text-after-a-blank-line": (
        "login:\n  password: |\n\n    # madeup\n  user: a\n",
        1,
        "password",
    ),
    # The reviewer's examples on the third round: bare attribute values,
    # and an object or a list under a secret-named key.
    "html-input-unquoted-name": ('<input name=password value="madeup">', 1, "value"),
    "html-input-unquoted-value": ('<input name="password" value=madeup>', 1, "value"),
    "html-input-both-unquoted": ("<input name=password value=madeup>", 1, "value"),
    "html-input-unquoted-self-closing": ("<input name=password value=madeup/>", 1, "value"),
    "json-object-under-credentials": ('{"credentials":{"value":"madeup"}}', 1, "value"),
    "json-list-under-licence-key": ('{"licenseKey":["MADEUP-MADEUP"]}', 1, "licenseKey"),
    "yaml-mapping-under-credentials": ("credentials:\n  value: madeup\n", 1, "value"),
    "yaml-list-under-licence-key": ("licenseKey:\n  - MADEUP-MADEUP\n", 1, "licenseKey"),
    "json-object-nested-under-credentials": (
        '{"credentials":{"type":"x","inner":{"value":"madeup"}}}',
        1,
        "value",
    ),
    "json-secret-key-inside-a-schema-example": (
        '{"credentials":{"example":{"token":{"value":"madeup"}}}}',
        1,
        "value",
    ),
    "json-list-of-objects-under-credentials": ('{"credentials":[{"pass":"madeup"}]}', 1, "pass"),
    "json-value-of-words-under-credentials": (
        '{"credentials":{"value":"made up of words"}}',
        1,
        "value",
    ),
    "json-object-on-the-next-line-under-credentials": (
        '{"credentials":\n  {"value": "madeup"}}',
        1,
        "value",
    ),
    "yaml-list-of-mappings-under-credentials": (
        "credentials:\n  - pass: madeup\n",
        1,
        "pass",
    ),
    "yaml-block-scalar-inside-credentials": ("credentials:\n  value: |\n    madeup\n", 1, "value"),
    "yaml-hidden-sibling-then-value": (
        "credentials:\n  - type: x\n    value: madeup\n",
        1,
        "value",
    ),
    "yaml-anchored-mapping-aliased-by-a-password": (
        "pw: &pw {value: madeup}\npassword: *pw\n",
        2,
        "value",
    ),
    "script-object-under-credentials": (
        "window.cfg = {credentials: {value: 'madeup'}};",
        1,
        "value",
    ),
}


@pytest.mark.parametrize(
    ("text", "count", "first_name"), list(_REFUSED.values()), ids=list(_REFUSED)
)
def test_secret_values_are_found_and_replaced(text: str, count: int, first_name: str) -> None:
    scrubbed, replaced = scrub_api_file_secrets(text)
    assert replaced == count
    assert "madeup" not in scrubbed.lower().replace("\n", "").replace(" ", "")
    assert "MADEUP" not in scrubbed and "mädeup" not in scrubbed
    assert scrubbed.count(REDACTED) == count

    hits = find_api_file_secrets(text)
    assert len(hits) == count
    assert hits[0].name == first_name
    assert all("made" not in hit.description.lower() for hit in hits)


_ACCEPTED = {
    "schema-property": '{"properties":{"password":{"type":"string","format":"password"}}}',
    "short-stubs": '{"password": "string", "token": "string", "max_tokens": 12345678901}',
    "security-scheme": '{"type": "apiKey", "name": "Authorization", "in": "header"}',
    "security-scheme-name": (
        '{"securitySchemes": {"apiKey": {"type": "apiKey", "name": "Authorization"}}}'
    ),
    "key-with-a-secret-prefix": '{"passwordPolicy": {"minimum": "eight-or-more"}}',
    "url-names": '{"tokenUrl": "https://login.example/token", "authorizationUrl": "https://a.example/"}',
    "licence-object": '{"info": {"license": {"name": "Apache 2.0", "url": "https://www.apache.org/"}}}',
    "placeholders": (
        '{"password": "<password>", "token": "${TOKEN}", "licenseKey": "{{ licence_key }}"}'
    ),
    "empty-and-literals": '{"password": "", "token": null, "secret": true}',
    "sentence-about-a-password": '{"description": "Use password: must be at least 8 characters"}',
    "parameter-named-password": '{"name": "password", "in": "formData", "type": "string"}',
    "yaml-schema": (
        "components:\n  schemas:\n    Login:\n      properties:\n        password:\n"
        "          type: string\n        token: {type: string}\n"
        "        pwd: |\n          <password>\n"
    ),
    "yaml-stubs-and-placeholder": (
        "password: string\ntoken: null\nlicenseKey: <licence key>\nsecret: # set later\n"
    ),
    "yaml-comment-tag-and-tagged-stub": (
        "password:\n  # the schema\n  type: string\ntoken: !!null\nsecret: !!str string\n"
    ),
    "yaml-anchored-schema": "- password: &pw\n    type: string\n",
    "yaml-flow-anchor-and-empty-block": "s: &s {type: string}\ntoken: |\nnext: 1\n",
    "yaml-block-of-blank-lines": "token: |\n\n\nnext: 1\n",
    "yaml-list-under-the-key": "password:\n  - type: string\n",
    "yaml-masked-value": "password: ********\n",
    "already-replaced": f"password: {REDACTED}\n" + '{"password": "' + REDACTED + '"}',
    "xsd-element-named-password": '<xs:element name="password" type="xs:string"/>',
    "html-password-input-without-a-value": '<input type="password" name="password">',
    "empty-elements": "<password/><password></password><password><!-- none --></password>",
    "script-with-credentials-flag": "fetch(url, {withCredentials: true});\n",
    # A collection under a secret-named key that describes an API.
    "path-item-named-after-a-password": (
        '{"paths":{"/users/{id}/reset-password":{"post":{"tags":["users"],"summary":"Reset",'
        '"operationId":"reset_password","parameters":[{"name":"sort","in":"query",'
        '"schema":{"type":"string","default":"asc","enum":["asc","desc"]}}],'
        '"responses":{"200":{"description":"Done","content":{"text/html":'
        '{"schema":{"type":"string"}}}}},"security":[{"OAuth2":["read"]}]}}}}'
    ),
    "oauth-password-flow": (
        "components:\n  securitySchemes:\n    oauth:\n      type: oauth2\n      flows:\n"
        "        password:\n          tokenUrl: https://login.example/token\n"
        "          scopes:\n            read: Read access\n"
    ),
    "schema-with-an-example": '{"password": {"type": "string", "example": "example-only"}}',
    "numbers-and-literals-under-credentials": '{"credentials":{"port":8443,"on":true,"n":null}}',
    "description-block-under-credentials": "credentials:\n  description: |\n    value: words\n",
    "stubs-in-a-list-under-a-token": "token:\n  - string\n  - <token>\n",
    "placeholder-in-brackets-under-credentials": (
        f"credentials:\n  value: {REDACTED}\n  - {REDACTED}\n"
    ),
}


@pytest.mark.parametrize("text", list(_ACCEPTED.values()), ids=list(_ACCEPTED))
def test_ordinary_api_description_text_is_left_as_it_is(text: str) -> None:
    assert find_api_file_secrets(text) == []
    assert scrub_api_file_secrets(text) == (text, 0)


def test_everything_outside_the_values_stays_as_it_is() -> None:
    text = (
        '{\n  "openapi": "3.0.1",\n  "x-login": {"user": "svc", "password": "madeup"},\n'
        '  "x-page": "<settings><password>madeup</password></settings>"\n}\n'
    )
    scrubbed, count = scrub_api_file_secrets(text)
    assert count == 2
    assert scrubbed == text.replace("madeup", REDACTED)


def test_the_placeholder_is_the_one_the_governed_vault_read_writes() -> None:
    assert scrub_secret_named_values({"password": "madeup"}) == ({"password": REDACTED}, True)


def test_a_secret_split_across_a_part_cut_is_found_in_the_whole_text() -> None:
    head = '{"pad":"' + "a" * 100 + '","pass'
    tail = 'word": "madeup"}'
    assert find_api_file_secrets(head) == [] and find_api_file_secrets(tail) == []
    assert [hit.name for hit in find_api_file_secrets(head + tail)] == ["password"]


def test_the_description_names_the_field_and_the_place_never_the_value() -> None:
    (hit,) = find_api_file_secrets('{"password": "madeup"}')
    assert hit.description == "the field 'password' at character 14"
    (hit,) = find_api_file_secrets("<password>madeup</password>")
    assert hit.description == "the element 'password' at character 10"
    (hit,) = find_api_file_secrets('<property name="password" value="madeup"/>')
    assert hit.description == "the attribute 'value' at character 33"
    (hit,) = find_api_file_secrets('<input id="login-field" name="password" value="madeup">')
    assert hit.description == "the attribute 'value' at character 47"
    (hit,) = find_api_file_secrets('{"credentials":{"value":"madeup"}}')
    assert hit.description == "the field 'value' at character 25"
    (hit,) = find_api_file_secrets(_PEM_HEADER + "\nMADEUP\n" + _PEM_FOOTER)
    assert hit.description == "a PEM private key (RSA PRIVATE KEY) at character 32"


@pytest.mark.parametrize(
    "text",
    [
        "<password>" * 20_000,
        "".join(f"password: *a{i}\n" for i in range(5_000))
        + "".join(f"x{i}: &a{i} madeup\n" for i in range(5_000)),
        '"password": "madeup", ' * 20_000,
        "password:\n" * 20_000,
        '{"token":' * 20_000 + '"madeup"' + "}" * 20_000,
        '{"credentials":[' + '"madeup",' * 50_000 + '"madeup"]}',
        "credentials:\n" + "  - a: madeup\n" * 50_000,
    ],
    ids=["open-tags", "aliases", "keys", "empty-keys", "nested-objects", "list", "block-list"],
)
def test_many_repeated_shapes_scan_in_one_pass(text: str) -> None:
    # Each shape once scanned a quadratic path; the scan must stay linear.
    scrubbed, _ = scrub_api_file_secrets(text)
    assert "madeup" not in scrubbed
