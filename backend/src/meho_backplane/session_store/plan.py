# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Compare an upload with what is stored, and find the new tail of each file.

A laptop sends the whole current files of a session every time. A session
that is still open grows only at the end of its files, so MEHO stores only
the new bytes: the tail. For each file of the upload, against the stored
size and SHA-256 hash of that file (before the secret check):

* **New file** -- the whole file is the tail.
* **Same size and hash** -- nothing new.
* **Same as an older stored state** -- a retry of an older upload; nothing
  new, and nothing is overwritten.
* **Longer, and the first stored-size bytes have the stored hash** -- the
  bytes after them are the tail.
* **Anything else** -- the stored part changed. The upload is refused as a
  conflict. Nothing is overwritten.

A stored file that the upload does not carry stays as it is.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FileState", "UploadPlan", "plan_upload", "sha256_hex"]


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hash of *data* in hex."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FileState:
    """The size and the hash of the whole file at one point."""

    size: int
    sha256: str

    def as_json(self) -> dict[str, Any]:
        return {"size": self.size, "sha256": self.sha256}


@dataclass
class UploadPlan:
    """What an upload adds: the tail of each file and the new file states."""

    tails: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    """``{path: (offset, tail bytes)}`` of each file with new bytes."""
    states: dict[str, FileState] = field(default_factory=dict)
    """The state of each file after the upload: stored files plus new ones."""
    conflicts: list[str] = field(default_factory=list)
    """Paths whose stored part changed."""

    @property
    def new_bytes(self) -> int:
        return sum(len(tail) for _, tail in self.tails.values())


def _state(raw: Mapping[str, Any]) -> FileState:
    return FileState(size=int(raw["size"]), sha256=str(raw["sha256"]))


def plan_upload(
    stored: Mapping[str, Mapping[str, Any]],
    earlier: Iterable[Mapping[str, Mapping[str, Any]]],
    files: Mapping[str, bytes],
) -> UploadPlan:
    """Plan an upload of *files* against the *stored* states.

    *earlier* holds the file states after each earlier upload, so a retry of
    an older upload is known.
    """
    known: set[tuple[str, int, str]] = set()
    for states in earlier:
        for path, raw in states.items():
            state = _state(raw)
            known.add((path, state.size, state.sha256))
    plan = UploadPlan(states={path: _state(raw) for path, raw in stored.items()})
    for path in sorted(files):
        data = files[path]
        whole = FileState(size=len(data), sha256=sha256_hex(data))
        current = plan.states.get(path)
        if current is None:
            plan.tails[path] = (0, data)
            plan.states[path] = whole
        elif whole == current or (path, whole.size, whole.sha256) in known:
            continue
        elif whole.size > current.size and sha256_hex(data[: current.size]) == current.sha256:
            plan.tails[path] = (current.size, data[current.size :])
            plan.states[path] = whole
        else:
            plan.conflicts.append(path)
    return plan
