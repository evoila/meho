# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One table: each secret shape against each place that hides or refuses it.

Shapes go down, places go across:

* ``free_text``: the free-text check (:func:`contains_secret`);
* ``card``: the parameters shown on an approval card
  (:func:`_redact_echoed_params`);
* ``stored``: the stored result of a call
  (:func:`apply_connector_boundary_redaction`).

Each shape is checked alone, inside a sentence, and after a secret-like
label (``api_key=``). Every value is made up at run time. A new shape is
added in one place: the named patterns of
:mod:`meho_backplane.redaction.patterns`, and one row here.
"""

from __future__ import annotations

import base64
import json
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from meho_backplane.instance_settings.schema import (
    contains_secret,
    find_secret_shaped_values,
    parse_settings_file,
)
from meho_backplane.operations._preview import _redact_echoed_params
from meho_backplane.redaction import apply_connector_boundary_redaction, get_default_policy
from meho_backplane.redaction.secret_shapes import SHAPE_WORDS

_UPPER_DIGITS = string.ascii_uppercase + string.digits
_ALNUM = string.ascii_letters + string.digits
# Built from parts, so no line of this file holds a whole key line.
_KEY_LINE = "PRIVATE " + "KEY-----"


def _word(length: int, alphabet: str = _ALNUM) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signed_token() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": _word(12)}).encode())
    return f"{header}.{payload}.{_word(43)}"


def _key_base64() -> str:
    while True:
        text = base64.b64encode(secrets.token_bytes(32)).decode()
        kinds = (str.isupper, str.islower, str.isdigit)
        if all(any(kind(c) for c in text) for kind in kinds):
            return text


@dataclass(frozen=True)
class Place:
    """Where a text goes, and what is left of it afterwards."""

    name: str
    run: Callable[[str], str]


def _free_text(text: str) -> str:
    return "" if contains_secret(text) else text


def _card(text: str) -> str:
    echoed = _redact_echoed_params(
        {"note": text}, connector_id="made-up", tenant=None, op_id="made-up.op"
    )
    return str(echoed["note"])


def _stored(text: str) -> str:
    stored = apply_connector_boundary_redaction(
        {"message": text}, connector_id="made-up", tenant=None, op="made-up.op"
    )
    return str(stored.redacted["message"])


PLACES = (
    Place("free_text", _free_text),
    Place("card", _card),
    Place("stored", _stored),
)


@dataclass(frozen=True)
class Shape:
    """One row: how to make the value, and where it is caught when it stands alone.

    ``caught`` names the places that catch the value alone and inside a
    sentence. After a secret-like label every place catches it.
    """

    name: str
    make: Callable[[], str]
    caught: frozenset[str]


_ALL = frozenset(place.name for place in PLACES)
_NONE: frozenset[str] = frozenset()

SHAPES = (
    # Vendor tokens with a clear prefix: caught everywhere.
    *(
        Shape(f"github {prefix}", lambda p=prefix: p + _word(36), _ALL)
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")
    ),
    Shape("github fine-grained", lambda: f"github_pat_{_word(22)}_{_word(59)}", _ALL),
    Shape("cloud key id AKIA", lambda: "AKIA" + _word(16, _UPPER_DIGITS), _ALL),
    Shape("cloud key id ASIA", lambda: "ASIA" + _word(16, _UPPER_DIGITS), _ALL),
    *(
        Shape(
            f"chat bot {prefix}",
            lambda p=prefix: f"{p}{_word(12, string.digits)}-{_word(24)}",
            _ALL,
        )
        for prefix in ("xoxb-", "xoxp-")
    ),
    Shape("payment sk_live_", lambda: "sk_live_" + _word(24), _ALL),
    Shape("payment rk_live_", lambda: "rk_live_" + _word(24), _ALL),
    # Shapes the redaction engine catches by form: caught everywhere.
    Shape("signed token", _signed_token, _ALL),
    Shape(
        "signed token form, parts not JSON",
        lambda: f"eyJ{_word(20)}.eyJ{_word(30)}.{_word(30)}",
        _ALL,
    ),
    Shape(
        "key block",
        lambda: f"-----BEGIN {_KEY_LINE}\n{_word(64)}\n-----END {_KEY_LINE}",
        _ALL,
    ),
    Shape("key begin line alone", lambda: f"-----BEGIN RSA {_KEY_LINE}", _ALL),
    Shape("bearer header", lambda: "Authorization: Bearer " + _word(32), _ALL),
    Shape(
        "connection string with a password",
        lambda: f"postgresql://app:{_word(16)}@db.example.invalid:5432/app",
        _ALL,
    ),
    Shape(
        "password in a link",
        lambda: f"https://example.invalid/x?password={_word(16)}",
        _ALL,
    ),
    # No clear prefix: shown unless a label stands in front of it.
    Shape("three random parts", lambda: f"{_word(20)}.{_word(30)}.{_word(30)}", _NONE),
    Shape("checksum (64 hex)", lambda: secrets.token_hex(32), _NONE),
    Shape("commit id (40 hex)", lambda: secrets.token_hex(20), _NONE),
    # Base64 that decodes to a key: the free-text check decodes it.
    Shape("32-byte base64 key", _key_base64, frozenset({"free_text"})),
)


def _sentence(value: str) -> str:
    return f"the run on Monday used {value} at noon"


def _cases() -> list[object]:
    return [
        pytest.param(shape, place, id=f"{shape.name}-{place.name}")
        for shape in SHAPES
        for place in PLACES
    ]


@pytest.mark.parametrize(("shape", "place"), _cases())
def test_a_shape_alone(shape: Shape, place: Place) -> None:
    value = shape.make()
    assert (value in place.run(value)) is (place.name not in shape.caught)


@pytest.mark.parametrize(("shape", "place"), _cases())
def test_a_shape_in_a_sentence(shape: Shape, place: Place) -> None:
    value = shape.make()
    sentence = _sentence(value)
    left = place.run(sentence)
    assert (value in left) is (place.name not in shape.caught)
    if place.name != "free_text":
        assert left.startswith("the run on Monday used ")


@pytest.mark.parametrize(("shape", "place"), _cases())
def test_a_shape_after_a_label(shape: Shape, place: Place) -> None:
    value = shape.make()
    assert value not in place.run("api_key=" + value)


@pytest.mark.parametrize(
    "shape", [shape for shape in SHAPES if shape.caught == _ALL], ids=lambda s: s.name
)
def test_the_settings_import_refuses_a_shape(shape: Shape) -> None:
    value = shape.make()
    assert len(find_secret_shaped_values({"notes": value})) == 1
    content = (
        "apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        "    - targets:\n        - name: t\n          product: ssh\n          host: h\n"
        f"          notes: {json.dumps(value)}\n"
    ).encode()
    parsed = parse_settings_file(content)
    assert parsed.model is None
    assert any("notes" in mistake for mistake in parsed.mistakes)
    assert all(value not in mistake for mistake in parsed.mistakes)


def test_every_rule_of_the_default_policy_has_words() -> None:
    assert {rule.pattern for rule in get_default_policy().rules} <= set(SHAPE_WORDS)
