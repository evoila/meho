# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write and read the provenance block stored in kb entry metadata.

The service stamps :data:`~meho_backplane.kb.schemas.META_PROVENANCE`
on every write so a reader can tell where an entry came from:

* ``source_kind="directory"`` -- written by a directory ingest;
  ``relative_path`` is the file path relative to the ingest
  directory (POSIX separators).
* ``source_kind="direct"`` -- written by an explicit create or
  replace (REST, MCP, console); ``relative_path`` is ``None``.

``ingested_at`` is the UTC time of the write. No absolute host path is
ever stored. Reading is defensive: rows written before this block
existed, or rows whose ``provenance`` key holds something else (an old
front-matter key, for example), read back as ``state="legacy"``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from meho_backplane.kb.schemas import META_PROVENANCE, KbProvenance

__all__ = ["read_provenance", "stamp_provenance"]

_SourceKind = Literal["directory", "direct"]


class _StoredProvenance(BaseModel):
    """The exact shape the service writes under ``metadata["provenance"]``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_kind: _SourceKind
    relative_path: str | None
    ingested_at: datetime

    @field_validator("relative_path")
    @classmethod
    def _must_be_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise ValueError("relative_path must not be absolute")
        if ".." in PurePosixPath(value).parts:
            raise ValueError("relative_path must not leave the ingest directory")
        return value


def stamp_provenance(
    metadata: dict[str, object],
    *,
    source_kind: _SourceKind,
    relative_path: str | None,
    ingested_at: datetime,
) -> dict[str, object]:
    """Return a copy of *metadata* with a fresh provenance block.

    Any ``provenance`` value already in *metadata* (from a caller or a
    front-matter block) is replaced: the service is the only writer of
    this key. Raises :class:`ValueError` when *relative_path* is
    absolute or escapes the ingest directory.
    """
    stored = _StoredProvenance(
        source_kind=source_kind,
        relative_path=relative_path,
        ingested_at=ingested_at,
    )
    stamped = dict(metadata)
    stamped[META_PROVENANCE] = stored.model_dump(mode="json")
    return stamped


def read_provenance(metadata: dict[str, object]) -> KbProvenance:
    """Read the provenance block from *metadata*; ``legacy`` when absent or malformed."""
    raw = metadata.get(META_PROVENANCE)
    if raw is None:
        return KbProvenance(state="legacy")
    try:
        stored = _StoredProvenance.model_validate(raw)
    except ValidationError:
        return KbProvenance(state="legacy")
    return KbProvenance(
        state="recorded",
        source_kind=stored.source_kind,
        relative_path=stored.relative_path,
        ingested_at=stored.ingested_at,
    )
