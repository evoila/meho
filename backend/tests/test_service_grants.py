# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Service-layer tests for the service-principal standing-grant CRUD (#3151).

Exercises :class:`~meho_backplane.operations.service_grants.ServicePrincipalGrantService`
directly (no HTTP): create with the full create-time review (wildcard
refusal, delete-shaped refusal by pattern and by descriptor tag, past /
naive expiry, duplicate-active refusal), soft-delete revoke, and list
filtering (active-only vs full history).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, ServicePrincipalGrant, Target
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
    consult_and_record_grant,
    find_live_grant,
    null_target_only_park_hint,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000031a4")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000031b5")
_PRINCIPAL = "svc:deploy-bot"
_CONNECTOR = "vmware-rest-9.0"
_CREATOR = "op-admin"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _payload(
    *,
    op_id: str = "vmware.composite.vm.create",
    connector_id: str = _CONNECTOR,
    target_id: uuid.UUID | None = None,
    reason: str = "unattended substrate build",
    expires_at: datetime | None = None,
    principal_sub: str = _PRINCIPAL,
) -> ServiceGrantCreate:
    return ServiceGrantCreate(
        principal_sub=principal_sub,
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        reason=reason,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_create_returns_row_with_scopes() -> None:
    """A well-formed grant is created and echoes its exact scopes."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload())
    assert entry.principal_sub == _PRINCIPAL
    assert entry.op_id == "vmware.composite.vm.create"
    assert entry.connector_id == _CONNECTOR
    assert entry.target_id is None
    assert entry.reason == "unattended substrate build"
    assert entry.created_by_sub == _CREATOR
    assert entry.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, payload_kwargs",
    [
        ("op_id", {"op_id": "*"}),
        ("op_id", {"op_id": "vmware.composite.vm.*"}),
        ("connector_id", {"connector_id": "vmware-*"}),
        ("principal_sub", {"principal_sub": "svc:*"}),
    ],
)
async def test_create_refuses_wildcards(field: str, payload_kwargs: dict[str, str]) -> None:
    """Any glob in op_id / connector_id / principal_sub is refused (#3151)."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(**payload_kwargs))
    assert field in str(exc.value)
    assert "wildcard" in str(exc.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        # vCenter ?action= governance keys carry a literal query string as
        # part of their exact op id (vm.power / vm.deploy / host-software).
        "POST:/vcenter/vm/{vm}/power?action=start",
        "POST:/vcenter/vm/{vm}/guest/power?action=shutdown",
        "POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy",
        # multi-param, including a hyphenated param name (vmw-task).
        "POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true",
    ],
)
async def test_create_accepts_literal_query_string_op_id(op_id: str) -> None:
    """A literal ``?action=`` query string on an op_id is exact, not a glob."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))
    assert entry.op_id == op_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        "POST:/vcenter/vm/*",  # glob in the path
        "POST:/vcenter/vm/{vm}/power?action=*",  # glob in the value
        "POST:/vcenter/vm/?",  # bare ? (no key=value)
        "POST:/x?",  # ? with nothing after it
        "POST:/x?a",  # key with no '='
        "POST:/x?a=b?c=d",  # a second '?' (not '&')
        "*.delete",  # typed-op glob
    ],
)
async def test_create_refuses_malformed_query_or_glob_op_id(op_id: str) -> None:
    """A malformed ``?`` use or any ``*`` in op_id is still refused."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))
    assert "op_id" in str(exc.value)
    assert "wildcard" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_query_string_grant_round_trips_to_find_live_grant(
    session: AsyncSession,
) -> None:
    """A query-param op id is created and matched by an exact-string dispatch.

    Regression for the defect where ``?action=`` op ids were refused as
    wildcards: without a creatable grant, the power/deploy composite sub-ops
    always parked for service principals. Grant matching is exact string
    equality, so the same string used to create must match at dispatch.
    """
    op_id = "POST:/vcenter/vm/{vm}/power?action=start"
    svc = ServicePrincipalGrantService()
    created = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))

    matched = await find_live_grant(
        session,
        tenant_id=_TENANT_ID,
        principal_sub=_PRINCIPAL,
        op_id=op_id,
        connector_id=_CONNECTOR,
        target_id=None,
    )
    assert matched is not None
    assert matched.id == created.id
    assert matched.op_id == op_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        "DELETE:/vcenter/vm/{vm}",
        "vault.sys.policy.delete",
        "argocd.app.destroy",
        "keycloak.user.remove",
        "vault.token.purge",
    ],
)
async def test_create_refuses_delete_shaped_by_pattern(op_id: str) -> None:
    """Delete-shaped ops are never grantable — refused by configured pattern."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))
    assert "delete-shaped" in str(exc.value)


_VIM_DELETE_OP_IDS = (
    "POST:/VirtualMachine/{moId}/Destroy_Task",
    "POST:/VirtualApp/{moId}/Destroy_Task",
    "POST:/HostNetworkSystem/{moId}/RemovePortGroup",
    "POST:/FileManager/{moId}/DeleteDatastoreFile_Task",
    "POST:/VirtualMachine/{moId}/UnregisterVM",
)


async def _grant_rows_for(op_id: str) -> int:
    async with get_sessionmaker()() as s:
        count = await s.execute(
            select(func.count())
            .select_from(ServicePrincipalGrant)
            .where(ServicePrincipalGrant.op_id == op_id)
        )
        return int(count.scalar_one())


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _VIM_DELETE_OP_IDS)
async def test_create_refuses_vendor_delete_action_name(op_id: str) -> None:
    """A VI-JSON delete is a POST, so only its action name says it is a delete (#4632)."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))
    action_name = op_id.rsplit("/", 1)[-1]
    assert f"is a delete (vendor action name {action_name!r})" in str(exc.value)
    assert await _grant_rows_for(op_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    ["POST:/VirtualMachine/{moId}/Destroyer_Task", "POST:/VirtualMachine/{moId}/Reset_Task"],
)
async def test_create_allows_names_that_only_look_like_a_delete(op_id: str) -> None:
    """The same lookalike pair the parser test uses stays grantable."""
    svc = ServicePrincipalGrantService()
    row = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))
    assert row.op_id == op_id


