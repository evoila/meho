# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``proxmox.about`` names only host names when the ticket login gets a redirect.

The operation runs through the real dispatcher, with the tenant's trace on,
against a local HTTP server. The server answers the ticket login with a 302
to another host. The ``Location`` address holds made-up values in the user
part, the port, the path, the query and the fragment. The answer, the audit
row and the stored trace hold none of them.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.proxmox import ProxmoxConnector
from meho_backplane.connectors.proxmox.session import ProxmoxCredentials
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000d303")
_TARGET_NAME = "proxmox-redirect-demo"
_OTHER_HOST = "other.test.invalid"
_OPERATOR = Operator(
    sub="proxmox-redirect-demo",
    name="Proxmox Redirect Demo Operator",
    email=None,
    raw_jwt="<proxmox-redirect-demo-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)


def _made_up() -> str:
    """A made-up value no other hide rule knows: "zq" and 28 random hex characters."""
    return "zq" + secrets.token_hex(14)


def _ticket_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> ProxmoxCredentials:
        return ProxmoxCredentials(mode="ticket", username="demo@pve", password="not-a-password")

    return _load()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_flight_recorder_config_cache_for_testing()
    yield
    reset_dispatcher_caches()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


async def _start_server(location: str) -> asyncio.AbstractServer:
    """A local HTTP/1.1 server that answers every request with a 302 to *location*."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            length = 0
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            await reader.readexactly(length)
            writer.write(
                (
                    "HTTP/1.1 302 Found\r\nContent-Length: 0\r\nConnection: close\r\n"
                    f"Location: {location}\r\n\r\n"
                ).encode("latin-1")
            )
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(_handle, "127.0.0.1", 0)


@pytest.fixture
async def connector() -> AsyncIterator[ProxmoxConnector]:
    await ProxmoxConnector.register_operations()
    instance = ProxmoxConnector(credentials_loader=_ticket_loader)
    _CONNECTOR_INSTANCE_CACHE[ProxmoxConnector] = instance
    yield instance
    await instance.aclose()
    _CONNECTOR_INSTANCE_CACHE.pop(ProxmoxConnector, None)


async def _seed(port: int) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT_ID,
                slug="proxmox-redirect-demo",
                name="proxmox-redirect-demo",
                flight_recorder_enabled=True,
            )
        )
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="proxmox",
                host="127.0.0.1",
                port=port,
                fqdn=None,
                secret_ref="site/proxmox/ticket",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"scheme": "http"},
                fingerprint={"version": "8.x"},
                preferred_impl_id="proxmox-api",
            )
        )
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()


def _row_text(row: Any) -> str:
    return json.dumps(
        {column.name: getattr(row, column.key) for column in row.__mapper__.columns},
        default=str,
    )


async def _stored_text(audit_id: uuid.UUID) -> dict[str, str]:
    """The audit row, and the stored trace with its spans, each as text."""
    async with get_sessionmaker()() as s:
        audit = await s.get(AuditLog, audit_id)
        trace = await s.scalar(select(DispatchTrace).filter_by(audit_id=audit_id))
        assert audit is not None
        assert trace is not None
        spans = list(await s.scalars(select(DispatchTraceSpan).filter_by(trace_id=trace.id)))
    assert spans
    trace_text = _row_text(trace) + "".join(map(_row_text, spans))
    return {"audit row": _row_text(audit), "stored trace": trace_text}


@pytest.mark.asyncio
async def test_about_with_a_redirected_ticket_login_names_only_host_names(
    connector: ProxmoxConnector,
) -> None:
    user, password, path, query, fragment = (_made_up() for _ in range(5))
    port = str(10000 + secrets.randbelow(55000))
    location = f"http://{user}:{password}@{_OTHER_HOST}:{port}/{path}?q={query}#{fragment}"
    server = await _start_server(location)
    try:
        await _seed(server.sockets[0].getsockname()[1])
        async with get_sessionmaker()() as session:
            target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
        result = await dispatch(
            operator=_OPERATOR,
            connector_id="proxmox-api-8.x",
            op_id="proxmox.about",
            target=target,
            params={},
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "error"
    assert result.extras["exception_message"] == (
        f"HTTPStatusError: Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert result.audit_id is not None
    places = {"answer": result.model_dump_json(), **(await _stored_text(result.audit_id))}
    for place, text in places.items():
        for value in (user, password, port, path, query, fragment):
            assert value not in text, f"a made-up value is in the {place}"
