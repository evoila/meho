# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared log read on the path an agent takes.

For each connector: ``search_operations`` finds the read with the query
``logs``, ``call_operation`` runs it and writes one audit row, and
``result_query`` reads the rows behind the result handle. A private key
that spans log lines is hidden in the inline answer, in the stored rows
and in the audit row.
"""

from __future__ import annotations

import json
import re
import uuid
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
#: The key is in the inline sample (the first rows) and behind it.
_LINES = ["first", *_KEY, *[f"line {i:03d}" for i in range(100)], *_KEY, "last"]
_MARKER = "s=" + "a" * 32 + ";i={};b=" + "b" * 32 + ";m=1;t=2;x=3"


_DIMENSION = 384


async def _word_vector(text: str, **_: Any) -> list[float]:
    """A small stand-in for the embedding model: how often each word occurs."""
    vector = [0.0] * _DIMENSION
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(word.rstrip("s").encode()) % _DIMENSION] += 1.0
    return vector


async def _journal(_self: Any, _target: Any, _cmd: str, **_kwargs: Any) -> Any:
    entries = [
        json.dumps(
            {
                "__CURSOR": _MARKER.format(number),
                "__REALTIME_TIMESTAMP": str(1_790_000_000_000_000 + number),
                "MESSAGE": line,
            }
        )
        for number, line in enumerate(_LINES)
    ]
    proc = MagicMock()
    proc.stdout = "\n".join(entries) + "\nMEHO_JOURNAL_RC=0\n"
    proc.stderr = ""
    proc.exit_status = 0
    return proc


async def _pod_log(*_args: Any) -> dict[str, Any]:
    lines = [f"2026-11-10T08:00:00.{i:09d}Z {line}" for i, line in enumerate(_LINES)]
    return {"lines": lines, "truncated": False}


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
        _MARKER.format(len(_LINES) - 1),
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
    monkeypatch.setattr(LinuxSshConnector, "_run_command", _journal)
    monkeypatch.setattr("meho_backplane.connectors.kubernetes.ops_logs_read.k8s_logs", _pod_log)
    return memory


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

    answer = await call_operation(
        _OPERATOR,
        {
            "connector_id": case.connector_id,
            "op_id": hit["op_id"],
            "target": target,
            "params": {"source": case.source, "since": "2026-01-01T00:00:00Z"},
        },
    )
    assert answer["status"] == "ok", answer
    assert answer["result"]["truncated"] is False
    assert answer["result"]["covered"]["from"] < answer["result"]["covered"]["to"]
    assert answer["result"]["continuation"] == case.marker

    audit = await _audit_rows(case.op_id)
    assert len(audit) == 1 and audit[0].operator_sub == _OPERATOR.sub

    handle = answer["handle"]
    assert handle["total_rows"] == len(_LINES)
    sample = json.dumps(answer["result"]) + json.dumps(handle)
    rows: list[dict[str, Any]] = []
    while len(rows) < len(_LINES):
        page = await _result_query_handler(
            _OPERATOR, {"handle_id": handle["handle_id"], "offset": len(rows), "limit": 50}
        )
        assert page["rows"]
        rows += page["rows"]
    assert rows[0]["line"] == "first" and rows[-1]["line"] == "last"
    assert [row["line"] for row in rows if row["line"].startswith("line ")] == [
        line for line in _LINES if line.startswith("line ")
    ]
    assert all(row["time"] for row in rows)

    stored = b"".join(store.client.values.values()).decode()
    for text in (sample, json.dumps(rows), stored, json.dumps(audit[0].payload, default=str)):
        assert not any(part in text for part in _KEY_BODY)
