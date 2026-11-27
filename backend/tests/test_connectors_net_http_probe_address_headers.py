# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``net.http_probe``: an address in the answer's ``headers`` shows only its host name.

The probe runs through the real dispatcher, with the tenant's trace on,
against a local HTTP server. The last answer carries a ``Location`` or a
``Content-Location`` header whose address holds made-up values in the path,
the query (also a ticket value), the fragment, the user part or the port.
The tests check the answer, every column of the audit row, the stored trace
and its spans, the agent's copy of the trace, and the log output at DEBUG
and above. None of them holds a made-up value. The ``headers`` value is the
host name of the address, and the other headers stay as the vendor sent them.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.log_host import UNPARSEABLE_ADDRESS
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.http_probe import (
    _advance_or_halt,
    register_net_http_probe_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._http_redirect_harness import (  # noqa: F401 -- the autouse fixture applies by import
    _AGENT_STORE,
    _agent_trace_store,
    _assert_absent,
    _made_up,
    _row_text,
    _start_server,
)

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000d303")
_VENDOR_HEADER = "x-vendor-note"
_VENDOR_WORDS = "kept as the vendor sent it"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
async def _registered_probe(_settings_env: None) -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_net_http_probe_operations(embedding_service=service)
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT_ID, slug="probe-demo", name="probe-demo", flight_recorder_enabled=True
            )
        )
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()


def _operator() -> Operator:
    return Operator(
        sub="op-probe-demo",
        name="Probe Demo Operator",
        email=None,
        raw_jwt="op.probe.demo.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        web_session=True,
    )


async def _stored_text(audit_id: UUID) -> tuple[str, str]:
    """All columns of the audit row, and of the stored trace and its spans."""
    async with get_sessionmaker()() as s:
        audit = (await s.execute(select(AuditLog).where(AuditLog.id == audit_id))).scalar_one()
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = (
            (
                await s.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == trace.id)
                )
            )
            .scalars()
            .all()
        )
        return _row_text(audit), _row_text(trace) + "".join(_row_text(span) for span in spans)


async def _probe(
    caplog: pytest.LogCaptureFixture, status: str, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Probe a local server whose ``/thing`` answers *status* with *headers*."""
    server = await _start_server(
        {"/thing": {**headers, _VENDOR_HEADER: _VENDOR_WORDS}}, status=status
    )
    caplog.set_level(logging.DEBUG)
    try:
        with capture_logs() as events:
            result = await dispatch(
                operator=_operator(),
                connector_id="net-probe-1.x",
                op_id="net.http_probe",
                target=None,
                params={"url": f"http://127.0.0.1:{server.port}/thing", "method": "GET"},
            )
    finally:
        server.server.close()
        await server.server.wait_closed()
    assert result.status == "ok", result.error
    assert result.audit_id is not None
    audit_text, trace_text = await _stored_text(result.audit_id)
    places = {
        "answer": result.model_dump_json(),
        "audit row": audit_text,
        "stored trace": trace_text,
        "log events": repr(events),
        "log records": caplog.text,
        "agent copy of the trace": _AGENT_STORE.text(),
    }
    return result.result, places


def _address(authority: str) -> tuple[str, list[str]]:
    """An address on *authority* with made-up values, and those values."""
    user, password, path, fragment, ticket_user, ticket_sig = (_made_up() for _ in range(6))
    hex_part = secrets.token_hex(4).upper()
    ticket = f"PVE:{ticket_user}@pve:{hex_part}::{ticket_sig}"
    address = f"http://{user}:{password}@{authority}/{path}?ticket={ticket}#{fragment}"
    return address, [user, password, path, fragment, ticket_user, hex_part, ticket_sig]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "header"),
    [
        ("300 Multiple Choices", "Location"),
        ("200 OK", "Content-Location"),
        ("201 Created", "Location"),
    ],
)
async def test_address_header_of_the_last_answer_shows_only_the_host_name(
    caplog: pytest.LogCaptureFixture, status: str, header: str
) -> None:
    address, values = _address("appliance.test.invalid:8443")

    result, places = await _probe(caplog, status, {header: address})

    assert result["status"] == int(status.split()[0])
    assert result["headers"][header.lower()] == "appliance.test.invalid"
    assert result["headers"][_VENDOR_HEADER] == _VENDOR_WORDS
    _assert_absent(values, places)


@pytest.mark.asyncio
async def test_both_address_headers_and_a_relative_address_show_the_host_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, query = _made_up(), _made_up()
    address, values = _address("appliance.test.invalid")

    result, places = await _probe(
        caplog, "201 Created", {"Location": f"/{path}?q={query}", "Content-Location": address}
    )

    assert result["headers"]["location"] == "127.0.0.1"
    assert result["headers"]["content-location"] == "appliance.test.invalid"
    _assert_absent([path, query, *values], places)


@pytest.mark.asyncio
async def test_address_with_an_unreadable_port_shows_one_fixed_mark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    port = _made_up()
    address, values = _address(f"appliance.test.invalid:{port}")

    result, places = await _probe(caplog, "200 OK", {"Content-Location": address})

    assert result["headers"]["content-location"] == UNPARSEABLE_ADDRESS
    _assert_absent([port, *values], places)


def test_redirect_without_a_next_request_shows_only_the_host_name() -> None:
    """A 3xx with a ``Location`` but no next request is a last answer: its address is cut."""
    address, values = _address("appliance.test.invalid")
    request = httpx.Request("GET", "http://127.0.0.1:8080/thing")
    response = httpx.Response(302, headers={"Location": address}, request=request)

    outcome = _advance_or_halt(
        response=response,
        request=request,
        redirect_chain=[],
        url="http://127.0.0.1:8080/thing",
        method="GET",
        started=time.perf_counter(),
        tenant_id=_TENANT_ID,
    )

    assert isinstance(outcome, dict)
    assert outcome["headers"]["location"] == "appliance.test.invalid"
    _assert_absent(values, {"answer": json.dumps(outcome)})
