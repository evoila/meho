"""One rule for a ticket id ("work ref") at every door.

A ticket id can enter MEHO through the ``Meho-Work-Ref`` header, a request
body, an MCP tool input, a console form, a scheduler trigger, a runbook
start and a run approval request. Every door calls :func:`normalize_work_ref`,
so the same ticket id is the same text everywhere. An audit search then
finds every row of one ticket, and a run approval covers exactly the calls
of that ticket.

The rule:

* white space at both ends is removed;
* an empty value is refused;
* the text is stored in Unicode form NFC;
* a control character (also NUL, a line break and a tab), an invisible
  format character (zero-width and direction marks), a line or paragraph
  separator, and a lone surrogate are refused;
* the text is at most :data:`WORK_REF_MAX_CHARS` characters long.

A value that breaks the rule is refused with a message that names the rule.
It is never cut or changed without a word, apart from the strip and NFC.
Rows stored before this rule may carry white space at the ends.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Final

from pydantic import BeforeValidator

__all__ = [
    "WORK_REF_MAX_CHARS",
    "WorkRef",
    "WorkRefError",
    "normalize_work_ref",
    "normalize_work_ref_input",
]

WORK_REF_MAX_CHARS: Final[int] = 256

# Cc: control (NUL, tab, line break). Cf: format (zero-width, direction
# marks). Zl / Zp: line and paragraph separators. Cs: lone surrogates, which
# cannot be encoded as UTF-8.
_REFUSED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


class WorkRefError(ValueError):
    """A ticket id breaks the rule. The message names the rule."""


def normalize_work_ref(value: str) -> str:
    """Return the cleaned ticket id, or raise :class:`WorkRefError`."""
    cleaned = unicodedata.normalize("NFC", value.strip())
    if not cleaned:
        raise WorkRefError("work_ref must not be empty")
    for char in cleaned:
        if unicodedata.category(char) in _REFUSED_CATEGORIES:
            raise WorkRefError(
                "work_ref must not contain control or invisible characters "
                f"(found U+{ord(char):04X})"
            )
    if len(cleaned) > WORK_REF_MAX_CHARS:
        raise WorkRefError(f"work_ref must be at most {WORK_REF_MAX_CHARS} characters")
    return cleaned


def normalize_work_ref_input(value: Any) -> Any:
    """Clean a ticket id from a request field; pass a non-string on.

    A non-string is left for the field type to refuse. ``None`` stays
    ``None``, so an optional field keeps its "no ticket" meaning.
    """
    if isinstance(value, str):
        return normalize_work_ref(value)
    return value


#: A pydantic field type for a ticket id. A bad value gives a validation error.
WorkRef = Annotated[str, BeforeValidator(normalize_work_ref_input)]
