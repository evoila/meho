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
  the limits allow. The gzip stream is unpacked with a byte limit before
  tar reads any header, so tar metadata (for example a large PAX header)
  cannot grow past the limit either;
* an archive with no file.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import zlib
from collections.abc import Mapping
from typing import Final

__all__ = ["ArchiveError", "read_archive", "write_archive"]

_MAX_PATH_CHARS: Final[int] = 512

#: Tar bytes one entry may add on top of its file bytes: its header, the
#: padding of its data, and an extended header for a long path.
_ENTRY_OVERHEAD: Final[int] = 4 * tarfile.BLOCKSIZE + 2 * _MAX_PATH_CHARS

#: The end blocks and the padding of the last tar record.
_END_OVERHEAD: Final[int] = 2 * tarfile.RECORDSIZE


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


def _gunzip(data: bytes, limit: int) -> bytes:
    """Unpack the gzip *data*, refusing more than *limit* bytes."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            tar = stream.read(limit + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveError(
            "archive_unreadable", "the upload is not a readable gzip tar archive"
        ) from exc
    if len(tar) > limit:
        raise ArchiveError(
            "archive_too_large", f"the archive unpacks to more than {limit} tar bytes"
        )
    return tar


def read_archive(data: bytes, *, max_files: int, max_unpacked_bytes: int) -> dict[str, bytes]:
    """Return ``{path: bytes}`` of every regular file in the gzip tar *data*."""
    tar = _gunzip(data, max_unpacked_bytes + max_files * _ENTRY_OVERHEAD + _END_OVERHEAD)
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as archive:
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
