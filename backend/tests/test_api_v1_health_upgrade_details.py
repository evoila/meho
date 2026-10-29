# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/health`` shows what an operator needs to confirm an upgrade (#4830).

Three things, all read from ``build_health_response`` so the CLI and the
MCP ``meho_status`` tool share them:

* ``db`` carries the database revision and the revision this build
  expects, for a database at head, behind it and ahead of it;
* ``readiness`` carries the same checks ``/ready`` returns;
* ``approvals`` and ``features`` carry the approval settings in force
  and the gated features — state words and setting names only. A test
  pins that the approval key value never appears in the response.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from meho_backplane import connector_packages
from meho_backplane.api.v1.health import (
    HealthResponse,
    VaultStatus,
    build_health_response,
    liveness,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connector_packages import ConnectorPackage
from meho_backplane.db.migrations import DbMigrationStatus
from meho_backplane.health import ProbeResult, register_probe
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004830")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("APPROVAL_ALLOW_SELF_APPROVAL", raising=False)
    monkeypatch.delenv("APPROVAL_ALLOW_DELEGATED_DECISION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _vault_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", _vault)


def _operator() -> Operator:
    return Operator(
        sub="op-upgrade",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _stub_db(monkeypatch: pytest.MonkeyPatch, status: DbMigrationStatus) -> None:
    async def _status() -> DbMigrationStatus:
        return status

    monkeypatch.setattr("meho_backplane.api.v1.health.db_migration_status", _status)


# ---------------------------------------------------------------------------
# db: revision + head as values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_at_head_reports_matching_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_db(
        monkeypatch,
        DbMigrationStatus(
            probe=ProbeResult(name="db", ok=True, detail="revision=0115"),
            revision="0115",
            head="0115",
        ),
    )
    response = await build_health_response(_operator())
    assert response.db.model_dump() == {
        "migrated": True,
        "revision": "0115",
        "head": "0115",
        "detail": "revision=0115",
    }


@pytest.mark.asyncio
async def test_db_behind_reports_both_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_db(
        monkeypatch,
        DbMigrationStatus(
            probe=ProbeResult(name="db", ok=False, detail="current=0114 head=0115"),
            revision="0114",
            head="0115",
        ),
    )
    response = await build_health_response(_operator())
    assert response.db.migrated is False
    assert (response.db.revision, response.db.head) == ("0114", "0115")
    assert response.db.detail == "current=0114 head=0115"


@pytest.mark.asyncio
async def test_db_ahead_after_rollback_stays_migrated(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_db(
        monkeypatch,
        DbMigrationStatus(
            probe=ProbeResult(name="db", ok=True, detail="current=0116 head=0115 db_ahead=true"),
            revision="0116",
            head="0115",
        ),
    )
    response = await build_health_response(_operator())
    assert response.db.migrated is True
    assert (response.db.revision, response.db.head) == ("0116", "0115")


@pytest.mark.asyncio
async def test_liveness_shares_the_revision_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/api/v1/health/live`` uses the same DbStatus, so it shows the revision too."""
    _stub_db(
        monkeypatch,
        DbMigrationStatus(
            probe=ProbeResult(name="db", ok=True, detail="revision=0115"),
            revision="0115",
            head="0115",
        ),
    )
    response = await liveness(_operator())
    assert (response.db.revision, response.db.head) == ("0115", "0115")
    assert not hasattr(response, "vault")
    assert not hasattr(response, "readiness")


# ---------------------------------------------------------------------------
# readiness: the /ready checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_mirrors_the_registered_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    register_probe("keycloak", lambda: ProbeResult(name="keycloak", ok=True, detail=None))
    register_probe(
        "vault",
        lambda: ProbeResult(name="vault", ok=False, detail="unreachable: ConnectError"),
    )
    response = await build_health_response(_operator())
    assert response.readiness is not None
    assert response.readiness.ready is False
    assert [check.model_dump() for check in response.readiness.checks] == [
        {"name": "keycloak", "ok": True, "detail": ""},
        {"name": "vault", "ok": False, "detail": "unreachable: ConnectError"},
    ]


@pytest.mark.asyncio
async def test_readiness_is_ready_when_every_check_passes() -> None:
    register_probe("keycloak", lambda: ProbeResult(name="keycloak", ok=True))
    register_probe("db", lambda: ProbeResult(name="db", ok=True, detail="revision=0115"))
    response = await build_health_response(_operator())
    assert response.readiness is not None
    assert response.readiness.ready is True
    assert [check.name for check in response.readiness.checks] == ["keycloak", "db"]


# ---------------------------------------------------------------------------
# approvals + features: state words and setting names, never a key value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approvals_default_posture_and_missing_key() -> None:
    response = await build_health_response(_operator())
    assert response.approvals is not None
    assert response.approvals.model_dump() == {
        "custody": "missing",
        "effective_posture": "four_eyes_enforced",
        "delegated_decision_posture": "production_own_hand_only",
    }
    by_name = {entry.name: entry for entry in response.features}
    assert set(by_name) == {
        "agent_runtime",
        "ui_surface",
        "audit_replay",
        "approval_queue",
        "approval_custody",
        "mcp",
    }
    assert by_name["approval_custody"].configured is False
    assert by_name["approval_custody"].missing_env == ["APPROVAL_HANDOFF_ENCRYPTION_KEY"]


@pytest.mark.asyncio
async def test_approvals_follow_the_settings_and_never_carry_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", key)
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    monkeypatch.setenv("APPROVAL_ALLOW_DELEGATED_DECISION", "true")
    get_settings.cache_clear()

    response = await build_health_response(_operator())
    assert response.approvals is not None
    assert response.approvals.model_dump() == {
        "custody": "configured",
        "effective_posture": "single_operator_break_glass",
        "delegated_decision_posture": "lab_class_delegation_allowed",
    }
    by_name = {entry.name: entry for entry in response.features}
    assert by_name["approval_custody"].configured is True
    assert by_name["approval_custody"].missing_env == []
    assert key not in response.model_dump_json()
    # The first 8 characters are enough to spot a partial echo.
    assert key[:8] not in response.model_dump_json()


@pytest.mark.asyncio
async def test_malformed_key_reports_malformed_without_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", "not-a-fernet-key-4830")
    get_settings.cache_clear()

    response = await build_health_response(_operator())
    assert response.approvals is not None
    assert response.approvals.custody == "malformed"
    assert "not-a-fernet-key-4830" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_connector_packages_match_the_version_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The health output lists the same connector packages as ``GET /version`` (#5036)."""
    package = ConnectorPackage(
        name="example-connectors",
        version="1.2.0",
        wheel_digest="sha256:" + "b" * 64,
        folders=["example_widget"],
    )
    monkeypatch.setattr(connector_packages, "_installed", (package,))

    response = await build_health_response(_operator())

    assert response.connector_packages == [package]


@pytest.mark.asyncio
async def test_connector_packages_empty_for_the_plain_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connector_packages, "_installed", ())

    response = await build_health_response(_operator())

    assert response.connector_packages == []


def test_older_payload_without_the_new_fields_still_validates() -> None:
    response = HealthResponse.model_validate(
        {
            "operator": {
                "sub": "s",
                "name": None,
                "email": None,
                "tenant_id": str(_TENANT),
                "tenant_role": "operator",
            },
            "vault": {"reachable": True, "read_ok": True},
            "db": {"migrated": True},
            "mcp_session_id_capture": "when_negotiated",
        }
    )
    assert response.db.revision is None
    assert response.readiness is None
    assert response.approvals is None
    assert response.features == []
