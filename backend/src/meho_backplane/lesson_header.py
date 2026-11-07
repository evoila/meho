# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The lesson header: the metadata fields a knowledge or memory note may carry.

Knowledge and memory entries keep free-form ``metadata``. This module
gives a fixed set of names in that metadata a meaning, and checks their
values. The same header is used in the team toolkit repos, so a note
can move from one side to the other without a change.

Fields:

* ``type``: what kind of note it is (:data:`LESSON_TYPES`). An
  ``observation`` is a story of what happened. It never counts as a
  confirmed lesson.
* ``applies_to``: who the note is about (:data:`LESSON_APPLIES_TO`).
* ``status``: how far it is trusted (:data:`LESSON_STATUSES`).
* ``learned_at``, ``confirmed_at``, ``expires_at``: dates. A date, a
  datetime or an ISO 8601 text is accepted and stored as ISO 8601 text.
* ``product``, ``version``, ``source``, ``author``, ``summary``: text.
* ``tags``: a list of text.

A field that is absent stays absent. A wrong value raises
:class:`LessonHeaderError`, which says which field and which values are
allowed.

MEHO keeps four names for itself: ``slug``, ``provenance``, ``kind`` and
``scope``. The header never uses them. :func:`normalise_lesson_header`
refuses them, except the names the caller says MEHO overwrites on its own
(``allowed_reserved``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Final

__all__ = [
    "CANDIDATE_EXPIRY_MONTHS",
    "LESSON_APPLIES_TO",
    "LESSON_STATUSES",
    "LESSON_TYPES",
    "RESERVED_NAMES",
    "LessonHeaderError",
    "add_candidate_expiry",
    "header_expires_at",
    "header_rows",
    "normalise_lesson_header",
]

LESSON_TYPES: Final[frozenset[str]] = frozenset(
    {"fact", "lesson", "decision", "rule", "person-and-role", "preference", "observation"}
)
LESSON_APPLIES_TO: Final[frozenset[str]] = frozenset(
    {"tenant", "customer", "target", "product-and-version", "team", "person"}
)
LESSON_STATUSES: Final[frozenset[str]] = frozenset(
    {"candidate", "confirmed", "disputed", "replaced", "retired"}
)

#: Names MEHO sets itself. The header never uses them.
RESERVED_NAMES: Final[frozenset[str]] = frozenset({"slug", "provenance", "kind", "scope"})

#: A candidate that names no ``expires_at`` expires this many months
#: after ``learned_at``. A confirmed entry gets no expiry.
CANDIDATE_EXPIRY_MONTHS: Final[int] = 6

_ENUM_FIELDS: Final[dict[str, frozenset[str]]] = {
    "type": LESSON_TYPES,
    "applies_to": LESSON_APPLIES_TO,
    "status": LESSON_STATUSES,
}
_TEXT_FIELDS: Final[tuple[str, ...]] = ("product", "version", "source", "author", "summary")
_DATE_FIELDS: Final[tuple[str, ...]] = ("learned_at", "confirmed_at", "expires_at")
#: The header fields in the order a read surface shows them.
_SHOWN_FIELDS: Final[tuple[str, ...]] = (
    *_ENUM_FIELDS,
    "summary",
    "product",
    "version",
    "source",
    "author",
    *_DATE_FIELDS,
    "tags",
)


class LessonHeaderError(ValueError):
    """A lesson header field has a wrong value, or uses a name MEHO keeps."""


def normalise_lesson_header(
    metadata: Mapping[str, Any],
    *,
    allowed_reserved: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Check the header fields in *metadata* and return a copy with dates as text.

    Fields that are not part of the header pass through unchanged.
    *allowed_reserved* lists the reserved names the caller lets through
    because MEHO overwrites or uses them itself.
    """
    refused = sorted((RESERVED_NAMES & metadata.keys()) - allowed_reserved)
    if refused:
        raise LessonHeaderError(
            f"the metadata name(s) {', '.join(refused)} are kept by MEHO and cannot be used"
            " in the header; remove them"
        )
    result = dict(metadata)
    for name, allowed in _ENUM_FIELDS.items():
        if name in result:
            _check_enum(name, result[name], allowed)
    for name in _TEXT_FIELDS:
        if name in result:
            result[name] = _as_text(name, result[name])
    for name in _DATE_FIELDS:
        if name in result:
            result[name] = _as_iso_date(name, result[name])
    if "tags" in result:
        _check_tags(result["tags"])
    return result


def add_candidate_expiry(
    metadata: dict[str, Any],
    *,
    existing: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """Return *metadata* with the default expiry of a candidate added.

    Only a ``status`` of ``candidate`` changes anything. It gets a
    ``learned_at`` (the one the stored entry has, else *now*) and, when
    no ``expires_at`` is given, an ``expires_at`` six months later. A
    re-write of the same note keeps the same dates. Any other status,
    and a note with no status, is returned unchanged.
    """
    if metadata.get("status") != "candidate":
        return metadata
    result = dict(metadata)
    if result.get("learned_at") is None:
        stored = existing.get("learned_at") if existing is not None else None
        result["learned_at"] = stored if isinstance(stored, str) else now.isoformat()
    if result.get("expires_at") is None:
        learned = _parse_iso(result["learned_at"])
        result["expires_at"] = _add_months(learned, CANDIDATE_EXPIRY_MONTHS).isoformat()
    return result


def header_expires_at(metadata: Mapping[str, Any] | None) -> datetime | None:
    """Return the header's ``expires_at`` as a datetime.

    ``None`` when *metadata* has no ``expires_at``, or a value that is
    not a date: :func:`normalise_lesson_header` reports a wrong value.
    """
    if metadata is None:
        return None
    try:
        text = _as_iso_date("expires_at", metadata.get("expires_at"))
    except LessonHeaderError:
        return None
    return None if text is None else _parse_iso(text)


def header_rows(metadata: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(field, value as text)`` for each header field *metadata* has.

    For a page that shows the header. An empty value is left out; tags
    are joined with commas.
    """
    rows: list[tuple[str, str]] = []
    for name in _SHOWN_FIELDS:
        value = metadata.get(name)
        if value is None or value == "" or value == []:
            continue
        text = ", ".join(str(tag) for tag in value) if isinstance(value, list) else str(value)
        rows.append((name, text))
    return rows


def _check_enum(name: str, value: object, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise LessonHeaderError(
            f"{name} must be one of {', '.join(sorted(allowed))}; got {value!r}"
        )


def _as_text(name: str, value: object) -> str:
    # A version such as 9.0 is a number in YAML; keep it as text.
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise LessonHeaderError(f"{name} must be text; got {value!r}")
    text = str(value).strip()
    if not text:
        raise LessonHeaderError(f"{name} must not be empty")
    return text


def _check_tags(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(tag, str) and tag for tag in value):
        raise LessonHeaderError("tags must be a list of text values")


def _as_iso_date(name: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).isoformat()
    if isinstance(value, str):
        try:
            return _parse_iso(value).isoformat()
        except ValueError:
            pass
    raise LessonHeaderError(f"{name} must be a date in ISO 8601 form (2026-11-03); got {value!r}")


def _parse_iso(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _add_months(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + (moment.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    # Clip the day to the last day of the new month (31 Aug + 6 months).
    for day in (moment.day, 30, 29, 28):
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover
