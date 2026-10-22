# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the whole-file secret scan of ``vcfa.vra8.api_file.get``.

The scan runs on the whole file as text, whatever its format. Every value
here is made up. The read's own tests
(``test_connectors_vra8_api_file.py``) prove the scan runs before the text
is returned; these tests pin what the scan replaces and what it keeps.
"""

from __future__ import annotations

import pytest

from meho_backplane.broadcast import events
from meho_backplane.connectors.vra8.api_file_redaction import REDACTED, redact_secret_values

# Built from two strings so the repository's private-key scan does not see a key.
_PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
_PEM_FOOTER = "-----END " + "RSA PRIVATE KEY-----"


def test_placeholder_is_the_one_the_governed_vault_read_writes() -> None:
    assert REDACTED == events._REDACTED_PARAM


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        # JSON: a quoted key, on one line, with any value shape.
        (
            '{"a": 1, "password": "made-up", "b": 2}',
            f'{{"a": 1, "password": "{REDACTED}", "b": 2}}',
        ),
        ('{"password": "it\'s-made-up"}', f'{{"password": "{REDACTED}"}}'),
        ('{"password": "a\\"b"}', f'{{"password": "{REDACTED}"}}'),
        ('{"pwd": "a"}', f'{{"pwd": "{REDACTED}"}}'),
        ('{"pwd": 1234}', f'{{"pwd": {REDACTED}}}'),
        ('{"licenseKey": "MADE-UP-0000"}', f'{{"licenseKey": "{REDACTED}"}}'),
        ('{"refreshToken": "made-up"}', f'{{"refreshToken": "{REDACTED}"}}'),
        ('{"x-api-key": "made-up"}', f'{{"x-api-key": "{REDACTED}"}}'),
        # The value on the next non-empty line, or the colon on its own line.
        ('{"password":\n    "made-up"}', f'{{"password":\n    "{REDACTED}"}}'),
        ('{"token"\n  :\n  "made-up"}', f'{{"token"\n  :\n  "{REDACTED}"}}'),
        ('{"pwd":\n\n    1234}', f'{{"pwd":\n\n    {REDACTED}}}'),
        # YAML: a bare key at the start of a line, after a list dash, in a flow mapping.
        ("password: made-up\n", f"password: {REDACTED}\n"),
        ("  Password: 'made-up'\n", f"  Password: '{REDACTED}'\n"),
        ("- token: 'it''s-made-up'\n", f"- token: '{REDACTED}'\n"),
        ("licenseKey: MADE-UP-0000\n", f"licenseKey: {REDACTED}\n"),
        ("password:\n  made-up\n", f"password:\n  {REDACTED}\n"),
        ('"password":\n  made-up\n', f'"password":\n  {REDACTED}\n'),
        ("example: {password: made-up}\n", f"example: {{password: {REDACTED}}}\n"),
        ("examples: [{token: made-up}]\n", f"examples: [{{token: {REDACTED}}}]\n"),
        ("password: made-up # note\n", f"password: {REDACTED} # note\n"),
        # A bare value with a space inside goes whole; the comment after it stays.
        ("password: made up\n", f"password: {REDACTED}\n"),
        ("password: made up # note\n", f"password: {REDACTED} # note\n"),
        ("- token: made up, and more\n", f"- token: {REDACTED}, and more\n"),
        # A quoted value over two lines (a YAML quoted scalar) goes up to its closing quote.
        ('password: "made\n  up"\n', f'password: "{REDACTED}"\n'),
        ("password: 'made\n  up'\n", f"password: '{REDACTED}'\n"),
        ('"password":\n  "made\n  up"\nnext: 1\n', f'"password":\n  "{REDACTED}"\nnext: 1\n'),
        # A quote that never closes takes the rest of its line.
        ('password: "made\n', f"password: {REDACTED}\n"),
        # A YAML block scalar: the indented lines below the indicator go too.
        (
            "a:\n  password: |\n    line-one\n    line-two\n  next: 1\n",
            f"a:\n  password: {REDACTED}\n  next: 1\n",
        ),
        (
            "a:\n  password: >-\n    line-one\n\n  next: 1\n",
            f"a:\n  password: {REDACTED}\n  next: 1\n",
        ),
        ("password: |\n  line-one\n", f"password: {REDACTED}\n"),
        # A comment after the indicator stays; the indented lines still go.
        (
            "a:\n  password: | # comment\n    line-one\n    line-two\n  next: 1\n",
            f"a:\n  password: {REDACTED} # comment\n  next: 1\n",
        ),
        ("- password: >-   # c\n    line-one\n", f"- password: {REDACTED}   # c\n"),
        # A properties file, a query string.
        ("password = made-up\n", f"password = {REDACTED}\n"),
        ("url?password=made-up&x=1", f"url?password={REDACTED}"),
        # XML and HTML elements, with a prefix, attributes or a CDATA section.
        ("<password>made-up</password>", f"<password>{REDACTED}</password>"),
        (
            '<ns:password type="x">made-up</ns:password>',
            f'<ns:password type="x">{REDACTED}</ns:password>',
        ),
        ("<password><![CDATA[made<up]]></password>", f"<password>{REDACTED}</password>"),
        # Text split by a comment or a CDATA section goes whole.
        ("<password>made<!-- note -->up</password>", f"<password>{REDACTED}</password>"),
        ("<password>ma<![CDATA[de]]>up</password>", f"<password>{REDACTED}</password>"),
        (
            "<ns:token><!-- a -->made<![CDATA[-]]>up<!-- b --></ns:token>",
            f"<ns:token>{REDACTED}</ns:token>",
        ),
        (
            "<credentials><user>bob</user><password>made-up</password></credentials>",
            f"<credentials><user>bob</user><password>{REDACTED}</password></credentials>",
        ),
        # HTML and XML attributes.
        ('<input password="made-up" name="x">', f'<input password="{REDACTED}" name="x">'),
        ("<input password='made-up'>", f"<input password='{REDACTED}'>"),
        ('<x password=\n "made-up">', f'<x password=\n "{REDACTED}">'),
        # A script block in an HTML page.
        (
            '<script>var cfg = {"password":\n    "made-up"};</script>',
            f'<script>var cfg = {{"password":\n    "{REDACTED}"}};</script>',
        ),
        # A PEM private key block, with real line breaks or JSON escapes.
        (
            f"{_PEM_HEADER}\nMIIEmadeup\nMIIEmadeup\n{_PEM_FOOTER}\n",
            f"{_PEM_HEADER}{REDACTED}{_PEM_FOOTER}\n",
        ),
        (
            f'"key": "{_PEM_HEADER}\\nMIIEmadeup\\n{_PEM_FOOTER}"',
            f'"key": "{_PEM_HEADER}{REDACTED}{_PEM_FOOTER}"',
        ),
        (f"{_PEM_HEADER}\nMIIEmadeup\n", f"{_PEM_HEADER}{REDACTED}"),
    ],
)
def test_one_secret_value_is_replaced_and_the_rest_stays(text: str, cleaned: str) -> None:
    assert redact_secret_values(text) == (cleaned, 1)
    for secret in ("made", "MADE", "1234", "line-", "MIIE"):
        assert secret not in cleaned


@pytest.mark.parametrize(
    "text",
    [
        # Schema definitions name the field; they hold no value.
        '{"definitions": {"Login": {"properties": {"password": {"type": "string"}, '
        '"token": {"type": "string", "example": ""}}, "required": ["username", "password"]}}, '
        '"securityDefinitions": {"oauth": {"flow": "password", '
        '"tokenUrl": "https://example.invalid/token"}}, '
        '"x-example": {"username": "u", "password": null, "pwd": false}}',
        "definitions:\n  Login:\n    properties:\n      password:\n        type: string\n"
        "        format: password\n      token: {type: string, example: ''}\n"
        "    required:\n      - username\n      - password\nsecurityDefinitions:\n  oauth:\n"
        "    flow: password\n    tokenUrl: https://example.invalid/token\n"
        "    description: 'Send the token: in the Authorization header'\n"
        "x-example: {username: u, password: null, pwd: '', token: {}}\n",
        "password:\n  - a\n  - b\n",
        '"password":\n}',
        "password: # only a comment\n",
        # HTML and XML that name the field, or hold the word in running text.
        '<input type="password" name="password" placeholder="Password">',
        '<xs:element name="password" type="xs:string"/>',
        "<password></password><password>  </password>",
        "<password><!-- set at install --></password><password><![CDATA[]]></password>",
        "<!DOCTYPE html>\n<html><body>\n<p>Docs: the token: see the header [not: yaml: {</p>\n"
        "</body></html>\n",
        ".token { color: red }\n--token-color: red;\n",
        # A label inside a string value is not a key; the stored-result
        # redaction refuses the long form of this one.
        '{"note": "password=made-up-pass-value-0123"}',
    ],
)
def test_text_without_a_secret_value_stays_as_it_is(text: str) -> None:
    assert redact_secret_values(text) == (text, 0)


def test_every_value_is_counted_and_a_placeholder_is_not_counted_again() -> None:
    text = (
        '{"x-examples": [{"password":\n "made-up"}, {"token":\n "made-up"}, {"pwd": null}],\n'
        f' "already": {{"secret": "{REDACTED}"}}}}'
    )
    cleaned, count = redact_secret_values(text)
    assert count == 2
    assert "made-up" not in cleaned
    assert redact_secret_values(cleaned) == (cleaned, 0)


def test_a_secret_named_element_with_children_is_checked_child_by_child() -> None:
    text = "<credentials>\n  <user>bob</user>\n  <token>made-up</token>\n</credentials>\n"
    cleaned, count = redact_secret_values(text)
    assert count == 1
    assert (
        cleaned
        == f"<credentials>\n  <user>bob</user>\n  <token>{REDACTED}</token>\n</credentials>\n"
    )
