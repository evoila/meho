# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step error envelope is cleaned before the reducer stores it (#5031).

The envelope of a multi-step operation's terminal error goes through the
operator's redaction policy, then the reducer. A shadow-mode policy (#1073)
leaves every secret in place, so the dispatcher must run the full error
cleanup (the secret-field walk, then the pinned Tier-1 default policy) over
the envelope **before** the reduce: the reducer writes the handle's stored
rows and sample rows right then, and the later cleanup of the inline result
cannot reach them. These tests drive the real JSONFlux reducer with an
in-memory store and check the inline result, the handle's sample rows, the
stored rows and the audit row. When the cleanup itself fails, the envelope is
withheld and nothing is stored.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations import dispatcher as dispatcher_module
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction import clear_overrides, parse_policy, register_policy
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryResultStore

_TOKEN = "made-up-token-5031-shadow"
_PASSWORD = "made-up-pass-5031-shadow"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000005032")
_OP_ID = "vault.composite.planted_shadow_error"

#: More issues than the reducer keeps inline, so it mints a result handle.
_ISSUE_COUNT = 80


async def _composite_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Module-level multi-step handler: a terminal-error envelope full of secrets."""
    del operator, target, params
    issues = [
        {
            "category": "deploy",
            "severity": "warning",
            "message": f"disk {n} skipped; session Bearer {_TOKEN}",
            "rejected": {"password": _PASSWORD},
        }
        for n in range(_ISSUE_COUNT)
    ]
    issues[-1] = {
        "category": "deploy",
        "severity": "error",
        "message": f"deploy failed: Bearer {_TOKEN}",
        "rejected": {"password": _PASSWORD},
    }
    return {"status": "deploy_error", "vm_id": None, "issues": issues}


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _Fingerprint:
    version: str | None = None


class _Target:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = _Fingerprint()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "made-up-target"
        self.host = "target.example.invalid"
        self.port = 443
        self.auth_model = "shared_service_account"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    yield
    set_default_reducer(PassThroughReducer())
    clear_overrides()
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def shadow_policy() -> None:
    """Register a shadow-mode policy for every call: detection only, no change."""
    register_policy(
        parse_policy(
            "id: shadow-5031\nversion: 1\nmode: shadow\nrules:\n"
            "  - name: strip-bearer\n    pattern: bearer_token\n    action: redact\n"
            '    reason: "shadow test"\n'
        )
    )


def _operator() -> Operator:
    return Operator(
        sub="op-shadow",
        name="Shadow Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register_composite() -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=_OP_ID,
                source_kind="composite",
                method=None,
                path=None,
                handler_ref=f"{__name__}._composite_handler",
                summary="Multi-step terminal-error probe with planted secrets.",
                description="Returns an error envelope with a token and a password field.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=[0.1] * 384,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _dispatch_with_real_reducer() -> tuple[OperationResult, MemoryResultStore, Operator]:
    await _register_composite()
    operator = _operator()
    store = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=store))
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id=_OP_ID,
        target=_Target(),
        params={},
    )
    return result, store, operator


async def _audit_row() -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == _OP_ID))
        return rows.scalars().one()


def _leaks(text: str) -> list[str]:
    return [value for value in (_TOKEN, _PASSWORD) if value in text]


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow_policy")
async def test_envelope_is_clean_in_result_handle_samples_and_stored_rows() -> None:
    """With a shadow policy, the stored copy behind the handle is still clean."""
    result, store, operator = await _dispatch_with_real_reducer()

    assert result.status == "error"
    assert result.extras["error_code"] == "deploy_error"
    assert result.handle is not None
    assert result.handle.total_rows == _ISSUE_COUNT
    assert (result.error or "").startswith("deploy_error: deploy failed:")

    assert _leaks(result.model_dump_json()) == []
    assert _leaks(json.dumps(result.handle.sample_rows, default=str)) == []

    stored = await store.fetch_rows(
        tenant_id=_TENANT, operator_sub=operator.sub, handle_id=result.handle.handle_id
    )
    assert stored is not None
    assert len(stored.rows) == _ISSUE_COUNT
    assert _leaks(json.dumps(stored.rows, default=str)) == []
    assert stored.rows[-1]["message"].startswith("deploy failed:")
    assert stored.rows[-1]["rejected"] == {"password": "[REDACTED:param_name]"}

    row = await _audit_row()
    assert row.payload["result_status"] == "error"
    assert _leaks(json.dumps(row.payload, default=str)) == []
    # The raw payload keeps the handler's answer for replay, as for a normal
    # row (``docs/codebase/redaction.md``); the shadow policy left it as is.
    assert _leaks(json.dumps(row.raw_payload, default=str)) == [_TOKEN, _PASSWORD]


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow_policy")
async def test_failed_cleanup_withholds_the_envelope_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cleanup fails, no envelope is returned and no handle is stored."""

    def _boom(node: Any, policy: Any = None) -> Any:
        raise RuntimeError("redaction unavailable")

    monkeypatch.setattr(dispatcher_module, "redact_error_payload", _boom)
    result, store, _ = await _dispatch_with_real_reducer()

    assert result.status == "error"
    assert result.extras["error_code"] == "deploy_error"
    assert result.error == "deploy_error"
    assert result.result is None
    assert result.handle is None
    assert result.delivery == "unavailable"
    assert _leaks(result.model_dump_json()) == []
    assert store.client.values == {}

    row = await _audit_row()
    assert row.payload["result_status"] == "error"
    assert _leaks(json.dumps(row.payload, default=str)) == []
