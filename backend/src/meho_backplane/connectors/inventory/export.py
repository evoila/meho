# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The answer of ``inventory.export``, the same for every file format.

A format mapping (for example :mod:`.azure_migrate_csv`) fills it. The
answer holds no rule of one target: the format name says which file it is.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "ExportResult",
    "LeftOutReason",
    "LeftOutRecord",
    "RenamedRecord",
]

LeftOutReason = Literal["missing_required_field", "same_machine_as", "over_file_limit"]


class LeftOutRecord(BaseModel):
    """A record that is not in the file, and why."""

    source_id: str = Field(description="The record's source_id.")
    name: str | None = Field(description="The record's name, when it has one.")
    reason: LeftOutReason = Field(
        description=(
            "Why it is not in the file. missing_required_field: the format needs a value "
            "the record lacks. same_machine_as: the machine is already in the file through "
            "another record. over_file_limit: the file is full."
        )
    )
    missing: dict[str, str] = Field(
        default_factory=dict,
        description="For missing_required_field: the field and the reason it is unknown.",
    )
    same_as: str | None = Field(
        default=None, description="For same_machine_as: the source_id already in the file."
    )


class RenamedRecord(BaseModel):
    """A record whose name in the file differs from its name in the inventory."""

    source_id: str = Field(description="The record's source_id.")
    original_name: str = Field(description="The name in the inventory.")
    name_in_file: str = Field(description="The name in the file.")
    reasons: list[Literal["repeated_name", "cleaned_text"]] = Field(
        description="repeated_name: another record has the name. cleaned_text: line breaks "
        "or a leading formula character were changed."
    )


class ExportResult(BaseModel):
    """The file in one format, and what was changed or left out."""

    format: str = Field(description="The format of the file, as given in the call.")
    lines: list[str] = Field(
        description=(
            "The file, one item per line without the line end. Join the items with "
            "line_end to get the file. A stored result pages this list with result_query."
        )
    )
    line_end: str = Field(description="The line end of the file, for example CRLF.")
    row_count: int = Field(description="Records in the file (a header line not counted).")
    columns: list[str] = Field(description="The header row; empty when the format has none.")
    left_out: list[LeftOutRecord] = Field(description="Records not in the file.")
    renamed: list[RenamedRecord] = Field(description="Records with a changed name.")
    skipped_records: dict[str, int] = Field(
        description="Records of kinds the format has no place for, per kind."
    )
    notes: list[str] = Field(description="Other things to know, for example cut disk lists.")
