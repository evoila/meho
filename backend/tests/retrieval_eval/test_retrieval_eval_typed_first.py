# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The typed-first discovery test (#4717): task file and search-only run.

* The public task file holds 40 tasks. Each answer is a typed or
  multi-step operation that MEHO registers for the task's connector.
* A broken task file fails at load time.
* The search-only run searches each task twice (raw operations visible
  and hidden), records the rank of the first correct answer and sums up
  top-1 and top-3. A task that could not run does not count.
* Against the SQLite search of the test suite, hiding raw operations
  really leaves them out of the candidates.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from scripts.retrieval_eval.corpus import CorpusValidationError
from scripts.retrieval_eval.operation_search import OperationSearchResult
from scripts.retrieval_eval.typed_first import (
    SEARCH_DEPTH,
    SearchOnlyReport,
    TypedFirstTask,
    TypedFirstTasks,
    load_typed_first_tasks,
    main,
    run_search_only,
)
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


# ---------------------------------------------------------------------------
# The public task file
# ---------------------------------------------------------------------------


def test_public_task_file_has_40_tasks() -> None:
    tasks = load_typed_first_tasks()

    assert len(tasks.tasks) == 40
    assert len({t.id for t in tasks.tasks}) == 40


@pytest.mark.asyncio
async def test_every_answer_is_a_registered_typed_operation_of_its_connector() -> None:
    await run_typed_op_registrars(embedding_service=_stub_embedding_service())
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(
                    EndpointDescriptor.product,
                    EndpointDescriptor.version,
                    EndpointDescriptor.impl_id,
                    EndpointDescriptor.op_id,
                ).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.source_kind.in_(("typed", "composite")),
                )
            )
        ).all()
    registered = {(r.product, r.version, r.impl_id, r.op_id) for r in rows}

    missing = [
        f"{task.id}: {op_id}"
        for task in load_typed_first_tasks().tasks
        for op_id in task.expected_op_ids
        if (*parse_connector_id(task.connector_id), op_id) not in registered
    ]
    assert missing == []


# ---------------------------------------------------------------------------
# A broken task file fails at load time
# ---------------------------------------------------------------------------

