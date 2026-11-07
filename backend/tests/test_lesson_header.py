# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :mod:`meho_backplane.lesson_header`."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from meho_backplane.lesson_header import (
    LessonHeaderError,
    add_candidate_expiry,
    normalise_lesson_header,
)

_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def test_a_full_header_passes_and_other_keys_are_kept() -> None:
    header = {
        "type": "observation",
        "applies_to": "product-and-version",
        "status": "candidate",
        "product": "vcenter",
        "version": 9.0,
        "source": "lab field note",
        "author": "ops",
        "summary": "short",
        "tags": ["a", "b"],
        "learned_at": "2026-11-03",
        "other": {"x": 1},
    }
    result = normalise_lesson_header(header)
    assert result["version"] == "9.0"
    assert result["learned_at"] == "2026-11-03T00:00:00+00:00"
    assert result["other"] == {"x": 1}
    assert header["learned_at"] == "2026-11-03"  # the input is not changed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "story"),
        ("type", None),
        ("type", ["fact"]),
        ("applies_to", "everyone"),
        ("status", "Confirmed"),
        ("status", ""),
    ],
)
def test_a_wrong_enum_value_is_refused_with_the_allowed_values(field: str, value: object) -> None:
    with pytest.raises(LessonHeaderError) as exc:
        normalise_lesson_header({field: value})
    assert field in str(exc.value)
    assert "must be one of" in str(exc.value)


@pytest.mark.parametrize("name", ["slug", "provenance", "kind", "scope"])
def test_names_kept_by_meho_are_refused(name: str) -> None:
    with pytest.raises(LessonHeaderError, match=name):
        normalise_lesson_header({name: "x"})


def test_a_reserved_name_the_caller_allows_passes() -> None:
    assert normalise_lesson_header({"slug": "x"}, allowed_reserved=frozenset({"slug"})) == {
        "slug": "x"
    }


@pytest.mark.parametrize("value", ["not a date", 5, True, "2026-13-45"])
def test_a_bad_date_is_refused(value: object) -> None:
    with pytest.raises(LessonHeaderError, match="expires_at"):
        normalise_lesson_header({"expires_at": value})


def test_date_objects_from_yaml_become_iso_text() -> None:
    result = normalise_lesson_header(
        {"learned_at": date(2026, 11, 3), "confirmed_at": datetime(2026, 11, 4, 8, 30)}
    )
    assert result["learned_at"] == "2026-11-03T00:00:00+00:00"
    assert result["confirmed_at"] == "2026-11-04T08:30:00+00:00"


@pytest.mark.parametrize("value", ["a", [1], ["a", ""], {"a": 1}])
def test_tags_must_be_a_list_of_text(value: object) -> None:
    with pytest.raises(LessonHeaderError, match="tags"):
        normalise_lesson_header({"tags": value})


@pytest.mark.parametrize("field", ["product", "summary"])
@pytest.mark.parametrize("value", ["", "  ", [], True])
def test_text_fields_refuse_empty_and_wrong_types(field: str, value: object) -> None:
    with pytest.raises(LessonHeaderError, match=field):
        normalise_lesson_header({field: value})


def test_a_candidate_gets_six_months() -> None:
    result = add_candidate_expiry({"status": "candidate"}, existing=None, now=_NOW)
    assert result["learned_at"] == _NOW.isoformat()
    assert result["expires_at"] == "2027-05-07T12:00:00+00:00"


def test_a_candidate_counts_from_its_own_learned_at() -> None:
    result = add_candidate_expiry(
        {"status": "candidate", "learned_at": "2026-08-31T00:00:00+00:00"},
        existing=None,
        now=_NOW,
    )
    # 31 August plus six months ends on the last day of February.
    assert result["expires_at"] == "2027-02-28T00:00:00+00:00"


def test_a_given_expires_at_is_kept() -> None:
    result = add_candidate_expiry(
        {"status": "candidate", "expires_at": "2026-12-01T00:00:00+00:00"},
        existing=None,
        now=_NOW,
    )
    assert result["expires_at"] == "2026-12-01T00:00:00+00:00"


def test_a_rewrite_keeps_the_stored_learned_at() -> None:
    result = add_candidate_expiry(
        {"status": "candidate"},
        existing={"learned_at": "2026-10-01T00:00:00+00:00"},
        now=_NOW,
    )
    assert result["learned_at"] == "2026-10-01T00:00:00+00:00"
    assert result["expires_at"] == "2027-04-01T00:00:00+00:00"


@pytest.mark.parametrize("metadata", [{"status": "confirmed"}, {"type": "fact"}, {}])
def test_other_statuses_and_no_status_get_no_expiry(metadata: dict[str, object]) -> None:
    assert add_candidate_expiry(metadata, existing=None, now=_NOW) == metadata
