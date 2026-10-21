# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vCenter version check of typed operations (#4316)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from packaging.version import InvalidVersion
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest._version_gate import (
    add_version_problem,
    require_vcenter_version,
    version_gated_preview,
    version_problem,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    register_preview_builder,
)
from meho_backplane.settings import get_settings

_OP_ID = "vmware.test.version_gated.read"
_FEATURE = "Test jobs"
_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000c4d6")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` needs for the dispatcher test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


class _Target:
    """A target with a probed version (a JSON mapping, like the ORM row) or none."""

    def __init__(self, version: str | None, *, asserted: str | None = None) -> None:
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = "vc-test"
        self.product = "vmware"
        self.fingerprint = {"version": version} if version is not None else None
        self.version = asserted
        self.preferred_impl_id: str | None = None
        self.host = "vc.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.secret_ref: str | None = None


def _check(target: Any, minimum: str = "9.0") -> str | None:
    return version_problem(target, op_id=_OP_ID, minimum=minimum, feature=_FEATURE)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_below_minimum_raises_with_op_version_and_minimum() -> None:
    with pytest.raises(NotImplementedError) as exc_info:
        require_vcenter_version(_Target("8.0.3"), op_id=_OP_ID, minimum="9.0", feature=_FEATURE)
    assert str(exc_info.value) == (
        f"{_OP_ID}: {_FEATURE} needs vCenter 9.0 or newer; this target is 8.0.3."
    )


@pytest.mark.parametrize("version", ["9.0", "9.0.0", "9.1.1", "9.0.0.24755230"])
def test_equal_or_above_minimum_goes_on(version: str) -> None:
    require_vcenter_version(_Target(version), op_id=_OP_ID, minimum="9.0", feature=_FEATURE)
    assert _check(_Target(version)) is None


def test_patch_level_counts() -> None:
    assert _check(_Target("9.1.0"), minimum="9.1.1") is not None
    assert _check(_Target("9.1.1"), minimum="9.1.1") is None


@pytest.mark.parametrize("version", [None, "", "not-a-version"])
def test_unknown_version_goes_on(version: str | None) -> None:
    require_vcenter_version(_Target(version), op_id=_OP_ID, minimum="9.0", feature=_FEATURE)
    assert _check(_Target(version)) is None


def test_operator_set_version_is_used_without_a_probe() -> None:
    assert _check(_Target(None, asserted="8.0.2")) is not None


def test_probed_version_wins_over_operator_set_version() -> None:
    assert _check(_Target("9.0.0", asserted="8.0.2")) is None


def test_bad_minimum_is_a_bug() -> None:
    with pytest.raises(InvalidVersion):
        _check(_Target("8.0.3"), minimum="nine")
    with pytest.raises(InvalidVersion):
        version_gated_preview(minimum="nine", feature=_FEATURE)


# ---------------------------------------------------------------------------
# The approval request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Descriptor:
    op_id: str


_OPERATOR = Operator(
    sub="op-version-gate",
    name="Version Gate Test Operator",
    email=None,
    raw_jwt="<test-raw-jwt>",
    tenant_id=_TENANT,
    tenant_role=TenantRole.OPERATOR,
)


def _ctx(op_id: str, target: Any, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=_Descriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_OPERATOR,
        target=target,
        params=params,
        connector_id="vmware-rest-8.0",
    )


def test_add_version_problem_keeps_preview_when_version_is_fine() -> None:
    preview = {"vm": "vm-1"}
    out = add_version_problem(
        preview, _Target("9.0"), op_id=_OP_ID, minimum="9.0", feature=_FEATURE
    )
    assert out is preview
    assert (
        add_version_problem(None, _Target(None), op_id=_OP_ID, minimum="9.0", feature=_FEATURE)
        is None
    )


@pytest.mark.asyncio
async def test_registered_version_gated_preview_shows_problem() -> None:
    op_id = "vmware.test.version_gated.create"
    register_preview_builder(
        op_id, version_gated_preview(minimum="9.0", feature=_FEATURE, echo=("name",))
    )

    effect = await build_proposed_effect(
        _ctx(op_id, _Target("8.0.3"), {"name": "job-1", "other": "x"})
    )
    assert effect is not None
    assert effect["preview"] == {
        "name": "job-1",
        "version_problem": (
            f"{op_id}: {_FEATURE} needs vCenter 9.0 or newer; this target is 8.0.3."
        ),
    }

    effect = await build_proposed_effect(_ctx(op_id, _Target("9.0"), {"name": "job-1"}))
    assert effect is not None
    assert effect["preview"] == {"name": "job-1"}


@pytest.mark.asyncio
async def test_own_preview_builder_adds_version_problem() -> None:
    op_id = "vmware.test.version_gated.delete"

    async def _own_builder(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = {"blast_radius": {"object": ctx.params["id"]}}
        return add_version_problem(
            preview, ctx.target, op_id=ctx.descriptor.op_id, minimum="9.1.1", feature=_FEATURE
        )

    register_preview_builder(op_id, _own_builder)

    effect = await build_proposed_effect(_ctx(op_id, _Target("9.0.0"), {"id": "job-1"}))
    assert effect is not None
    assert effect["preview"]["blast_radius"] == {"object": "job-1"}
    assert effect["preview"]["version_problem"] == (
        f"{op_id}: {_FEATURE} needs vCenter 9.1.1 or newer; this target is 9.0.0."
    )

    effect = await build_proposed_effect(_ctx(op_id, _Target("9.1.1"), {"id": "job-1"}))
    assert effect is not None
    assert "version_problem" not in effect["preview"]


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


async def _gated_handler(operator: Any, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A fake typed operation that needs vCenter 9.0."""
    require_vcenter_version(target, op_id=_OP_ID, minimum="9.0", feature=_FEATURE)
    return {"vendor_called": True}


async def _seed_typed_descriptor() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vmware",
                version="8.0",
                impl_id="vmware-rest",
                op_id=_OP_ID,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref=f"{__name__}._gated_handler",
                summary="Read test jobs.",
                description="Fake typed op for the version check.",
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
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )


@pytest.mark.asyncio
async def test_dispatch_on_older_target_returns_connector_unsupported_and_audits() -> None:
    await _seed_typed_descriptor()

    result = await dispatch(
        operator=_OPERATOR,
        connector_id="vmware-rest-8.0",
        op_id=_OP_ID,
        target=_Target("8.0.3"),
        params={},
    )

    expected = f"{_OP_ID}: {_FEATURE} needs vCenter 9.0 or newer; this target is 8.0.3."
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_unsupported:")
    assert result.extras["error_code"] == "connector_unsupported"
    assert result.extras["cause"] == "unsupported_feature"
    assert result.extras["detail"] == expected

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