_ONE_TASK = """\
  - id: a
    connector_id: k8s-1.x
    request: list pods
    expected_op_ids: [k8s.pod.list]
"""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"version: 2\ntasks:\n{_ONE_TASK}", "version 2"),
        (f"version: 1\ntasks:\n{_ONE_TASK}{_ONE_TASK}", "used twice"),
        ("version: 1\ntasks: []\n", "at least 1 item"),
        (
            "version: 1\ntasks:\n  - id: a\n    connector_id: k8s-1.x\n"
            "    request: list pods\n    expected_op_ids: []\n",
            "at least 1 item",
        ),
        (
            "version: 1\ntasks:\n  - id: a\n    connector_id: k8s-1.x\n"
            "    request: ''\n    expected_op_ids: [k8s.pod.list]\n",
            "at least 1 character",
        ),
        (f"version: 1\ntasks:\n{_ONE_TASK}    expected_op_id: x\n", "Extra inputs"),
        ("version: '1'\ntasks:\n" + _ONE_TASK, "valid integer"),
    ],
)
def test_broken_task_file_is_refused(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(CorpusValidationError, match=message):
        load_typed_first_tasks(path)


def test_task_file_from_a_path_loads(tmp_path: Path) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(f"version: 1\ntasks:\n{_ONE_TASK}", encoding="utf-8")

    tasks = load_typed_first_tasks(path)

    assert tasks.tasks == [
        TypedFirstTask(
            id="a", connector_id="k8s-1.x", request="list pods", expected_op_ids=["k8s.pod.list"]
        )
    ]


# ---------------------------------------------------------------------------
# The search-only run, with a stub search
# ---------------------------------------------------------------------------


def _tasks(*tasks: tuple[str, list[str]]) -> TypedFirstTasks:
    return TypedFirstTasks(
        version=1,
        tasks=[
            TypedFirstTask(
                id=tid, connector_id="c-1.x", request=f"request {tid}", expected_op_ids=e
            )
            for tid, e in tasks
        ],
    )


class _ScriptedSearch:
    """Answers per (task request, typed_only); records every call."""

    def __init__(self, answers: dict[tuple[str, bool], OperationSearchResult]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> OperationSearchResult:
        self.calls.append(kwargs)
        return self.answers[(kwargs["query"], kwargs["typed_only"])]


@pytest.mark.asyncio
async def test_search_only_ranks_both_modes_and_sums_up() -> None:
    hits = OperationSearchResult
    search = _ScriptedSearch(
        {
            # First with raw hidden only.
            ("request a", False): hits(op_ids=["raw.1", "raw.2", "raw.3", "op.a"]),
            ("request a", True): hits(op_ids=["op.a", "op.x"]),
            # Second correct answer counts; third place is top-3.
            ("request b", False): hits(op_ids=["raw.1", "raw.2", "op.b2"]),
            ("request b", True): hits(op_ids=["op.b2"]),
            # Not found at all, and an empty answer.
            ("request c", False): hits(op_ids=["raw.1"]),
            ("request c", True): hits(op_ids=[]),
            # Not run: does not count in either mode.
            ("request d", False): hits(not_run_reason="no operations"),
            ("request d", True): hits(not_run_reason="no operations"),
        }
    )
    tasks = _tasks(("a", ["op.a"]), ("b", ["op.b1", "op.b2"]), ("c", ["op.c"]), ("d", ["op.d"]))

    report = await run_search_only(tasks, tenant_id=_TENANT, search_fn=search)

    by_id = {r.task_id: r for r in report.tasks}
    assert by_id["a"].raw_visible.rank == 4
    assert by_id["a"].raw_visible.top_op_ids == ["raw.1", "raw.2", "raw.3"]
    assert by_id["a"].raw_hidden.rank == 1
    assert by_id["b"].raw_visible.rank == 3
    assert by_id["b"].raw_visible.top_3 and not by_id["b"].raw_visible.top_1
    assert by_id["c"].raw_visible.rank is None
    assert by_id["c"].raw_hidden.rank is None
    assert by_id["c"].raw_hidden.top_op_ids == []
    assert by_id["d"].raw_visible.not_run_reason == "no operations"
    assert by_id["d"].raw_visible.rank is None

    visible = report.summary["raw_visible"]
    assert (visible.run_count, visible.not_run_count) == (3, 1)
    assert (visible.top_1_count, visible.top_3_count) == (0, 1)
    assert visible.top_1_rate == 0.0
    assert visible.top_3_rate == pytest.approx(1 / 3)
    hidden = report.summary["raw_hidden"]
    assert (hidden.top_1_count, hidden.top_3_count) == (2, 2)
    assert hidden.top_1_rate == pytest.approx(2 / 3)

    assert {(c["query"], c["typed_only"]) for c in search.calls} == set(search.answers)
    assert {c["limit"] for c in search.calls} == {SEARCH_DEPTH}
    assert {c["tenant_id"] for c in search.calls} == {_TENANT}
    assert {c["connector_id"] for c in search.calls} == {"c-1.x"}


@pytest.mark.asyncio
async def test_search_only_with_no_task_run_has_no_rates() -> None:
    async def nothing_runs(**_: Any) -> OperationSearchResult:
        return OperationSearchResult(not_run_reason="no operations")

    report = await run_search_only(
        _tasks(("a", ["op.a"])), tenant_id=_TENANT, search_fn=nothing_runs
    )

    for summary in report.summary.values():
        assert (summary.run_count, summary.not_run_count) == (0, 1)
        assert summary.top_1_rate is None
        assert summary.top_3_rate is None


@pytest.mark.asyncio
async def test_search_error_is_not_a_miss() -> None:
    async def broken(**_: Any) -> OperationSearchResult:
        raise RuntimeError("database gone")

    with pytest.raises(RuntimeError, match="database gone"):
        await run_search_only(_tasks(("a", ["op.a"])), tenant_id=_TENANT, search_fn=broken)


def test_main_writes_the_json_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(f"version: 1\ntasks:\n{_ONE_TASK}", encoding="utf-8")
    seen: list[tuple[TypedFirstTasks, uuid.UUID]] = []

    async def fake_run(tasks: TypedFirstTasks, tenant_id: uuid.UUID) -> SearchOnlyReport:
        seen.append((tasks, tenant_id))

        async def found(**_: Any) -> OperationSearchResult:
            return OperationSearchResult(op_ids=["k8s.pod.list"])

        return await run_search_only(tasks, tenant_id=tenant_id, search_fn=found)

    monkeypatch.setattr("scripts.retrieval_eval.typed_first._run_and_close", fake_run)
    out = tmp_path / "report.json"

    code = main(["--tenant-id", str(_TENANT), "--tasks", str(tasks_path), "--out", str(out)])

    assert code == 0
    assert [(t.tasks[0].id, tenant) for t, tenant in seen] == [("a", _TENANT)]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["raw_hidden"]["top_1_rate"] == 1.0
    assert report["tasks"][0]["raw_visible"]["rank"] == 1


def test_stdout_holds_only_the_json_report(tmp_path: Path) -> None:
    """Start the real entry module; the startup logs must not reach stdout."""
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        "version: 1\ntasks:\n"
        "  - id: a\n"
        "    connector_id: nothere-api-1.0\n"
        "    request: list the pods\n"
        "    expected_op_ids: [nothere.pod.list]\n",
        encoding="utf-8",
    )

    done = subprocess.run(
        [
            sys.executable,
            *("-m", "scripts.typed_first_eval"),
            *("--tenant-id", str(_TENANT)),
            *("--tasks", str(tasks_path)),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "connector_registered" in done.stderr
    report = json.loads(done.stdout)
    assert report["summary"]["raw_visible"]["not_run_count"] == 1


# ---------------------------------------------------------------------------
# The real search (SQLite path): hiding raw operations leaves them out
# ---------------------------------------------------------------------------

_PRODUCT, _VERSION, _IMPL = "vault", "1.x", "vault"
_TYPED_HANDLER = "meho_backplane.connectors.vault.ops.vault_kv_read"


def _row(*, op_id: str, kind: str, summary: str) -> EndpointDescriptor:
    now = datetime.now(UTC)
    ingested = kind == "ingested"
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        owner_package=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        op_id=op_id,
        source_kind=kind,
        method="GET" if ingested else None,
        path=f"/{op_id}" if ingested else None,
        handler_ref=None if ingested else _TYPED_HANDLER,
        summary=summary,
        description=summary,
        group_id=None,
        tags=[],
        parameter_schema={"type": "object"},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=None,
        custom_description=None,
        custom_notes=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_real_search_hides_raw_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    query = "read the secret"
    async with get_sessionmaker()() as session, session.begin():
        session.add(_row(op_id="vault.kv.read", kind="typed", summary="Read a secret"))
        for n in range(3):
            session.add(
                _row(op_id=f"GET:/secret/data/{n}", kind="ingested", summary=f"{query} {n}")
            )
    tasks = TypedFirstTasks(
        version=1,
        tasks=[
            TypedFirstTask(
                id="t",
                connector_id=f"{_IMPL}-{_VERSION}",
                request=query,
                expected_op_ids=["vault.kv.read"],
            )
        ],
    )

    report = await run_search_only(tasks, tenant_id=_TENANT)

    result = report.tasks[0]
    assert result.raw_visible.not_run_reason is None
    assert result.raw_visible.top_op_ids[0].startswith("GET:")
    assert result.raw_visible.rank == 4
    assert result.raw_hidden.top_op_ids == ["vault.kv.read"]
    assert result.raw_hidden.rank == 1
