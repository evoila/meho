# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the YAML or JSON text of a ``k8s.apply`` manifest.

:func:`load_manifest_documents` reads every document of the text. When the
text cannot be read, it raises :class:`ManifestYamlError`, whose message
quotes nothing from the manifest, so a Secret value in the manifest cannot
reach the approval card, an agent, the audit row or a log through it.

The message keeps:

* the explanation of the parser, for example
  ``expected ',' or ']', but got '<stream end>'``. A quoted part of it stays
  when it names a token of the YAML grammar (``','``, ``'<stream end>'``),
  or when it is fixed text of the parser that says what it wanted
  (``expected a digit or '.'``) and not the text it read (after ``found``
  or ``got``). Any other quoted part (an alias name, a tag, a character of
  the manifest) becomes ``'…'``. For a ``%`` escape in a tag that is not
  UTF-8 the explanation is ``a %-escape in a tag is not valid UTF-8``, with
  no byte value;
* the line and the column of each part, or the position of a character the
  reader refuses;
* for a value that its tag cannot read (``!!int abc``), a fixed explanation
  with the line and the column of the value.

An error with no explanation (an object nested too deep, for example) gives
only ``manifest is not valid YAML/JSON``.
"""

from __future__ import annotations

import re
from typing import Any, Final

import yaml

from meho_backplane.redaction.command_line import hide_in_text

__all__ = ["ManifestYamlError", "load_manifest_documents"]

#: The text every error of :func:`load_manifest_documents` starts with.
_NOT_VALID: Final[str] = "manifest is not valid YAML/JSON"

#: The names of the YAML tokens a parser message may quote
#: (``yaml.tokens.*.id``). Each is a word of the grammar, never manifest text.
_TOKEN_NAMES: Final[frozenset[str]] = frozenset(
    {
        "<directive>",
        "<document start>",
        "<document end>",
        "<stream start>",
        "<stream end>",
        "<block sequence start>",
        "<block mapping start>",
        "<block end>",
        "<alias>",
        "<anchor>",
        "<tag>",
        "<scalar>",
        "[",
        "]",
        "{",
        "}",
        "?",
        ":",
        "-",
        ",",
    }
)

#: A quoted part of a parser message, in single or double quotes, as
#: ``repr`` writes it.
_QUOTED: Final[re.Pattern[str]] = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")

#: The quoted parts of PyYAML's fixed message texts that are not token
#: names (``expected a digit or '.'``, ``expected '!'``). Each tells the
#: caller what the parser wanted, and stays, except right after ``found``
#: or ``got``, where the parser quotes the text it read.
_FIXED_QUOTES: Final[frozenset[str]] = frozenset({".", " ", "!", ">"})

#: The words after which the parser quotes text of the manifest.
_READ_TEXT_BEFORE: Final[tuple[str, ...]] = ("found ", "got ")

#: The explanation for a ``%`` escape in a tag that is not UTF-8. The
#: parser's own text names the byte value.
_ESCAPE_NOT_UTF8: Final[str] = "a %-escape in a tag is not valid UTF-8"

#: Shown in place of a quoted part that is not a token name.
_QUOTE_HIDDEN: Final[str] = "'…'"

#: The explanation for a value that its tag cannot read.
_TAG_CANNOT_READ: Final[str] = "a value cannot be read as the type its tag names"


class ManifestYamlError(ValueError):
    """The manifest text cannot be read; the message quotes nothing from it."""


class _ManifestLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml has no stubs here
    """The safe loader, with a value that its tag cannot read as a YAML error.

    ``!!int abc`` makes Python's ``int`` raise a ``ValueError`` whose text
    holds ``abc``. Here it becomes a :class:`yaml.constructor.ConstructorError`
    with a fixed explanation and the place of the value.
    """

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except yaml.YAMLError:
            raise
        except Exception:
            raise yaml.constructor.ConstructorError(
                None, None, _TAG_CANNOT_READ, node.start_mark
            ) from None


def _keep_token_names(text: str) -> str:
    """*text* with each quoted part replaced that is neither a token name nor fixed text.

    A part of :data:`_FIXED_QUOTES` stays only where it is not the text the
    parser read (right after ``found`` or ``got``).
    """

    def _one(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1]
        if inner in _TOKEN_NAMES:
            return match.group(0)
        read_text = text[: match.start()].endswith(_READ_TEXT_BEFORE)
        return match.group(0) if inner in _FIXED_QUOTES and not read_text else _QUOTE_HIDDEN

    return _QUOTED.sub(_one, text)


def _problem_text(text: str) -> str:
    """The parser's explanation, with no text of the manifest (:func:`_keep_token_names`).

    A ``%`` escape in a tag that is not UTF-8 gives :data:`_ESCAPE_NOT_UTF8`
    instead of the decoder's text, which names the byte value.
    """
    if "codec can't decode" in text:
        return _ESCAPE_NOT_UTF8
    return _keep_token_names(text)


def _place(mark: Any) -> str:
    """`` (line L, column C)`` of a YAML mark, or empty text when there is no mark."""
    if mark is None:
        return ""
    return f" (line {mark.line + 1}, column {mark.column + 1})"


def _explanation(exc: BaseException) -> str:
    """What the parser found wrong and where, with no text of the manifest."""
    if isinstance(exc, yaml.reader.ReaderError):
        reason = exc.reason if isinstance(exc.reason, str) else ""
        return f"unacceptable character: {reason} (position {exc.position})"
    parts = [
        f"{_problem_text(text)}{_place(getattr(exc, mark, None))}"
        for text, mark in (
            (getattr(exc, "context", None), "context_mark"),
            (getattr(exc, "problem", None), "problem_mark"),
        )
        if isinstance(text, str) and text
    ]
    return "; ".join(parts)


def load_manifest_documents(manifest: str) -> list[Any]:
    """Every document of *manifest*, as YAML reads it.

    Raises :class:`ManifestYamlError` for any error that reading the text
    raises, of any error class. Its message is described in the module text.
    """
    try:
        return _read_all(manifest)
    except Exception as exc:
        explanation = hide_in_text(_explanation(exc)) if isinstance(exc, yaml.YAMLError) else ""
        raise ManifestYamlError(
            f"{_NOT_VALID}: {explanation}" if explanation else _NOT_VALID
        ) from None


def _read_all(manifest: str) -> list[Any]:
    """Every document of *manifest*, as ``yaml.safe_load_all`` reads them."""
    # The reader checks the whole text for refused characters already here.
    loader = _ManifestLoader(manifest)
    try:
        docs: list[Any] = []
        while loader.check_data():
            docs.append(loader.get_data())
        return docs
    finally:
        loader.dispose()
