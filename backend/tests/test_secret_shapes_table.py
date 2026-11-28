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
label (``api_key=``). The settings check gives the free-text answer for
each row. Every value is made up at run time. A new shape is
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
    """One row: how to make the value, which rule it belongs to, and its two answers.

    ``hidden``: the approval card and the stored result hide the value.
    ``refused``: the free-text check and the settings check refuse it. Both
    answers hold alone and inside a sentence. After a secret-like label
    the approval card and the stored result hide every value. ``pattern`` names the default policy
    rule of the row (``None`` for a decode rule or a text of no rule).
    """

    name: str
    make: Callable[[], str]
    hidden: bool
    refused: bool
    pattern: str | None = None

    @property
    def caught(self) -> frozenset[str]:
        places = {"card", "stored"} if self.hidden else set()
        return frozenset(places | ({"free_text"} if self.refused else set()))


def _key_block(label: str = "", body_length: int = 64) -> str:
    return f"-----BEGIN {label}{_KEY_LINE}\n{_word(body_length)}\n-----END {label}{_KEY_LINE}"


def _slack_digits(*lengths: int) -> str:
    return "-".join(_word(length, string.digits) for length in lengths)


def _cookie_part() -> str:
    # A fixed digit, so the row never depends on chance.
    return _word(19) + secrets.choice(string.digits)


_SURE = {"hidden": True, "refused": True}
_HIDDEN_ONLY = {"hidden": True, "refused": False}
_SHOWN = {"hidden": False, "refused": False}

