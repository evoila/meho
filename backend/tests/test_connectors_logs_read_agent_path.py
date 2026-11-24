# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared log read on the path an agent takes.

For each connector: ``search_operations`` finds the read with the query
``logs``, ``call_operation`` runs it and writes one audit row, and
``result_query`` reads the rows behind the result handle. A private key
that spans log lines is hidden in the inline answer, in the stored rows
and in the audit row, also when the read holds only the middle of it.
"""

from __future__ import annotations

import json
import re
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.tools.result_query import _result_query_handler
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation, search_operations
from meho_backplane.settings import get_settings
from tests._log_read_helpers import FakeJournal, FakePodLog, cursor, journal_time
from tests._result_store_helpers import MemoryResultStore

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000057a1")
_OPERATOR = Operator(
    sub="logs-read-agent",
    name="Logs Read Agent",
    email=None,
    raw_jwt="<logs-read-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)
_KEY_BODY = [f"madeUpKeyBody{i:02d}" for i in range(20)]
_KEY = [
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    *_KEY_BODY,
    "-----END RSA " + "PRIVATE KEY-----",
]
_LONG_KEY = [_KEY[0], *[f"madeUpKeyBody{i:03d}" for i in range(180)], _KEY[-1]]
#: The key is in the inline sample (the first rows) and behind it.
_LINES = ["first", *_KEY, *[f"line {i:03d}" for i in range(100)], *_KEY, "last"]
_DIMENSION = 384


async def _word_vector(text: str, **_: Any) -> list[float]:
    """A small stand-in for the embedding model: how often each word occurs."""
    vector = [0.0] * _DIMENSION
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(word.rstrip("s").encode()) % _DIMENSION] += 1.0
    return vector


def _stamp(number: int) -> str:
    return f"2026-09-21T14:13:20.{number:09d}Z"


@dataclass(frozen=True)
class _Case:
    connector: type[LinuxSshConnector] | type[KubernetesConnector]
    product: str
    impl_id: str
    op_id: str
    source: str
    marker: str | None

    @property
    def connector_id(self) -> str:
        return f"{self.impl_id}-1.x"


_CASES = [
    _Case(
        LinuxSshConnector,
        "linux",
        "linux-ssh",
        "linux.logs.read",
        "app.service",
        cursor(len(_LINES) - 1),
    ),
    _Case(KubernetesConnector, "k8s", "k8s", "k8s.logs.read", "prod/web-1/app", None),
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """In-memory result store, word-count embeddings and log sources that need no host."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: memory)
    service = AsyncMock()
    service.encode_one.side_effect = _word_vector
    service.dimension = _DIMENSION
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text", _word_vector
    )
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return memory


@contextmanager
def _source(lines: list[str]) -> Iterator[None]:
    """A journal and a container log that hold *lines*."""
    journal = FakeJournal(list(lines))
    pod = FakePodLog([f"{_stamp(number)} {line}" for number, line in enumerate(lines)])

    async def run(_self: Any, target: Any, cmd: str, **kwargs: Any) -> Any:
        return await journal.run(target, cmd, **kwargs)

    with patch.object(LinuxSshConnector, "_run_command", run), pod.patched():
        yield


async def _seed_target(case: _Case) -> str:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=f"logs-{case.product}",
                aliases=[],
                product=case.product,
                host="logs.test.invalid",
                port=443,
                secret_ref="made-up/ref",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "1.x"},
                preferred_impl_id=case.impl_id,
            )
        )
        await session.commit()
    return f"logs-{case.product}"


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.scalars().all())


async def _call(case: _Case, target: str, **params: Any) -> dict[str, Any]:
    return await call_operation(
        _OPERATOR,
        {
            "connector_id": case.connector_id,
            "op_id": case.op_id,
            "target": target,
            "params": {"source": case.source, **params},
        },
    )