@pytest.mark.asyncio
async def test_create_refuses_vendor_delete_action_name_without_pattern_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The action-name sign does not depend on SERVICE_GRANT_DELETE_SHAPED_PATTERNS."""
    monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", "acme.custom.nuke")
    get_settings.cache_clear()
    assert get_settings().service_grant_delete_shaped_patterns == ("acme.custom.nuke",)
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(
            _TENANT_ID, _CREATOR, _payload(op_id="POST:/VirtualMachine/{moId}/Destroy_Task")
        )
    assert "vendor action name 'Destroy_Task'" in str(exc.value)


def _service_operator() -> Operator:
    return Operator(
        sub=_PRINCIPAL,
        raw_jwt="svc-jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _seed_grant_row(op_id: str) -> None:
    """Write a grant row straight into the database, bypassing create's review."""
    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                tenant_id=_TENANT_ID,
                principal_sub=_PRINCIPAL,
                op_id=op_id,
                connector_id=_CONNECTOR,
                reason="written before the rule existed",
                created_by_sub=_CREATOR,
            )
        )
        await s.commit()


async def _grant_use_audit_rows() -> int:
    async with get_sessionmaker()() as s:
        count = await s.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.path == "approval.decision")
        )
        return int(count.scalar_one())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "descriptor_kwargs",
    [
        pytest.param(
            {"source_kind": "ingested", "method": "POST", "tags": ["write"]},
            id="ingested-style-record",
        ),
        pytest.param({"source_kind": "composite"}, id="inner-step-record"),
    ],
)
async def test_existing_grant_row_never_lets_a_vendor_delete_run(
    descriptor_kwargs: dict[str, object],
) -> None:
    """A stored grant on a VI-JSON delete is inert at dispatch (#4632).

    The inner step of a multi-step operation arrives as an in-memory record
    with only its declared level: no method, no tags. The op id is the only
    sign, and it is enough.
    """
    op_id = "POST:/VirtualMachine/{moId}/Destroy_Task"
    await _seed_grant_row(op_id)
    descriptor = EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        safety_level="dangerous",
        requires_approval=True,
        parameter_schema={},
        **descriptor_kwargs,
    )

    grant_id = await consult_and_record_grant(
        operator=_service_operator(),
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR,
    )

    assert grant_id is None
    assert await _grant_use_audit_rows() == 0


