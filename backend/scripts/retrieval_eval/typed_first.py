# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-first discovery test: does search find the typed operation first? (#4717)

The task list (``typed_first_tasks.yaml``, next to this module) holds 40
ops requests. Each has one known answer: a typed or multi-step operation.
This module runs the **search-only** part of the test. No model runs.
For each task it searches twice with the request as the search text:

* ``raw_visible``: raw operations (made from a vendor API file) are
  candidates, as with the switch ``SEARCH_TYPED_FIRST`` off;
* ``raw_hidden``: only typed and multi-step operations are candidates, as
  with the switch on (#4712).

It records the rank of the first correct operation, and whether it is
first (top-1) or in the first three (top-3). A task whose connector this
install cannot search is "not run" and does not count.

The model run of the same tasks (tokens, steps, wrong picks) belongs to
the benchmark lane, which reads the same task file.

Run on an install (from ``backend/``, with the install's database
settings)::

    uv run python -m scripts.retrieval_eval.typed_first --tenant-id <tenant-uuid>
    uv run python -m scripts.retrieval_eval.typed_first --tenant-id <tenant-uuid> \\
        --tasks <private-task-file.yaml> --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scripts.retrieval_eval.corpus import CorpusValidationError
from scripts.retrieval_eval.operation_search import (
    SearchOperationsCallable,
    search_corpus_operations,
)

__all__ = [
    "SEARCH_DEPTH",
    "TASKS_FILE_VERSION",
    "ModeResult",
    "ModeSummary",
    "SearchOnlyReport",
    "TaskResult",
    "TypedFirstTask",
    "TypedFirstTasks",
    "load_typed_first_tasks",
    "main",
    "run_search_only",
]

#: Version of the task file this code reads. Bump it with the YAML when a
#: request or an answer changes.
TASKS_FILE_VERSION: Final[int] = 1

#: How many hits each search asks for. A correct operation further down
#: has no rank.
SEARCH_DEPTH: Final[int] = 10

_TASKS_FILE: Final[str] = "typed_first_tasks.yaml"
_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)

Mode = Literal["raw_visible", "raw_hidden"]
_NonEmptyStr = Annotated[str, Field(min_length=1)]


class TypedFirstTask(BaseModel):
    """One ops request and its known answer."""

    model_config = _MODEL_CONFIG

    id: _NonEmptyStr
    connector_id: _NonEmptyStr
    request: _NonEmptyStr
    #: Correct operations; the first is the main answer.
    expected_op_ids: Annotated[list[_NonEmptyStr], Field(min_length=1)]


