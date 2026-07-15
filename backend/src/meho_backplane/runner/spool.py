# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""On-disk retry spool for result batches that failed to POST.

When the uplink to central is down, a satellite runner keeps executing
locally and persists each un-posted :class:`RunnerResultBatch` to a plain
JSON file. The next tick drains the spool oldest-first, stopping at the
first re-post failure so ordering is preserved and a still-down uplink
does not spin.

Design:

* **One file per batch**, named ``<epoch_ms>-<seq>-<uuid4>.json`` so a
  lexical sort is chronological. ``<seq>`` is a per-instance monotonic
  counter that disambiguates batches written within the same millisecond
  (deterministic drain / drop order); ``<epoch_ms>`` keeps files written
  across process restarts ordered.
* **Atomic writes** — content is written to a ``.tmp`` sibling then
  :func:`os.replace`\\d into place, so a crash mid-write never leaves a
  half-written batch a later drain would choke on.
* **Bounded** — before each write, oldest files are dropped until adding
  one more stays within ``max_files``; each drop logs a warning. A runner
  partitioned for a long time sheds the oldest results rather than filling
  the disk.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import time
import uuid
from pathlib import Path

import structlog

from meho_backplane.runner.wire import RunnerResult, RunnerResultBatch

__all__ = ["ExecutedCommandStore", "ResultSpool"]

_log = structlog.get_logger(__name__)

_TMP_SUFFIX = ".tmp"


class ResultSpool:
    """A directory of un-posted result batches, drained oldest-first."""

    def __init__(self, spool_dir: str | os.PathLike[str], *, max_files: int) -> None:
        self._dir = Path(spool_dir)
        self._max_files = max_files
        self._seq = itertools.count()

    def _list_files(self) -> list[Path]:
        """Return spooled batch files, oldest-first. Excludes ``.tmp``."""
        if not self._dir.exists():
            return []
        return sorted(p for p in self._dir.glob("*.json") if not p.name.endswith(_TMP_SUFFIX))

    def write_batch(self, batch: RunnerResultBatch) -> Path:
        """Persist *batch* atomically and return its file path."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._enforce_cap()
        name = f"{int(time.time() * 1000):013d}-{next(self._seq):06d}-{uuid.uuid4().hex}.json"
        final = self._dir / name
        tmp = self._dir / (name + _TMP_SUFFIX)
        tmp.write_text(batch.model_dump_json(), encoding="utf-8")
        os.replace(tmp, final)
        _log.info("runner_spool_write", path=str(final), results=len(batch.results))
        return final

    def load_oldest_first(self) -> list[tuple[Path, RunnerResultBatch]]:
        """Return ``(path, batch)`` pairs, oldest-first.

        Unreadable / corrupt files are logged and skipped rather than
        stalling the drain — a truncated file from a crashed write should
        not wedge the spool forever.
        """
        out: list[tuple[Path, RunnerResultBatch]] = []
        for path in self._list_files():
            try:
                batch = RunnerResultBatch.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                _log.warning("runner_spool_unreadable", path=str(path), error=str(exc))
                continue
            out.append((path, batch))
        return out

    def remove(self, path: Path) -> None:
        """Delete a spooled file after it has been re-posted."""
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _enforce_cap(self) -> None:
        """Drop oldest files until one more write stays within the cap."""
        files = self._list_files()
        while len(files) >= self._max_files:
            oldest = files.pop(0)
            with contextlib.suppress(FileNotFoundError):
                oldest.unlink()
            _log.warning(
                "runner_spool_overflow_drop",
                path=str(oldest),
                max_files=self._max_files,
            )


class ExecutedCommandStore:
    """Persisted record-before-execute set of gateway command ids (#2500).

    The runner's half of the at-most-once guarantee: a gateway command's
    UUID is the request id, and it is recorded on disk **before** the
    handler is dispatched, so a crash between record and execute still
    refuses re-execution on redelivery. The produced :class:`RunnerResult`
    is persisted alongside the marker so a redelivery whose central
    consumption latch has not yet closed can be **re-submitted** rather than
    re-run (the centre's ``consumed_at`` latch makes re-submission safe).

    One file per command id, ``<command_id>.json``, in a dedicated subdir of
    the runner's spool directory:

    * :meth:`record` atomically creates an empty marker (``O_CREAT|O_EXCL``),
      returning ``True`` only for the caller that created it — the
      record-before-execute step.
    * :meth:`store_result` atomically overwrites the marker with the result
      JSON (tmp + :func:`os.replace`), after the handler ran.
    * :meth:`load_result` returns the stored result, or ``None`` when the id
      is unknown **or** the marker is still empty (recorded but the execution
      never produced a stored result — e.g. a crash mid-dispatch).
    """

    def __init__(self, executed_dir: str | os.PathLike[str]) -> None:
        self._dir = Path(executed_dir)

    def _path(self, command_id: str) -> Path:
        return self._dir / f"{command_id}.json"

    def record(self, command_id: str) -> bool:
        """Atomically record *command_id* before execution.

        Returns ``True`` when this call created the marker (proceed to
        execute), ``False`` when it already existed (a duplicate delivery —
        never re-execute).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path(command_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.close(fd)
        return True

    def has(self, command_id: str) -> bool:
        """Whether *command_id* has been recorded (executed or in-flight)."""
        return self._path(command_id).exists()

    def store_result(self, command_id: str, result: RunnerResult) -> None:
        """Persist *result* for a recorded *command_id* (atomic overwrite)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        final = self._path(command_id)
        tmp = final.with_suffix(final.suffix + _TMP_SUFFIX)
        tmp.write_text(result.model_dump_json(), encoding="utf-8")
        os.replace(tmp, final)

    def load_result(self, command_id: str) -> RunnerResult | None:
        """Return the stored result for *command_id*, or ``None``.

        ``None`` covers both an unknown id and a recorded-but-empty marker
        (recorded before execute, no result stored yet). A corrupt file is
        logged and treated as absent rather than raising into the tick loop.
        """
        path = self._path(command_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not raw:
            return None
        try:
            return RunnerResult.model_validate_json(raw)
        except ValueError as exc:
            _log.warning("runner_executed_store_unreadable", path=str(path), error=str(exc))
            return None