@pytest.mark.asyncio
async def test_existing_grant_row_still_clears_an_ordinary_write() -> None:
    """Control: the same seeded-row path auto-approves a non-delete inner step."""
    op_id = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
    await _seed_grant_row(op_id)
    descriptor = EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        source_kind="composite",
        safety_level="caution",
        requires_approval=False,
        parameter_schema={},
    )

    grant_id = await consult_and_record_grant(
        operator=_service_operator(),
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR,
    )

    assert grant_id is not None
    assert await _grant_use_audit_rows() == 1


@pytest.mark.asyncio
async def test_decide_only_consult_reports_the_grant_but_records_no_use() -> None:
    """``record=False`` answers "would a grant clear this?" and writes nothing (#4923).

    The preview asks the policy gate what the dispatcher would do; a grant
    that would clear the call is reported, but its use is not recorded, so
    a preview never shows up as an auto-approval on the audit ledger.
    """
    op_id = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
    await _seed_grant_row(op_id)
    descriptor = EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        source_kind="composite",
        safety_level="caution",
        requires_approval=False,
        parameter_schema={},
    )

    grant_id = await consult_and_record_grant(
        operator=_service_operator(),
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR,
        record=False,
    )

    assert grant_id is not None
    assert await _grant_use_audit_rows() == 0


@pytest.mark.asyncio
async def test_create_refuses_destructive_tagged_descriptor() -> None:
    """A non-delete-named op whose descriptor carries 'destructive' is refused.

    Belt-and-suspenders beyond the pattern set: the second, descriptor-level
    check catches a hand-tagged destructive typed op that the name globs miss.
    """
    # Seed a descriptor that does NOT match a delete-shaped name pattern but
    # is tagged destructive (vault-1.x → product/version/impl = vault//vault).
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                tenant_id=_TENANT_ID,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.sys.seal",
                source_kind="typed",
                method="POST",
                safety_level="dangerous",
                tags=["write", "destructive"],
                is_enabled=True,
            )
        )
        await s.commit()

    svc = ServicePrincipalGrantService()
    payload = _payload(op_id="vault.sys.seal", connector_id="vault-1.x")
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, payload)
    assert "destructive" in str(exc.value)


@pytest.mark.asyncio
async def test_create_refuses_destructive_tier_descriptor() -> None:
    """An op whose descriptor is in the ``destructive`` safety tier (#3183) is
    refused at grant creation, even without a delete-shaped name or the
    ``destructive`` tag — the tier itself is the authoritative signal.
    """
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                tenant_id=_TENANT_ID,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.sys.rekey",
                source_kind="typed",
                method="POST",
                safety_level="destructive",
                tags=["write"],
                is_enabled=True,
            )
        )
        await s.commit()

    svc = ServicePrincipalGrantService()
    payload = _payload(op_id="vault.sys.rekey", connector_id="vault-1.x")
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, payload)
    assert "destructive" in str(exc.value)


