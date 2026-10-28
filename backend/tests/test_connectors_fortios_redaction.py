# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No FortiOS secret reaches an answer, an audit row, an error text, a trace or saved test data.

The tests plant ``ENC ...`` values and values under known secret field names
in recorded answers and check every place a value can end up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.fortios import (
    FORTIOS_CONNECTOR_ID,
    FortiOSConnector,
    register_fortios_typed_operations,
)
from meho_backplane.connectors.fortios.redaction import REDACTED, is_secret_name, scrub, scrub_text
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from tests import _fortios_fixtures as fx
from tests._fortios_support import (
    FortiTarget,
    make_operator,
    reset_module_state,
    settings_env,
    stub_embedding,
    token_loader,
)

#: Planted secrets. None of them may show up anywhere.
_ENC_VALUE = "ENC c2VjcmV0LXBsYW50ZWQtdmFsdWUtMQ=="
_ENC_ODD_FIELD = "ENC b2RkLWZpZWxkLXBsYW50ZWQtdmFsdWU="
_PLAIN_PSK = "plain-planted-psk-value"
_PLAIN_PASSWORD = "plain-planted-password-value"
_PLANTED = (
    _ENC_VALUE,
    _ENC_ODD_FIELD,
    _PLAIN_PSK,
    _PLAIN_PASSWORD,
    "c2VjcmV0LXBsYW50ZWQtdmFsdWUtMQ",
    fx.TOKEN,
)

_VDOMS_WITH_SECRETS = [
    {
        "name": "root",
        "short-name": "root",
        "vcluster-id": 0,
        "password": _ENC_VALUE,
        "made-up-odd-field": _ENC_ODD_FIELD,
        "nested": [{"psksecret": _PLAIN_PSK, "sso-password": _PLAIN_PASSWORD}],
    }
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from settings_env(monkeypatch)


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    yield from reset_module_state()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    return stub_embedding(monkeypatch)


@pytest.fixture
async def session() -> Any:
    async with get_sessionmaker()() as s:
        yield s


def _assert_clean(text: str) -> None:
    for secret in _PLANTED:
        assert secret not in text


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_scrub_removes_enc_values_and_secret_fields_and_keeps_the_rest() -> None:
    cleaned, count = scrub(_VDOMS_WITH_SECRETS)
    row = cleaned[0]
    assert row["password"] == REDACTED
    assert row["made-up-odd-field"] == REDACTED
    assert row["nested"][0] == {"psksecret": REDACTED, "sso-password": REDACTED}
    assert (row["name"], row["short-name"], row["vcluster-id"]) == ("root", "root", 0)
    assert count == 4


@pytest.mark.parametrize(
    "name", ["password", "psksecret", "psksecret-remote", "ppk-secret", "private-key", "md5-key"]
)
def test_known_secret_names(name: str) -> None:
    assert is_secret_name(name)


@pytest.mark.parametrize(
    "name", ["name", "q_origin_key", "keylife", "keylifeseconds", "comment", "subnet", "mkey"]
)
def test_names_that_are_not_secrets(name: str) -> None:
    assert not is_secret_name(name)


def test_scrub_keeps_an_empty_secret_field_and_removes_a_pem_key() -> None:
    marker = "PRIVATE " + "KEY"
    pem = f"-----BEGIN {marker}-----\nmade-up\n-----END {marker}-----"
    cleaned, _ = scrub({"password": "", "comments": pem})
    assert cleaned == {"password": "", "comments": REDACTED}


def test_scrub_text_removes_enc_values_inside_free_text() -> None:
    text = f"set psksecret {_ENC_VALUE} failed"
    assert scrub_text(text) == f"set psksecret {REDACTED} failed"


def test_scrub_removes_enc_values_inside_a_longer_text_field() -> None:
    """A ``cli_error`` such as ``set psksecret ENC ...`` is cleaned inside the text."""
    cleaned, count = scrub({"cli_error": f"set psksecret {_ENC_VALUE} -- refused", "error": -5})
    assert cleaned == {"cli_error": f"set psksecret {REDACTED} -- refused", "error": -5}
    assert count == 1


def test_saved_fixtures_hold_no_secret() -> None:
    """The recorded answers committed with the tests are clean."""
    _, count = scrub(fx.DEVICE_ANSWERS)
    assert count == 0
    text = Path(fx.__file__).read_text(encoding="utf-8")
    assert "ENC " not in text
    assert "PRIVATE KEY" not in text


def test_the_trace_keeps_only_the_cleaned_answer() -> None:
    connector = FortiOSConnector(token_loader=token_loader)
    response = httpx.Response(200, json=fx.wrap(_VDOMS_WITH_SECRETS, path="system", name="vdom"))
    recorded = connector.trace_response_body(response)
    _assert_clean(json.dumps(recorded))
    assert recorded["results"][0]["name"] == "root"
    text_response = httpx.Response(500, text=f"made-up failure {_ENC_VALUE}")
    _assert_clean(json.dumps(connector.trace_response_body(text_response)))
    error_response = httpx.Response(
        500, json=fx.error_answer(500, error=-5, cli_error=f"set psksecret {_ENC_VALUE} -- no")
    )
    recorded = connector.trace_response_body(error_response)
    _assert_clean(json.dumps(recorded))
    assert recorded["cli_error"] == f"set psksecret {REDACTED} -- no"


# ---------------------------------------------------------------------------
# Through dispatch: answer, audit row, error text, trace
# ---------------------------------------------------------------------------


async def _register() -> None:
    await register_fortios_typed_operations()
    instance = get_or_create_connector_instance(FortiOSConnector)
    assert isinstance(instance, FortiOSConnector)
    instance._token_loader = token_loader


async def _dispatch(op_id: str, answers: dict[str, Any]) -> Any:
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fx.FakeFortiGate(answers))
        return await dispatch(
            operator=make_operator(),
            connector_id=FORTIOS_CONNECTOR_ID,
            op_id=op_id,
            target=FortiTarget(),
            params={},
        )


