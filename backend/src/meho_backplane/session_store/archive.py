# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read an uploaded session archive and write the stored one.

An upload is one gzip-compressed tar archive. It holds the session's
files with paths relative to the archive root, for Claude Code the
transcript ``<session-id>.jsonl`` and the folder ``<session-id>/``.

:func:`read_archive` never writes to disk. It refuses, with
:class:`ArchiveError`:

* a body that is not a gzip tar archive, or is cut off;
* a member that is not a regular file or a folder (links, devices);
* a path that is absolute, has a ``..`` part, a backslash or a control
  character, or that comes twice;
* more entries (files, folders, any member) or more unpacked bytes than
  the limits allow;
* an archive with no file.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping
from typing import Final

__all__ = ["ArchiveError", "read_archive", "write_archive"]

_MAX_PATH_CHARS: Final[int] = 512


class ArchiveError(ValueError):
    """The uploaded archive is not acceptable. ``reason`` is a short code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _clean_path(name: str) -> str:
    path = name.removeprefix("./")
    parts = path.split("/")
    bad = (
        not path
        or len(path) > _MAX_PATH_CHARS
        or path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    )
    if bad:
        raise ArchiveError("archive_bad_path", f"the archive has a path MEHO refuses: {name!r}")
    return path


def read_archive(data: bytes, *, max_files: int, max_unpacked_bytes: int) -> dict[str, bytes]:
    """Return ``{path: bytes}`` of every regular file in the gzip tar *data*."""
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for count, member in enumerate(archive, start=1):
                # Every entry counts toward the limit, folders too.
                if count > max_files:
                    raise ArchiveError(
                        "archive_too_large", f"the archive has more than {max_files} entries"
                    )
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ArchiveError(
                        "archive_bad_member",
                        f"the archive has a member that is not a file: {member.name!r}",
                    )
                path = _clean_path(member.name)
                if path in files:
                    raise ArchiveError("archive_bad_path", f"the archive has {path!r} twice")
                total += member.size
                if total > max_unpacked_bytes:
                    raise ArchiveError(
                        "archive_too_large",
                        f"the archive unpacks to more than {max_unpacked_bytes} bytes",
                    )
                handle = archive.extractfile(member)
                files[path] = handle.read() if handle is not None else b""
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ArchiveError(
            "archive_unreadable", "the upload is not a readable gzip tar archive"
        ) from exc
    if not files:
        raise ArchiveError("archive_empty", "the archive has no file")
    return files


def write_archive(files: Mapping[str, bytes]) -> bytes:
    """Return a gzip tar archive with *files*, in path order, with fixed metadata."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(files):
            content = files[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()
