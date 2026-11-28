# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A command line password of any shell shape is hidden whole, in answers and traces.

Each value is built from quoted parts, bare parts, escaped quotes and
escaped spaces. Its first piece is four characters long, so the api_key
rule hides nothing of it, and this rule must hide all of it. The option
name and the next command stay readable.

The inputs are built from ``BS``, ``SQ`` and ``DQ`` so that no backslash
or quote gets lost in the source.
"""

from __future__ import annotations

from typing import Final

import pytest

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.flight_recorder._content import _CONTENT_POLICY, scrub_content
from meho_backplane.redaction.policy import RedactionPolicy
from meho_backplane.redaction.resolver import get_default_policy

BS: Final = chr(92)
SQ: Final = chr(39)
DQ: Final = chr(34)

#: The made-up pieces of each value. None of them may be shown.
_PIECES: Final = ("EXAM", "VALUE", "PART", "ONE")
#: The two command line rules: before and after the api_key rule.
_COMMAND_RULES: Final = frozenset({"command_line_secret", "command_line_secret_rest"})

#: The value shapes, by name.
_SHAPES: Final[dict[str, str]] = {
    "bare": "EXAM",
    "single-quoted": SQ + "EXAM VALUE" + SQ,
    "double-quoted": DQ + "EXAM VALUE" + DQ,
    "double-quoted with an escaped double quote": DQ + "EXAM" + BS + DQ + "VALUE" + DQ,
    "double-quoted with two backslashes": DQ + "EXAM" + BS + BS + "VALUE" + DQ,
    "bare with an escaped double quote": "EXAM" + BS + DQ + "VALUE",
    "bare with an escaped single quote": "EXAM" + BS + SQ + "VALUE",
    "bare with an escaped space": "EXAM" + BS + " VALUE",
    "single then double": SQ + "EXAM" + SQ + DQ + "VALUE PART" + DQ,
    "double then bare": DQ + "EXAM" + DQ + "VALUE",
    "bare then single": "EXAM" + SQ + "VALUE PART" + SQ,
    "bare, escaped double quote, single": "EXAM" + BS + DQ + "VALUE" + SQ + "PART ONE" + SQ,
    "bare, escaped single quote, double": "EXAM" + BS + SQ + "VALUE" + DQ + "PART ONE" + DQ,
    "single, escaped single quote, single": SQ + "EXAM" + SQ + BS + SQ + SQ + "VALUE PART" + SQ,
    "ANSI-C with an escaped quote": "$" + SQ + "EXAM" + BS + SQ + "VALUE PART" + SQ,
    "ANSI-C with a space first": "$" + SQ + "EXAM VALUE" + BS + SQ + "PART" + SQ,
}

#: Each form, with ``{}`` where the value stands.
_FORMS: Final = (
    "run --password={}",
    "run --password {}",
    "app --api-token={}",
    "mysql -uroot -p{}",
    "sshpass -p {}",
    "DB_PASSWORD={} run",
    "export DB_PASSWORD={}",
)

#: Each tail after the form, and the word of it that must stay.
_TAILS: Final = (
    ("", ""),
    ("; echo ok", "echo ok"),
    (" && echo ok", "echo ok"),
    (" | cat", "cat"),
    ("\necho ok", "echo ok"),
    (";echo ok", "echo ok"),
)


def _without_command_rules(policy: RedactionPolicy) -> RedactionPolicy:
    rules = tuple(rule for rule in policy.rules if rule.pattern not in _COMMAND_RULES)
    return policy.model_copy(update={"rules": rules})


def _answer_and_trace(text: str) -> tuple[str, str]:
    return redact(text, get_default_policy()).redacted, scrub_content(text)[0]


def _on_main(text: str) -> tuple[str, str]:
    return (
        redact(text, _without_command_rules(get_default_policy())).redacted,
        redact(text, _without_command_rules(_CONTENT_POLICY)).redacted,
    )


def _problems(text: str, keep: str) -> list[str]:
    """What is wrong with the answer and the trace of *text*."""
    problems: list[str] = []
    for out, on_main in zip(_answer_and_trace(text), _on_main(text), strict=True):
        problems += [f"{piece} shown in {out!r}" for piece in _PIECES if piece in out]
        # Where the api_key rule takes the next command too, the answer is as on main.
        if keep not in out and out != on_main:
            problems.append(f"{keep!r} lost in {out!r}")
    return problems


def test_every_value_shape_is_hidden_whole_in_every_form_and_before_every_tail() -> None:
    wrong: dict[str, list[str]] = {}
    runs = 0
    for shape in _SHAPES.values():
        for form in _FORMS:
            for tail, keep in _TAILS:
                text = form.format(shape) + tail
                runs += 2
                if problems := _problems(text, keep):
                    wrong[text] = problems
    assert runs == len(_SHAPES) * len(_FORMS) * len(_TAILS) * 2
    assert wrong == {}


#: (text, the words that stay): the cases named in the review, and the ones
#: where a character other than white space follows the value.
_NAMED: Final = (
    ("run --password=EXAM; echo ok", ("run --password=", "; echo ok")),
    ("export DB_PASSWORD=EXAM; echo ok", ("export DB_PASSWORD=", "; echo ok")),
    ("run --password=EXAM" + BS + " VALUE; echo ok", ("run --password=", "; echo ok")),
    (
        "run --password=EXAM" + BS + DQ + "VALUE" + SQ + "PART ONE" + SQ + "; echo ok",
        ("run --password=", "; echo ok"),
    ),
    (
        "DB_PASSWORD=EXAM" + BS + DQ + "VALUE" + SQ + "PART ONE" + SQ + "; run",
        ("DB_PASSWORD=", "; run"),
    ),
    (
        "run --password=EXAM" + BS + SQ + "VALUE" + DQ + "PART ONE" + DQ + " && echo ok",
        ("run --password=", " && echo ok"),
    ),
    # An ANSI-C quoted value is one part: a backslash takes the next quote.
    (
        "run --password=$" + SQ + "EXAM" + BS + SQ + "VALUE PART" + SQ + "; echo ok",
        ("run --password=", "; echo ok"),
    ),
    (
        "DB_PASSWORD=$" + SQ + "EXAM" + BS + SQ + "VALUE PART" + SQ + " run",
        ("DB_PASSWORD=", " run"),
    ),
    ("mysql -p$" + SQ + "EXAM" + BS + SQ + "VALUE PART" + SQ + " db", ("mysql -p", " db")),
)


@pytest.mark.parametrize(("text", "kept"), _NAMED)
def test_a_value_before_an_operator_or_with_escapes_is_hidden_whole(
    text: str, kept: tuple[str, ...]
) -> None:
    for out in _answer_and_trace(text):
        assert not [piece for piece in _PIECES if piece in out], out
        for words in kept:
            assert words in out, out


#: The value of a password option whose end is not known: its quote is never closed.
_NO_END: Final = (
    ("run --password " + DQ + "EXAM VALUE; echo ok", "; echo ok"),
    ("run --password " + SQ + "EXAM VALUE | cat", "| cat"),
    ("mysql -p" + SQ + "EXAM VALUE && echo ok", "&& echo ok"),
)


@pytest.mark.parametrize(("text", "kept"), _NO_END)
def test_a_value_without_a_known_end_hides_the_rest_of_its_command(text: str, kept: str) -> None:
    for out in _answer_and_trace(text):
        assert "EXAM" not in out
        assert "VALUE" not in out
        assert out.endswith(kept)
