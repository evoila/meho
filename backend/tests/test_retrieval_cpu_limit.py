# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The embedding model uses one thread per CPU the container may use.

Without a cap, the ONNX runtime starts one thread per host core. In a pod
with a CPU limit those threads use up the limit and the kernel pauses the
whole process, the event loop too, so health checks time out during a
large spec ingest. These tests pin how the limit is read and that the
model is built with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meho_backplane.retrieval import cpu_limit
from meho_backplane.retrieval.cpu_limit import embedding_thread_count
from meho_backplane.retrieval.embedding import EmbeddingService


@pytest.fixture(autouse=True)
def _many_usable_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cpu_limit.os, "process_cpu_count", lambda: 64)


def _cgroup_v2(root: Path, cpu_max: str) -> Path:
    (root / "cpu.max").write_text(cpu_max + "\n")
    return root


def _cgroup_v1(root: Path, quota: str, period: str) -> Path:
    (root / "cpu").mkdir()
    (root / "cpu" / "cpu.cfs_quota_us").write_text(quota + "\n")
    (root / "cpu" / "cpu.cfs_period_us").write_text(period + "\n")
    return root


@pytest.mark.parametrize(
    ("cpu_max", "expected"),
    [
        ("100000 100000", 1),
        ("50000 100000", 1),
        ("150000 100000", 1),
        ("200000 100000", 2),
        ("400000 50000", 8),
    ],
)
def test_cgroup_v2_limit_gives_whole_cpus(tmp_path: Path, cpu_max: str, expected: int) -> None:
    assert embedding_thread_count(_cgroup_v2(tmp_path, cpu_max)) == expected


def test_cgroup_v2_without_limit_keeps_the_runtime_default(tmp_path: Path) -> None:
    assert embedding_thread_count(_cgroup_v2(tmp_path, "max 100000")) is None


@pytest.mark.parametrize("cpu_max", ["", "garbage", "100000", "abc 100000", "100000 0", "0 100000"])
def test_cgroup_v2_unreadable_value_keeps_the_runtime_default(tmp_path: Path, cpu_max: str) -> None:
    assert embedding_thread_count(_cgroup_v2(tmp_path, cpu_max)) is None


def test_cgroup_v1_limit_gives_whole_cpus(tmp_path: Path) -> None:
    assert embedding_thread_count(_cgroup_v1(tmp_path, "300000", "100000")) == 3


@pytest.mark.parametrize(("quota", "period"), [("-1", "100000"), ("x", "100000"), ("100000", "")])
def test_cgroup_v1_without_limit_keeps_the_runtime_default(
    tmp_path: Path, quota: str, period: str
) -> None:
    assert embedding_thread_count(_cgroup_v1(tmp_path, quota, period)) is None


def test_no_cgroup_files_keeps_the_runtime_default(tmp_path: Path) -> None:
    assert embedding_thread_count(tmp_path) is None


def test_limit_never_exceeds_the_cpus_the_process_may_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cpu_limit.os, "process_cpu_count", lambda: 2)
    assert embedding_thread_count(_cgroup_v2(tmp_path, "800000 100000")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("threads", [1, None])
async def test_model_is_built_with_the_container_thread_count(
    tmp_path: Path, threads: int | None
) -> None:
    service = EmbeddingService(model_name="BAAI/bge-small-en-v1.5", cache_dir=str(tmp_path))
    stub_model = MagicMock()
    stub_model.embed.side_effect = lambda texts: iter([[0.0] * 384 for _ in texts])
    seen: dict[str, Any] = {}

    def fake_text_embedding(**kwargs: Any) -> MagicMock:
        seen.update(kwargs)
        return stub_model

    with (
        patch(
            "meho_backplane.retrieval.embedding.embedding_thread_count",
            return_value=threads,
        ),
        patch("fastembed.TextEmbedding", side_effect=fake_text_embedding),
    ):
        await service.encode_one("text")

    assert seen["threads"] == threads
