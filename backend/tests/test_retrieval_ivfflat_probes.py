# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``RETRIEVAL_IVFFLAT_PROBES`` (#6785): the setting and what the vector query sends."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.retrieval.retriever import _COSINE_CANDIDATES_SQL, _cosine_candidates
from meho_backplane.settings import Settings, get_settings
from tests.mcp_test_fixtures import required_settings_env  # noqa: F401 -- autouse fixture


class _Result:
    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.statements.append((str(statement), params))
        return _Result()


async def _run(session: _RecordingSession) -> None:
    await _cosine_candidates(session, uuid.uuid4(), "[0.1]", None, None, None, None)  # type: ignore[arg-type]


def test_default_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETRIEVAL_IVFFLAT_PROBES", raising=False)
    get_settings.cache_clear()
    assert get_settings().retrieval_ivfflat_probes == 0


def test_reads_the_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_IVFFLAT_PROBES", "12")
    get_settings.cache_clear()
    assert get_settings().retrieval_ivfflat_probes == 12


@pytest.mark.parametrize("value", ["-1", "101", "many"])
def test_refuses_a_bad_value(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("RETRIEVAL_IVFFLAT_PROBES", value)
    get_settings.cache_clear()
    with pytest.raises((ValidationError, ValueError)):
        get_settings()


def test_field_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**get_settings().model_dump(), "retrieval_ivfflat_probes": -1})


async def test_zero_sends_only_the_vector_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETRIEVAL_IVFFLAT_PROBES", raising=False)
    get_settings.cache_clear()
    session = _RecordingSession()
    await _run(session)
    assert [sql for sql, _ in session.statements] == [str(_COSINE_CANDIDATES_SQL)]


async def test_above_zero_sets_probes_for_the_transaction_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETRIEVAL_IVFFLAT_PROBES", "25")
    get_settings.cache_clear()
    session = _RecordingSession()
    await _run(session)
    first_sql, first_params = session.statements[0]
    assert "set_config('ivfflat.probes', :probes, true)" in first_sql
    assert first_params == {"probes": "25"}
    assert session.statements[1][0] == str(_COSINE_CANDIDATES_SQL)
