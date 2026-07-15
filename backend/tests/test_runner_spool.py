# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the runner on-disk retry spool (#2497).

Covers: an atomic write leaves exactly one readable JSON file (no
lingering ``.tmp``); drain returns batches oldest-first; and exceeding
``max_files`` drops the oldest file with a warning.
"""

from __future__ import annotations

from pathlib import Path

from structlog.testing import capture_logs

from meho_backplane.runner.spool import ResultSpool
from meho_backplane.runner.wire import RunnerResult, RunnerResultBatch


def _batch(runner_id: str, *, uid: str) -> RunnerResultBatch:
    return RunnerResultBatch(
        runner_id=runner_id,
        results=[RunnerResult(result_uid=uid, check_ref=uid, op_id="net.tcp_check", status="ok")],
    )


def _spool_dir(tmp_path: Path) -> Path:
    # A dedicated subdir so unrelated test artifacts in tmp_path (e.g. the
    # conftest's sqlite default.db) never leak into directory assertions.
    return tmp_path / "spool"


def test_write_batch_is_atomic_single_file(tmp_path: Path) -> None:
    spool_dir = _spool_dir(tmp_path)
    spool = ResultSpool(spool_dir, max_files=100)
    path = spool.write_batch(_batch("r1", uid="a"))

    assert path.exists()
    assert path.suffix == ".json"
    # Exactly one file, no lingering .tmp sibling.
    all_files = sorted(p.name for p in spool_dir.iterdir())
    assert all_files == [path.name]
    assert not any(name.endswith(".tmp") for name in all_files)

    loaded = spool.load_oldest_first()
    assert len(loaded) == 1
    assert loaded[0][1].results[0].result_uid == "a"


def test_drain_is_oldest_first(tmp_path: Path) -> None:
    spool = ResultSpool(_spool_dir(tmp_path), max_files=100)
    for uid in ("a", "b", "c"):
        spool.write_batch(_batch("r1", uid=uid))

    loaded = spool.load_oldest_first()
    uids = [batch.results[0].result_uid for _path, batch in loaded]
    assert uids == ["a", "b", "c"]

    # Removing the oldest leaves the rest, still oldest-first.
    spool.remove(loaded[0][0])
    remaining = [b.results[0].result_uid for _p, b in spool.load_oldest_first()]
    assert remaining == ["b", "c"]


def test_overflow_drops_oldest_with_warning(tmp_path: Path) -> None:
    spool = ResultSpool(_spool_dir(tmp_path), max_files=2)
    spool.write_batch(_batch("r1", uid="a"))
    spool.write_batch(_batch("r1", uid="b"))

    with capture_logs() as logs:
        spool.write_batch(_batch("r1", uid="c"))

    remaining = [b.results[0].result_uid for _p, b in spool.load_oldest_first()]
    # Cap is 2: the oldest ("a") was dropped to make room for "c".
    assert remaining == ["b", "c"]
    assert any(entry["event"] == "runner_spool_overflow_drop" for entry in logs)


def test_unreadable_file_is_skipped(tmp_path: Path) -> None:
    spool_dir = _spool_dir(tmp_path)
    spool = ResultSpool(spool_dir, max_files=100)
    spool.write_batch(_batch("r1", uid="a"))
    # A truncated/corrupt spool file (e.g. a crashed write) must not wedge
    # the drain — it is skipped, the valid batch still loads. The all-zero
    # epoch prefix sorts it first, so it is attempted before the real batch.
    (spool_dir / "0000000000000-000000-deadbeef.json").write_text("{not json", encoding="utf-8")

    loaded = spool.load_oldest_first()
    uids = [b.results[0].result_uid for _p, b in loaded]
    assert uids == ["a"]