@pytest.mark.asyncio
async def test_create_refuses_past_and_naive_expiry() -> None:
    """A past or timezone-naive ``expires_at`` is refused."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError):
        await svc.create(
            _TENANT_ID, _CREATOR, _payload(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
    # A future but timezone-naive expiry is refused (must be tz-aware).
    naive_future = datetime(2999, 1, 1)
    with pytest.raises(GrantValidationError):
        await svc.create(_TENANT_ID, _CREATOR, _payload(expires_at=naive_future))


@pytest.mark.asyncio
async def test_create_refuses_duplicate_active_grant() -> None:
    """A second active grant for the same fully-scoped key is refused."""
    svc = ServicePrincipalGrantService()
    await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.power"))
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.power"))
    assert "already exists" in str(exc.value)


@pytest.mark.asyncio
async def test_revoke_is_soft_delete_and_allows_regrant() -> None:
    """Revoke stamps ``revoked_at`` (retains the row) and frees the key for re-grant."""
    svc = ServicePrincipalGrantService()
    op = "vmware.composite.vm.deploy"
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op))

    assert await svc.revoke(_TENANT_ID, entry.id, "op-revoker") is True
    revoked = await svc.get(_TENANT_ID, entry.id)
    assert revoked is not None
    assert revoked.revoked_at is not None
    assert revoked.revoked_by_sub == "op-revoker"
    # No reason given → no reason stored (a reason is optional on revoke).
    assert revoked.revoked_reason is None

    # Re-revoking an already-revoked grant is a no-op → False (404 at the boundary).
    assert await svc.revoke(_TENANT_ID, entry.id, "op-revoker") is False

    # The scope is free again: a fresh grant for the same key is accepted.
    again = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op))
    assert again.id != entry.id


@pytest.mark.asyncio
async def test_revoke_stores_optional_reason() -> None:
    """A reason passed to revoke is stored as ``revoked_reason`` on the row (#3489)."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.update"))

    assert await svc.revoke(_TENANT_ID, entry.id, "op-revoker", reason="bot retired") is True
    revoked = await svc.get(_TENANT_ID, entry.id)
    assert revoked is not None
    assert revoked.revoked_reason == "bot retired"


@pytest.mark.asyncio
async def test_revoke_absent_and_cross_tenant_returns_false() -> None:
    """Revoke of an absent / cross-tenant id returns False (no cross-tenant leak)."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.clone"))
    assert await svc.revoke(_TENANT_ID, uuid.uuid4(), "x") is False
    assert await svc.revoke(_OTHER_TENANT, entry.id, "x") is False


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none() -> None:
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.host.list"))
    assert await svc.get(_OTHER_TENANT, entry.id) is None


@pytest.mark.asyncio
async def test_list_excludes_revoked_by_default() -> None:
    """``list_`` hides revoked grants unless ``include_revoked=True``."""
    svc = ServicePrincipalGrantService()
    live = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.snap.create"))
    gone = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.snap.list"))
    await svc.revoke(_TENANT_ID, gone.id, "op-revoker")

    active = await svc.list_(_TENANT_ID, principal_sub=_PRINCIPAL)
    active_ids = {g.id for g in active}
    assert live.id in active_ids
    assert gone.id not in active_ids

    full = await svc.list_(_TENANT_ID, principal_sub=_PRINCIPAL, include_revoked=True)
    full_ids = {g.id for g in full}
    assert {live.id, gone.id} <= full_ids


@pytest.mark.asyncio
async def test_list_can_hide_expired_grants() -> None:
    """The explicit expiry filter keeps old REST list behavior by default."""
    svc = ServicePrincipalGrantService()
    live = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.live"))
    expired = await svc.create(
        _TENANT_ID,
        _CREATOR,
        _payload(
            op_id="vmware.composite.vm.expired",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        await db_session.execute(
            update(ServicePrincipalGrant)
            .where(ServicePrincipalGrant.id == expired.id)
            .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )

    historic = await svc.list_(_TENANT_ID, principal_sub=_PRINCIPAL)
    active = await svc.list_(
        _TENANT_ID,
        principal_sub=_PRINCIPAL,
        include_expired=False,
    )
    assert {live.id, expired.id} <= {entry.id for entry in historic}
    assert live.id in {entry.id for entry in active}
    assert expired.id not in {entry.id for entry in active}


# ---------------------------------------------------------------------------
# Target selectors (#3349)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_selector_grant_round_trips() -> None:
    """A selector grant is created and echoes its selector fields."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(
        _TENANT_ID,
        _CREATOR,
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_product="vmware",
            target_name_pattern="esx-dc*",
            reason="authorise runtime-created ESXi appliances",
        ),
    )
    assert entry.target_id is None
    assert entry.target_product == "vmware"
    assert entry.target_name_pattern == "esx-dc*"


