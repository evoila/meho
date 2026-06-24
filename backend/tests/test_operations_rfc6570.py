# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared RFC6570 path-operator vocabulary (#2066).

:mod:`meho_backplane.operations._rfc6570` is the single source of truth the
ingest parser and the path renderer share so the property key the parser
registers can never drift from the bare name the renderer looks up. These
tests pin :func:`split_path_operator`'s exact (single-leading-operator)
behaviour and assert the renderer's regex operator class is built from the
same constant.
"""

from __future__ import annotations

import pytest

from meho_backplane.operations._branches import _PATH_VAR_RE
from meho_backplane.operations._rfc6570 import (
    RFC6570_PATH_OPERATORS,
    split_path_operator,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("+path", ("+", "path")),
        ("#frag", ("#", "frag")),
        (".label", (".", "label")),
        ("/seg", ("/", "seg")),
        (";p", (";", "p")),
        ("?q", ("?", "q")),
        ("&r", ("&", "r")),
        ("path", ("", "path")),  # no operator
        ("++weird", ("+", "+weird")),  # only the first leading char is consumed
        ("path+", ("", "path+")),  # a trailing operator stays part of the name
        ("", ("", "")),  # empty name
    ],
)
def test_split_path_operator(name: str, expected: tuple[str, str]) -> None:
    assert split_path_operator(name) == expected


def test_renderer_regex_uses_the_shared_operator_set() -> None:
    """Every shared operator is recognised by the renderer regex, and vice versa.

    The anti-drift guarantee: the parser strips exactly the operators the
    renderer recognises, because both read :data:`RFC6570_PATH_OPERATORS`.
    Asserting the renderer's compiled regex strips each shared operator (and
    that a non-operator leading char is *not* stripped) keeps the two stages
    locked together even if the regex construction is later refactored.
    """
    for op in RFC6570_PATH_OPERATORS:
        match = _PATH_VAR_RE.fullmatch(f"{{{op}name}}")
        assert match is not None
        assert match.group(1) == op
        assert match.group(2) == "name"
    # A char outside the set is not treated as an operator (stays in the name).
    no_op = _PATH_VAR_RE.fullmatch("{xname}")
    assert no_op is not None
    assert no_op.group(1) == ""
    assert no_op.group(2) == "xname"