SHAPES = (
    # Vendor tokens with a clear prefix: hidden and refused.
    *(
        Shape(f"github {p}", lambda p=p: p + _word(36), **_SURE, pattern="vendor_token")
        for p in ("gh" + "p_", "gh" + "o_", "gh" + "u_", "gh" + "s_", "gh" + "r_")
    ),
    Shape(
        "github fine-grained",
        lambda: "github_" + f"pat_{_word(22)}_{_word(59)}",
        **_SURE,
        pattern="vendor_token",
    ),
    Shape("cloud key id AKIA", lambda: "AK" + "IA" + _word(16, _UPPER_DIGITS), **_SURE),
    Shape("cloud key id ASIA", lambda: "AS" + "IA" + _word(16, _UPPER_DIGITS), **_SURE),
    Shape("chat xoxb", lambda: "xo" + f"xb-{_slack_digits(12, 13)}-{_word(24)}", **_SURE),
    Shape(
        "chat xoxp",
        lambda: "xo" + f"xp-{_slack_digits(12, 12, 13)}-{secrets.token_hex(16)}",
        **_SURE,
    ),
    Shape("chat xoxe refresh", lambda: "xo" + "xe-1-" + _word(140, _UPPER_DIGITS), **_SURE),
    Shape(
        "chat xoxe.xoxp rotating",
        lambda: "xo" + "xe." + "xo" + "xp-1-" + _word(160, _UPPER_DIGITS),
        **_SURE,
    ),
    Shape(
        "chat xoxc client",
        lambda: "xo" + f"xc-{_slack_digits(12, 12, 13)}-{secrets.token_hex(32)}",
        **_SURE,
    ),
    Shape(
        "chat xoxd cookie",
        lambda: "xo" + f"xd-{_cookie_part()}%2F{_word(20)}%3D",
        **_SURE,
    ),
    Shape("payment sk_live_", lambda: "sk_" + "live_" + _word(24), **_SURE),
    Shape("payment rk_live_", lambda: "rk_" + "live_" + _word(24), **_SURE),
    Shape("not a token: xoxo- and letters", lambda: "xoxo-" + "hugs-and-kisses", **_SHOWN),
    Shape(
        "not a token: github_pat_ and a setting name",
        lambda: "github_pat_" + "expiry_notification_days",
        **_SHOWN,
    ),
    # The sure rules of the free-text check that a regex of the list cannot do.
    Shape("full key block", _key_block, **_SURE, pattern="pem_private_key"),
    # A body line that does not decode to a key, so the row checks the key
    # block rule and not the decode rule.
    Shape(
        "full key block, two spaces in the label",
        lambda: _key_block("RSA  ", 63),
        hidden=False,
        refused=True,
    ),
    Shape("signed token decoded", _signed_token, **_SURE),
    Shape("32-byte base64 key decoded", _key_base64, hidden=False, refused=True),
    Shape(
        "link with a user and a password",
        lambda: f"postgresql://app:{_word(16)}@db.example.invalid:5432/app",
        **_SURE,
        pattern="url_secret",
    ),
    # Hidden only: each needs a label or a key name to be a secret.
    Shape("key begin line alone", lambda: f"-----BEGIN RSA {_KEY_LINE}", **_HIDDEN_ONLY),
    Shape(
        "signed token form, parts not JSON",
        lambda: f"eyJ{_word(20)}.eyJ{_word(30)}.{_word(30)}",
        **_HIDDEN_ONLY,
        pattern="jwt",
    ),
    Shape(
        "link with a user only",
        lambda: "https://" + "deploy@git.example.invalid/r.git",
        **_HIDDEN_ONLY,
    ),
    Shape(
        "link with a password query value",
        lambda: "https://example.invalid/x?pass" + f"word={_word(16)}",
        **_HIDDEN_ONLY,
    ),
    # 30 letters: a word of 32 would decode to a 24-byte key by chance.
    Shape(
        "authorization header",
        lambda: "Authorization: " + "Bearer " + _word(30, string.ascii_letters),
        **_HIDDEN_ONLY,
        pattern="authorization_header",
    ),
    Shape(
        "bearer and a token",
        lambda: "Bearer " + _word(30, string.ascii_letters),
        **_HIDDEN_ONLY,
        pattern="bearer_token",
    ),
    Shape("bearer and a normal word", lambda: "Bearer " + "authentication", **_HIDDEN_ONLY),
    Shape(
        "word=value",
        lambda: "pass" + "word=" + _word(12, string.ascii_letters),
        **_HIDDEN_ONLY,
        pattern="api_key",
    ),
    Shape(
        "placeholder after a label",
        lambda: "pass" + "word: <from-openbao>",
        **_HIDDEN_ONLY,
    ),
    Shape(
        "kubeconfig",
        lambda: "apiVersion: v1\n" + "kind: Config\nclusters: []\n",
        **_HIDDEN_ONLY,
        pattern="kubeconfig",
    ),
    Shape(
        "password hash",
        lambda: "$" + "6$saltsalt$" + _word(43, _ALNUM + "./"),
        **_HIDDEN_ONLY,
        pattern="password_hash",
    ),
    Shape(
        "Ceph key",
        lambda: "key = " + "AQ" + _word(36, string.ascii_letters) + "==",
        **_HIDDEN_ONLY,
        pattern="ceph_key",
    ),
    Shape(
        "BIND key",
        lambda: (
            'key "rndc-key" { algorithm hmac-sha256; secret "'
            + _word(24, string.ascii_letters)
            + '=="; };'
        ),
        **_HIDDEN_ONLY,
        pattern="bind_key_secret",
    ),
    # No clear prefix: shown unless a label stands in front of it.
    Shape("three random parts", lambda: f"{_word(20)}.{_word(30)}.{_word(30)}", **_SHOWN),
    Shape("checksum (64 hex)", lambda: secrets.token_hex(32), **_SHOWN),
    Shape("commit id (40 hex)", lambda: secrets.token_hex(20), **_SHOWN),
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


@pytest.mark.parametrize(
    ("shape", "place"),
    [
        pytest.param(shape, place, id=f"{shape.name}-{place.name}")
        for shape in SHAPES
        for place in PLACES
        if place.name != "free_text"
    ],
)
def test_a_shape_after_a_label(shape: Shape, place: Place) -> None:
    value = shape.make()
    assert value not in place.run("api_key=" + value)


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_the_settings_check_gives_the_free_text_answer(shape: Shape) -> None:
    value = shape.make()
    mistakes = find_secret_shaped_values({"notes": value})
    assert len(mistakes) == (1 if shape.refused else 0)
    assert all(value not in mistake for mistake in mistakes)


@pytest.mark.parametrize("shape", [s for s in SHAPES if s.refused], ids=lambda s: s.name)
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


def test_every_rule_of_the_default_policy_has_words_and_a_row() -> None:
    rules = {rule.pattern for rule in get_default_policy().rules}
    assert rules <= set(SHAPE_WORDS)
    assert rules <= {shape.pattern for shape in SHAPES}