class TypedFirstTasks(BaseModel):
    """The task file: a version and the tasks, each with its own id."""

    model_config = _MODEL_CONFIG

    version: int
    tasks: Annotated[list[TypedFirstTask], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> TypedFirstTasks:
        if self.version != TASKS_FILE_VERSION:
            raise ValueError(
                f"task file version {self.version} is not the version this code reads "
                f"({TASKS_FILE_VERSION})"
            )
        seen: set[str] = set()
        for task in self.tasks:
            if task.id in seen:
                raise ValueError(f"task id {task.id!r} is used twice")
            seen.add(task.id)
        return self


def load_typed_first_tasks(path: Path | None = None) -> TypedFirstTasks:
    """Read and check a task file; the default is the public one next to this module.

    Raises :class:`CorpusValidationError` when the file is not valid, so a
    broken file never gives numbers.
    """
    if path is None:
        text = resources.files("scripts.retrieval_eval").joinpath(_TASKS_FILE).read_text("utf-8")
        source = _TASKS_FILE
    else:
        text = path.read_text(encoding="utf-8")
        source = str(path)
    try:
        return TypedFirstTasks.model_validate(yaml.safe_load(text))
    except ValidationError as exc:
        raise CorpusValidationError(f"{source} is not a valid task file: {exc}") from exc


class ModeResult(BaseModel):
    """The result of one search of one task."""

    model_config = ConfigDict(frozen=True)

    #: 1-based rank of the first correct operation; ``None`` when it is not
    #: in the first :data:`SEARCH_DEPTH` hits or the search did not run.
    rank: int | None = None
    #: The first three hits, to see what came first instead.
    top_op_ids: list[str] = Field(default_factory=list)
    not_run_reason: str | None = None

    @property
    def top_1(self) -> bool:
        return self.rank == 1

    @property
    def top_3(self) -> bool:
        return self.rank is not None and self.rank <= 3


class TaskResult(BaseModel):
    """Both searches of one task."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    connector_id: str
    raw_visible: ModeResult
    raw_hidden: ModeResult


class ModeSummary(BaseModel):
    """Totals of one search mode over the tasks that ran."""

    model_config = ConfigDict(frozen=True)

    run_count: int
    not_run_count: int
    top_1_count: int
    top_3_count: int
    #: Share of the tasks that ran; ``None`` when no task ran.
    top_1_rate: float | None
    top_3_rate: float | None


class SearchOnlyReport(BaseModel):
    """The search-only report: per task, and a summary per mode."""

    model_config = ConfigDict(frozen=True)

    tasks_file_version: int
    search_depth: int
    tasks: list[TaskResult]
    summary: dict[Mode, ModeSummary]


def _rank(op_ids: list[str], expected: list[str]) -> int | None:
    for index, op_id in enumerate(op_ids, start=1):
        if op_id in expected:
            return index
    return None


async def _search_one(
    task: TypedFirstTask,
    *,
    tenant_id: uuid.UUID,
    typed_only: bool,
    search_fn: SearchOperationsCallable,
) -> ModeResult:
    found = await search_fn(
        tenant_id=tenant_id,
        connector_id=task.connector_id,
        query=task.request,
        limit=SEARCH_DEPTH,
        typed_only=typed_only,
    )
    if found.not_run_reason is not None:
        return ModeResult(not_run_reason=found.not_run_reason)
    return ModeResult(rank=_rank(found.op_ids, task.expected_op_ids), top_op_ids=found.op_ids[:3])


def _summarise(results: list[ModeResult]) -> ModeSummary:
    ran = [r for r in results if r.not_run_reason is None]
    top_1 = sum(1 for r in ran if r.top_1)
    top_3 = sum(1 for r in ran if r.top_3)
    return ModeSummary(
        run_count=len(ran),
        not_run_count=len(results) - len(ran),
        top_1_count=top_1,
        top_3_count=top_3,
        top_1_rate=top_1 / len(ran) if ran else None,
        top_3_rate=top_3 / len(ran) if ran else None,
    )


async def run_search_only(
    tasks: TypedFirstTasks,
    *,
    tenant_id: uuid.UUID,
    search_fn: SearchOperationsCallable | None = None,
) -> SearchOnlyReport:
    """Search each task with raw operations visible and hidden; score both.

    *search_fn* defaults to the ranked search that ``search_operations``
    uses (:func:`~scripts.retrieval_eval.operation_search.search_corpus_operations`).
    Tests pass a stub.
    """
    search = search_fn if search_fn is not None else search_corpus_operations
    results: list[TaskResult] = []
    for task in tasks.tasks:
        visible = await _search_one(task, tenant_id=tenant_id, typed_only=False, search_fn=search)
        hidden = await _search_one(task, tenant_id=tenant_id, typed_only=True, search_fn=search)
        results.append(
            TaskResult(
                task_id=task.id,
                connector_id=task.connector_id,
                raw_visible=visible,
                raw_hidden=hidden,
            )
        )
    return SearchOnlyReport(
        tasks_file_version=tasks.version,
        search_depth=SEARCH_DEPTH,
        tasks=results,
        summary={
            "raw_visible": _summarise([r.raw_visible for r in results]),
            "raw_hidden": _summarise([r.raw_hidden for r in results]),
        },
    )


async def _run_and_close(tasks: TypedFirstTasks, tenant_id: uuid.UUID) -> SearchOnlyReport:
    from meho_backplane.connectors.registry import _eager_import_connectors
    from meho_backplane.db.engine import dispose_engine

    _eager_import_connectors()
    try:
        return await run_search_only(tasks, tenant_id=tenant_id)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    parser.add_argument("--tasks", type=Path, help="a task file; default: the public one")
    parser.add_argument("--out", type=Path, help="write the JSON report here; default: stdout")
    args = parser.parse_args(argv)

    report = asyncio.run(_run_and_close(load_typed_first_tasks(args.tasks), args.tenant_id))
    text = json.dumps(report.model_dump(mode="json"), indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