async def _stored_rows(handle: dict[str, Any], total: int) -> list[dict[str, Any]]:
    """Read all rows behind the handle with ``result_query``."""
    rows: list[dict[str, Any]] = []
    while len(rows) < total:
        page = await _result_query_handler(
            _OPERATOR, {"handle_id": handle["handle_id"], "offset": len(rows), "limit": 50}
        )
        assert page["rows"]
        rows += page["rows"]
    return rows


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.product)
async def test_agent_finds_runs_and_pages_the_log_read(
    case: _Case, store: MemoryResultStore
) -> None:
    await case.connector.register_operations()
    target = await _seed_target(case)

    found = await search_operations(
        _OPERATOR, {"connector_id": case.connector_id, "query": "logs", "limit": 10}
    )
    hit = next(hit for hit in found["hits"] if hit["op_id"] == case.op_id)
    assert hit["group_key"] == "logs" and hit["safety_level"] == "safe"
    assert hit["requires_approval"] is False

    with _source(_LINES):
        answer = await _call(case, target, since="2026-01-01T00:00:00Z")
    assert answer["status"] == "ok", answer
    assert answer["result"]["truncated"] is False
    assert answer["result"]["covered"]["from"] < answer["result"]["covered"]["to"]
    assert answer["result"]["continuation"] == case.marker

    audit = await _audit_rows(case.op_id)
    assert len(audit) == 1 and audit[0].operator_sub == _OPERATOR.sub

    handle = answer["handle"]
    assert handle["total_rows"] == len(_LINES)
    sample = json.dumps(answer["result"]) + json.dumps(handle)
    rows = await _stored_rows(handle, len(_LINES))
    assert rows[0]["line"] == "first" and rows[-1]["line"] == "last"
    assert [row["line"] for row in rows if row["line"].startswith("line ")] == [
        line for line in _LINES if line.startswith("line ")
    ]
    assert all(row["time"] for row in rows)

    stored = b"".join(store.client.values.values()).decode()
    for text in (sample, json.dumps(rows), stored, json.dumps(audit[0].payload, default=str)):
        assert not any(part in text for part in _KEY_BODY)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.product)
async def test_a_window_inside_a_key_shows_no_part_of_it(
    case: _Case, store: MemoryResultStore
) -> None:
    """The window starts and ends between the first and the last line of the key."""
    await case.connector.register_operations()
    target = await _seed_target(case)
    lines = ["first", *_LONG_KEY, "last"]
    # One line apart: a microsecond in the journal, a nanosecond in the container log.
    time = journal_time if case.connector is LinuxSshConnector else _stamp
    window = {"since": time(30), "until": time(149)}

    with _source(lines):
        answer = await _call(case, target, **window)
        short = await _call(case, target, **window, lines=3)
        matched = await _call(case, target, **window, filter="madeUpKeyBody")
        behind = await _call(case, target, since=time(30))
    assert answer["status"] == "ok", answer
    handle = answer["handle"]
    assert handle["total_rows"] == 120
    rows = await _stored_rows(handle, 120)
    assert all(row["time"] and row["line"] for row in rows)
    assert short["result"]["total"] == 3 and short["result"]["truncated"] is True
    assert matched["result"]["rows"] == [] and matched["result"]["truncated"] is False
    assert behind["status"] == "ok"
    assert (await _stored_rows(behind["handle"], len(lines) - 30))[-1]["line"] == "last"

    stored = b"".join(store.client.values.values()).decode()
    audit = [row.payload for row in await _audit_rows(case.op_id)]
    for text in (json.dumps([answer, short, behind]), json.dumps(rows), stored):
        assert "madeUpKeyBody" not in text
    # Only the filter that the caller gave is in an audit row.
    assert json.dumps(audit, default=str).count("madeUpKeyBody") <= 1


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.product)
async def test_a_key_that_starts_far_before_the_window_shows_no_part_of_it(
    case: _Case, store: MemoryResultStore
) -> None:
    """Other messages stand between the lines of the key, and its end is not read."""
    await case.connector.register_operations()
    target = await _seed_target(case)
    body = _LONG_KEY[1:-1]
    other = [f"svc: request {i} ok" for i in range(300)]
    lines = [_KEY[0], *other, *body[:120], "svc: done", _KEY[-1]]
    time = journal_time if case.connector is LinuxSshConnector else _stamp
    window = {"since": time(301), "until": time(421)}

    with _source(lines):
        answer = await _call(case, target, **window)
        short = await _call(case, target, **window, lines=3)
        matched = await _call(case, target, **window, filter="madeUpKeyBody")
    assert answer["status"] == "ok", answer
    assert "120 of the lines" in answer["result"]["warning"]
    rows = await _stored_rows(answer["handle"], 121)
    assert rows[-1]["line"] == "svc: done"
    assert short["result"]["total"] == 3
    assert matched["result"]["rows"] == [] and "warning" in matched["result"]

    stored = b"".join(store.client.values.values()).decode()
    for text in (json.dumps([answer, short, matched]), json.dumps(rows), stored):
        assert "madeUpKeyBody" not in text
