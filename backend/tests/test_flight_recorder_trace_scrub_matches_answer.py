# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A flight-recorder trace hides at least what the normal answer hides.

The trace scrub (``redaction/flight_recorder/_content.py``) uses the same
patterns, in the same order, as the default policy of the normal answer
(``redaction/policies/default.yaml``), and it hides a number under a secret
name as the normal answer does. The secret test values of the default policy
go through the real vendor-call seam (``record_vendor_call``) here, and the
stored trace must not hold any of them.

All values are made up and built from pieces, so secret scanners do not flag
this file.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.redaction.flight_recorder._content import _CONTENT_POLICY, scrub_content
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER, scrub_secret_fields
from meho_backplane.settings import get_settings

_BASE_URL = "https://vendor.example.com"

_DASHES = "-" * 5
_PEM_BODY = "MHcCAQEEmadeupPemBody"
_PEM_PRIVATE_KEY = (
    _DASHES
    + "BEGIN "
    + "EC PRIVATE KEY"
    + _DASHES
    + "\n"
    + _PEM_BODY
    + "\n"
    + _DASHES
    + "END "
    + "EC PRIVATE KEY"
    + _DASHES
)
_PASSWORD_HASH = "$" + "6$" + "saltsalt$" + "abcdefghijklmnopqrstuvwxyz0123456789./ABCD"
_CEPH_KEY = "AQ" + "AAAAAAAAAAAAAAAAAAAA" + "bbbbbbbbbbbbbbbb" + "=="
_BIND_SECRET = "exampleTsig" + "Value=="
_NUMERIC_PASSWORD = 73519046
_NUMERIC_PAIR_VALUE = 60418273


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


def _vendor_answer() -> dict[str, Any]:
    """A vendor answer with each secret kind of the default policy under a plain key."""
    return {
        "name": "node-1",
        "port": 8443,
        "enabled": True,
        "tls_key": _PEM_PRIVATE_KEY,
        "shadow_line": "root:" + _PASSWORD_HASH + ":19000:0:99999:7:::",
        "keyring": "[client.admin]\n\tkey = " + _CEPH_KEY + "\n",
        "named_conf": 'key "rndc-key" { algorithm hmac-sha256; secret "' + _BIND_SECRET + '"; };',
        "password": _NUMERIC_PASSWORD,
        "settings": [{"name": "PASSWORD", "value": _NUMERIC_PAIR_VALUE}],
        "secret_enabled": False,
    }


_SECRET_NEEDLES = (
    _PEM_BODY,
    _PASSWORD_HASH,
    _CEPH_KEY,
    _BIND_SECRET,
    str(_NUMERIC_PASSWORD),
    str(_NUMERIC_PAIR_VALUE),
)


def test_trace_policy_has_the_patterns_of_the_default_policy_in_order() -> None:
    trace_patterns = [rule.pattern for rule in _CONTENT_POLICY.rules]
    default_patterns = [rule.pattern for rule in get_default_policy().rules]
    assert trace_patterns == default_patterns
    assert all(rule.action == "redact" for rule in _CONTENT_POLICY.rules)


def test_scrub_content_hides_every_default_policy_secret() -> None:
    scrubbed, fired = scrub_content(_vendor_answer())
    assert fired is True
    blob = repr(scrubbed)
    for needle in _SECRET_NEEDLES:
        assert needle not in blob
    assert scrubbed["password"] == SECRET_FIELD_MARKER
    assert scrubbed["settings"][0]["value"] == SECRET_FIELD_MARKER
    # Plain values, and a flag under a secret name, stay.
    assert scrubbed["name"] == "node-1"
    assert scrubbed["port"] == 8443
    assert scrubbed["enabled"] is True
    assert scrubbed["secret_enabled"] is False


def test_secret_field_walk_keeps_numbers_without_hide_numbers() -> None:
    """The error path keeps its rule: only the trace scrub hides numbers."""
    node = {"password": 1234, "pin": None, "secret_enabled": True}
    assert scrub_secret_fields(node) == node
    hidden = scrub_secret_fields(node, hide_numbers=True)
    assert hidden == {"password": SECRET_FIELD_MARKER, "pin": None, "secret_enabled": True}


def test_scrub_content_hides_a_float_but_not_a_bool_under_a_secret_name() -> None:
    scrubbed, fired = scrub_content({"password": 12.5, "token": True, "passwords": [1, 2]})
    assert fired is True
    assert scrubbed == {
        "password": SECRET_FIELD_MARKER,
        "token": True,
        "passwords": SECRET_FIELD_MARKER,
    }


# --------------------------------------------------------------------------
# Through the vendor-call seam into the stored trace
# --------------------------------------------------------------------------


class _HttpConnector(HttpConnector):
    product = "test-http"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _seed_tenant() -> UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"fr-scrub-{tenant_id.hex[:8]}",
                name="Trace scrub tenant",
                flight_recorder_enabled=True,
            )
        )
        await session.commit()
    return tenant_id


async def _stored_spans(audit_id: UUID) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        return list(
            (
                await session.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == header.id)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_default_policy_secrets_do_not_reach_the_stored_trace() -> None:
    tenant_id = await _seed_tenant()
    operator = Operator(
        sub="op-scrub",
        name=None,
        email=None,
        raw_jwt="made-up-token",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    target = SimpleNamespace(
        name="test-target",
        host="vendor.example.com",
        port=443,
        id="11111111-1111-1111-1111-111111111111",
        tenant_id=str(tenant_id),
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )
    conn = _HttpConnector()
    audit_id = uuid.uuid4()
    handle = await capture.begin_dispatch_capture(
        audit_id=audit_id,
        operator=operator,
        target=target,
        descriptor=SimpleNamespace(op_id="svc.nodes.get", tags=[], source_kind="ingested"),
        connector_id="test-http-1.x",
    )
    try:
        async with respx.mock(base_url=_BASE_URL) as mock:
            mock.get("/api/nodes/1").respond(200, json=_vendor_answer())
            result = await conn._get_json(target, "/api/nodes/1", operator=operator)
    finally:
        await capture.end_dispatch_capture(handle)
        await conn.aclose()

    # The connector result itself is untouched; only the trace is scrubbed.
    assert result == _vendor_answer()
    spans = await _stored_spans(audit_id)
    assert len(spans) == 1
    stored = repr(spans[0].attributes)
    for needle in _SECRET_NEEDLES:
        assert needle not in stored, f"{needle!r} reached the stored trace"
    body = spans[0].attributes["response_body"]
    assert body["name"] == "node-1"
    assert body["port"] == 8443
    assert body["secret_enabled"] is False
