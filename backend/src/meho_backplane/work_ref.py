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

A search filter on a ticket id (the audit search, the approvals, runs,
scheduler and runbook run lists and the broadcast history) is checked with
the same rule (:data:`WorkRefFilter`). It matches the cleaned id, so a search
finds an id however it is typed. Rows stored before this rule may carry
white space at the ends or another Unicode form. The filter also matches its
text exactly as typed (:func:`work_ref_filter_values`), so a search with the
exact old text still finds such a row.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Final

from pydantic import BeforeValidator

__all__ = [
    "WORK_REF_FIELD_DESCRIPTION",
    "WORK_REF_MAX_CHARS",
    "WorkRef",
    "WorkRefError",
    "WorkRefFilter",
    "check_work_ref_filter_input",
    "normalize_work_ref",
    "normalize_work_ref_input",
    "work_ref_filter_values",
]

WORK_REF_MAX_CHARS: Final[int] = 256

#: The rule in one line, for the description of a ``work_ref`` field.
WORK_REF_FIELD_DESCRIPTION: Final[str] = (
    "Ticket id. White space at both ends is removed; it must not be blank, "
    f"must not hold control or invisible characters, and is at most {WORK_REF_MAX_CHARS} "
    "characters."
)

# Cc: control (NUL, tab, line break). Cf: format (zero-width, direction
# marks). Zl / Zp: line and paragraph separators. Cs: lone surrogates, which
# cannot be encoded as UTF-8.
_REFUSED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


class WorkRefError(ValueError):
    """A ticket id breaks the rule. The message names the rule.

    ``rule`` is a short code for the rule that failed (``empty``,
    ``invisible_character`` or ``too_long``). It never holds the value, so
    a log line can carry it.
    """

    def __init__(self, message: str, *, rule: str) -> None:
        super().__init__(message)
        self.rule = rule


def normalize_work_ref(value: str) -> str:
    """Return the cleaned ticket id, or raise :class:`WorkRefError`."""
    cleaned = unicodedata.normalize("NFC", value.strip())
    if not cleaned:
        raise WorkRefError("work_ref must not be empty", rule="empty")
    for char in cleaned:
        if unicodedata.category(char) in _REFUSED_CATEGORIES:
            raise WorkRefError(
                "work_ref must not contain control or invisible characters "
                f"(found U+{ord(char):04X})",
                rule="invisible_character",
            )
    if len(cleaned) > WORK_REF_MAX_CHARS:
        raise WorkRefError(
            f"work_ref must be at most {WORK_REF_MAX_CHARS} characters", rule="too_long"
        )
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


def check_work_ref_filter_input(value: Any) -> Any:
    """Check a ticket id search filter with the rule; keep the text as typed.

    A value that breaks the rule raises :class:`WorkRefError`. A good value
    is returned unchanged, so :func:`work_ref_filter_values` can match rows
    stored before the rule too. A non-string is left for the field type.
    """
    if isinstance(value, str):
        normalize_work_ref(value)
    return value


def work_ref_filter_values(value: str) -> tuple[str, ...]:
    """Return the stored texts a ticket id search filter matches.

    The cleaned id, and the text as typed when it differs: a row stored
    before the rule keeps its old text. A value that breaks the rule (a
    console filter box does not check it) matches only its exact text.
    """
    try:
        cleaned = normalize_work_ref(value)
    except WorkRefError:
        return (value,)
    return (cleaned,) if cleaned == value else (cleaned, value)


#: A pydantic field type for a ticket id search filter. A bad value gives a
#: validation error; a good value keeps its text for :func:`work_ref_filter_values`.
WorkRefFilter = Annotated[str, BeforeValidator(check_work_ref_filter_input)]