def test_create_rejects_target_id_with_selector() -> None:
    """target_id and a selector are mutually exclusive at the schema boundary."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_id=uuid.uuid4(),
            target_product="vmware",
            reason="contradiction",
        )


@pytest.mark.asyncio
async def test_create_refuses_duplicate_active_selector() -> None:
    """A second live selector with the identical (product, pattern) is refused."""
    svc = ServicePrincipalGrantService()
    payload = ServiceGrantCreate(
        principal_sub=_PRINCIPAL,
        op_id="vmware.composite.vm.create",
        connector_id=_CONNECTOR,
        target_product="vmware",
        target_name_pattern="esx-dc*",
        reason="first",
    )
    await svc.create(_TENANT_ID, _CREATOR, payload)
    with pytest.raises(GrantValidationError, match="selector grant"):
        await svc.create(_TENANT_ID, _CREATOR, payload)


@pytest.mark.asyncio
async def test_create_selector_and_pure_targetless_coexist() -> None:
    """A selector grant and a pure targetless grant share the same key without collision."""
    svc = ServicePrincipalGrantService()
    await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.create"))
    # Same (principal, op, connector) but a selector — the narrowed targetless
    # unique index must not treat these as duplicates.
    entry = await svc.create(
        _TENANT_ID,
        _CREATOR,
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_product="vmware",
            reason="selector alongside targetless",
        ),
    )
    assert entry.target_product == "vmware"


@pytest.mark.asyncio
async def test_selector_grant_matches_target_fingerprint(session: AsyncSession) -> None:
    """A selector grant matches a target-scoped dispatch whose fingerprint fits.

    The core #3349 win: the target need not have existed at grant-create time.
    """
    svc = ServicePrincipalGrantService()
    created = await svc.create(
        _TENANT_ID,
        _CREATOR,
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_product="vmware",
            target_name_pattern="esx-dc*",
            reason="runtime-created appliances",
        ),
    )
    fresh_target_id = uuid.uuid4()  # a target that did not exist at create time

    matched = await find_live_grant(
        session,
        tenant_id=_TENANT_ID,
        principal_sub=_PRINCIPAL,
        op_id="vmware.composite.vm.create",
        connector_id=_CONNECTOR,
        target_id=fresh_target_id,
        target_product="vmware",
        target_name="esx-dc19",
    )
    assert matched is not None
    assert matched.id == created.id

    # Product mismatch -> no match.
    assert (
        await find_live_grant(
            session,
            tenant_id=_TENANT_ID,
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_id=fresh_target_id,
            target_product="k8s",
            target_name="esx-dc19",
        )
    ) is None

    # Name-glob mismatch -> no match.
    assert (
        await find_live_grant(
            session,
            tenant_id=_TENANT_ID,
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_id=fresh_target_id,
            target_product="vmware",
            target_name="mgmt-dc01",
        )
    ) is None


@pytest.mark.asyncio
async def test_selector_grant_does_not_match_targetless_dispatch(session: AsyncSession) -> None:
    """A selector needs a target fingerprint; it never covers a targetless dispatch."""
    svc = ServicePrincipalGrantService()
    await svc.create(
        _TENANT_ID,
        _CREATOR,
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_product="vmware",
            reason="selector only",
        ),
    )
    assert (
        await find_live_grant(
            session,
            tenant_id=_TENANT_ID,
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_id=None,
        )
    ) is None


@pytest.mark.asyncio
async def test_concrete_target_grant_wins_over_selector(session: AsyncSession) -> None:
    """A concrete-target grant is preferred over a matching selector."""
    svc = ServicePrincipalGrantService()
    concrete_id = uuid.uuid4()
    concrete = await svc.create(
        _TENANT_ID,
        _CREATOR,
        _payload(op_id="vmware.composite.vm.create", target_id=concrete_id),
    )
    await svc.create(
        _TENANT_ID,
        _CREATOR,
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_product="vmware",
            reason="broad selector",
        ),
    )
    matched = await find_live_grant(
        session,
        tenant_id=_TENANT_ID,
        principal_sub=_PRINCIPAL,
        op_id="vmware.composite.vm.create",
        connector_id=_CONNECTOR,
        target_id=concrete_id,
        target_product="vmware",
        target_name="esx-dc19",
    )
    assert matched is not None
    assert matched.id == concrete.id


@pytest.mark.asyncio
async def test_null_target_only_park_hint_fires_for_target_scoped_dispatch(
    session: AsyncSession,
) -> None:
    """A target-scoped dispatch with only a null-target grant surfaces the #3349 hint."""
    svc = ServicePrincipalGrantService()
    await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.create"))

    hint = await null_target_only_park_hint(
        session,
        tenant_id=_TENANT_ID,
        principal_sub=_PRINCIPAL,
        op_id="vmware.composite.vm.create",
        connector_id=_CONNECTOR,
        target_id=uuid.uuid4(),
    )
    assert hint is not None
    assert "null-target" in hint

    # A targetless dispatch has no mismatch to name.
    assert (
        await null_target_only_park_hint(
            session,
            tenant_id=_TENANT_ID,
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_id=None,
        )
    ) is None


# ---------------------------------------------------------------------------
# Orphaned grants (#3430)
# ---------------------------------------------------------------------------


