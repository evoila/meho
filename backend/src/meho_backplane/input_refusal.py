# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one way MEHO words a refused input: the field and the kind of error.

Every refusal of an input that reaches an answer, a stored result, an audit
row or a log line is built here. A refusal names:

* the **field path**, each part cut to :data:`PATH_PART_CAP` characters and
  the whole path to :data:`PATH_CAP` characters, because a part can be a map
  key the caller sent;
* the **kind** of error (the pydantic error type or the JSON Schema keyword);
* a message only when the library builds it from the schema alone.

It never holds the input, a message that a custom validator built (a
``value_error`` can quote the value), or a part of the input that the
library quotes (a tagged-union tag, a JSON Schema message).

* :func:`refusal_text` — one line for an exception message.
* :func:`refusal_lines` — the same, one line per error, for a list answer.
* :func:`refusal_entries` — the error list of a pydantic or FastAPI
  refusal, for a structured answer such as the REST 422 body.
* :func:`refusal_path` / :func:`cut_key` — the path rule alone.
* :func:`error_text` — the text of any caught error, with this rule for a
  refused input.

A check whose message never holds the input raises a subclass of
:class:`FixedMessageError`; its message then stays in the refusal.
"""

from __future__ import annotations

import typing
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

import jsonschema
from pydantic import ValidationError
from pydantic_core.core_schema import ErrorType

__all__ = [
    "PATH_CAP",
    "PATH_PART_CAP",
    "FixedMessageError",
    "cut_key",
    "error_text",
    "refusal_entries",
    "refusal_lines",
    "refusal_path",
    "refusal_text",
]

#: Most characters one part of a field path keeps.
PATH_PART_CAP: Final[int] = 40
#: Most characters a whole field path keeps.
PATH_CAP: Final[int] = 120
#: Most characters one refusal line keeps.
_TEXT_CAP: Final[int] = 2000
_CUT_MARK: Final[str] = "…"

#: The message shown for a kind whose own message can hold input.
_NO_MESSAGE: Final[str] = "the value is not valid"

#: Built-in pydantic kinds whose message can hold the input or text built from it.
_KINDS_WITH_INPUT_IN_MESSAGE: Final[frozenset[str]] = frozenset(
    {
        "assertion_error",
        "bytes_invalid_encoding",
        "datetime_object_invalid",
        "get_attribute_error",
        "iteration_error",
        "json_invalid",
        "mapping_type",
        "no_such_attribute",
        "union_tag_invalid",
        "value_error",
    }
)

#: Built-in pydantic kinds whose message is built from the schema alone.
_KINDS_WITH_SAFE_MESSAGE: Final[frozenset[str]] = (
    frozenset(typing.get_args(ErrorType)) - _KINDS_WITH_INPUT_IN_MESSAGE
)


class FixedMessageError(ValueError):
    """A refusal whose message is fixed text: it never holds the input.

    A pydantic ``value_error`` raised with a subclass of this keeps its
    message in :func:`refusal_text` and :func:`refusal_entries`.
    """


def _cut(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1] + _CUT_MARK


def cut_key(part: object) -> str:
    """One part of a field path, cut to :data:`PATH_PART_CAP` characters.

    A character that cannot be encoded as UTF-8 (a lone surrogate from a
    JSON escape) is written as a backslash escape, so the answer can be sent.
    """
    text = str(part).encode("utf-8", "backslashreplace").decode("utf-8")
    return _cut(text, PATH_PART_CAP)


def refusal_path(parts: Iterable[object], *, empty: str = "params") -> str:
    """The field path ``a.b[0].c``, each part and the whole path cut; *empty* when none."""
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            key = cut_key(part)
            path += f".{key}" if path else key
    return _cut(path, PATH_CAP) if path else empty


def _message_is_safe(error: Mapping[str, Any]) -> bool:
    if str(error.get("type", "")) in _KINDS_WITH_SAFE_MESSAGE:
        return True
    ctx = error.get("ctx")
    return isinstance(ctx, Mapping) and isinstance(ctx.get("error"), FixedMessageError)


def _safe_message(error: Mapping[str, Any]) -> str:
    return str(error.get("msg") or _NO_MESSAGE) if _message_is_safe(error) else _NO_MESSAGE


def refusal_entries(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The errors of a pydantic or FastAPI refusal, with no input in them.

    Each entry keeps the keys it had (``loc``, ``msg``, ``type``, and
    ``ctx`` / ``url`` when present) except ``input``. ``loc`` holds cut
    parts. ``msg`` and ``ctx`` stay only for a kind whose message comes
    from the schema, or a :class:`FixedMessageError`; any other kind gets
    a fixed message and no ``ctx``.
    """
    entries: list[dict[str, Any]] = []
    for error in errors:
        safe = _message_is_safe(error)
        entry: dict[str, Any] = {}
        for key, value in error.items():
            if key == "input" or (key == "ctx" and not safe):
                continue
            if key == "loc":
                entry[key] = [part if isinstance(part, int) else cut_key(part) for part in value]
            elif key == "msg":
                entry[key] = _safe_message(error)
            else:
                entry[key] = value
        entries.append(entry)
    return entries


def refusal_lines(exc: ValidationError) -> list[str]:
    """One line per error of a pydantic refusal: ``<path>: <message> (<kind>)``."""
    return [
        f"{refusal_path(error['loc'])}: {_safe_message(error)} ({error['type']})"
        for error in exc.errors(include_input=False, include_url=False)
    ]


def _schema_text(exc: jsonschema.ValidationError) -> str:
    # The import is here: ``_param_errors`` imports ``_errors``, which imports this module.
    from meho_backplane.operations._param_errors import describe_violation

    entry = describe_violation(exc)
    return f"{refusal_path(exc.absolute_path)}: {entry['expected']} ({entry['validator']})"


def refusal_text(exc: ValidationError | jsonschema.ValidationError) -> str:
    """One line for a refused input: each field path and kind of error, never the input.

    A pydantic refusal lists every error; a JSON Schema refusal is the one
    error the validator raised. ``str()`` of either holds the input.
    """
    if isinstance(exc, ValidationError):
        return _cut("; ".join(refusal_lines(exc)), _TEXT_CAP)
    return _cut(_schema_text(exc), _TEXT_CAP)


def error_text(exc: BaseException) -> str:
    """:func:`refusal_text` for a refused input, ``str(exc)`` for any other error."""
    if isinstance(exc, ValidationError | jsonschema.ValidationError):
        return refusal_text(exc)
    return str(exc)