async def _audit_text(audit_id: UUID) -> str:
    async with get_sessionmaker()() as s:
        row = (await s.execute(select(AuditLog).where(AuditLog.id == audit_id))).scalar_one()
        values = {c.name: getattr(row, c.name) for c in AuditLog.__table__.columns}
    return json.dumps(values, default=str)


async def _trace_text(audit_id: UUID) -> str:
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = (
            await s.execute(select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == trace.id))
        ).scalars()
        return json.dumps([f"{span.name}|{span.attributes}" for span in spans], default=str)


async def _enable_trace_capture(session: AsyncSession) -> None:
    tenant_id = make_operator().tenant_id
    session.add(
        Tenant(
            id=tenant_id, slug="fgt-redaction", name="fgt-redaction", flight_recorder_enabled=True
        )
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


@pytest.mark.asyncio
async def test_planted_secrets_reach_no_answer_audit_row_or_trace(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    await _enable_trace_capture(session)
    answers = {**fx.DEVICE_ANSWERS, "cmdb/system/vdom": _VDOMS_WITH_SECRETS}
    result = await _dispatch("fortios.vdom.list", answers)
    assert result.status == "ok", result.error
    assert result.result["vdoms"][0]["name"] == "root"
    _assert_clean(json.dumps(result.result) + json.dumps(dict(result.extras), default=str))
    assert result.audit_id is not None
    _assert_clean(await _audit_text(result.audit_id))
    trace = await _trace_text(result.audit_id)
    assert f"GET {fx.BASE_URL}/api/v2/cmdb/system/vdom" in trace
    assert REDACTED in trace
    _assert_clean(trace)


@pytest.mark.asyncio
async def test_planted_secrets_in_an_error_answer_reach_no_trace(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    await _enable_trace_capture(session)
    cli_error = f"set psksecret {_ENC_VALUE} -- made-up refusal"
    for answer in (
        httpx.Response(200, json=fx.error_answer(500, error=-5, cli_error=cli_error)),
        httpx.Response(
            424, json={**fx.error_answer(424, cli_error=cli_error), "password": _PLAIN_PASSWORD}
        ),
    ):
        result = await _dispatch("fortios.vdom.list", {"cmdb/system/vdom": answer})
        assert result.status == "error"
        assert result.audit_id is not None
        trace = await _trace_text(result.audit_id)
        assert f"GET {fx.BASE_URL}/api/v2/cmdb/system/vdom" in trace
        assert REDACTED in trace
        _assert_clean(trace)


@pytest.mark.asyncio
async def test_planted_secrets_reach_no_error_text(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    cli_error = f"set psksecret {_ENC_VALUE} -- made-up refusal"
    for answer in (
        httpx.Response(200, json=fx.error_answer(500, error=-5, cli_error=cli_error)),
        httpx.Response(500, json=fx.error_answer(500, error=-5, cli_error=cli_error)),
        httpx.Response(424, json={**fx.error_answer(424), "password": _PLAIN_PASSWORD}),
    ):
        result = await _dispatch("fortios.vdom.list", {"cmdb/system/vdom": answer})
        assert result.status == "error"
        rendered = f"{result.error} {json.dumps(dict(result.extras), default=str)}"
        _assert_clean(rendered)
        assert "cmdb/system/vdom" in rendered
        assert result.audit_id is not None
        _assert_clean(await _audit_text(result.audit_id))