async def _seed_target(name: str, *, deleted: bool = False) -> uuid.UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            Target(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product="vmware",
                host=f"{name}.test",
                port=443,
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
    return target_id


async def _soft_delete_target(target_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Target).where(Target.id == target_id).values(deleted_at=datetime.now(UTC))
        )


@pytest.mark.asyncio
async def test_grant_on_re_registered_target_is_orphaned_with_remediation() -> None:
    """Delete + register again under a new id: the old grant reads as orphaned.

    The live-target grant and a selector grant on the same op are not flagged.
    The orphaned grant is not revoked (read-time classification only).
    """
    svc = ServicePrincipalGrantService()
    old_id = await _seed_target("sddc-mgr")
    orphan = await svc.create(_TENANT_ID, _CREATOR, _payload(target_id=old_id))
    assert orphan.orphaned is False

    # Tear down and rebuild: the old row is soft-deleted, the new row gets a new id.
    await _soft_delete_target(old_id)
    new_id = await _seed_target("sddc-mgr")
    live = await svc.create(_TENANT_ID, _CREATOR, _payload(target_id=new_id))
    selector = await svc.create(
        _TENANT_ID,
        _CREATOR,
        ServiceGrantCreate(
            principal_sub=_PRINCIPAL,
            op_id="vmware.composite.vm.create",
            connector_id=_CONNECTOR,
            target_name_pattern="sddc-mgr",
            reason="survives a new registration",
        ),
    )
    targetless = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.power"))

    listed = {g.id: g for g in await svc.list_(_TENANT_ID)}
    assert listed[orphan.id].orphaned is True
    remediation = listed[orphan.id].orphan_remediation
    assert remediation is not None
    assert str(old_id) in remediation
    assert "target_name_pattern='sddc-mgr'" in remediation
    assert "revoke" in remediation
    for ok in (live, selector, targetless):
        assert listed[ok.id].orphaned is False
        assert listed[ok.id].orphan_remediation is None

    shown = await svc.get(_TENANT_ID, orphan.id)
    assert shown is not None
    assert shown.orphaned is True
    # Never auto-revoked: cleanup stays an operator decision.
    assert shown.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_name", "lookalike", "expected_pattern"),
    [
        ("example[1]", "example1", "example[[]1]"),
        ("ex*ample", "example", "ex[*]ample"),
        ("ex?ample", "exXample", "ex[?]ample"),
    ],
)
async def test_orphan_remediation_escapes_glob_characters_in_old_name(
    old_name: str, lookalike: str, expected_pattern: str
) -> None:
    """The suggested selector matches the old name literally, not a look-alike.

    ``target_name_pattern`` is an fnmatch glob. A raw ``example[1]`` would
    match ``example1`` and miss ``example[1]`` itself.
    """
    svc = ServicePrincipalGrantService()
    old_id = await _seed_target(old_name, deleted=True)
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(target_id=old_id))
    assert entry.orphaned is True
    remediation = entry.orphan_remediation
    assert remediation is not None
    assert f"target_name_pattern={expected_pattern!r}" in remediation

    pattern = re.search(r"target_name_pattern='([^']*)'", remediation)
    assert pattern is not None
    assert fnmatchcase(old_name, pattern.group(1))
    assert not fnmatchcase(lookalike, pattern.group(1))


@pytest.mark.asyncio
async def test_grant_on_unknown_target_id_is_orphaned_without_old_name() -> None:
    """A target id with no row at all (or in another tenant) is orphaned too."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(target_id=uuid.uuid4()))
    assert entry.orphaned is True
    assert entry.orphan_remediation is not None
    assert "target_name_pattern or target_product" in entry.orphan_remediation


@pytest.mark.asyncio
async def test_revoked_grant_on_deleted_target_is_not_flagged() -> None:
    """A revoked grant is already cleaned up, so it is not called orphaned."""
    svc = ServicePrincipalGrantService()
    target_id = await _seed_target("gone", deleted=True)
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(target_id=target_id))
    assert entry.orphaned is True
    assert await svc.revoke(_TENANT_ID, entry.id, _CREATOR)

    history = {g.id: g for g in await svc.list_(_TENANT_ID, include_revoked=True)}
    assert history[entry.id].orphaned is False
    assert history[entry.id].orphan_remediation is None
