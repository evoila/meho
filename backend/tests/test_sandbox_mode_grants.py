# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Delete grants in sandbox mode: creation, storage, listing, revocation (#4622).

* **Mode off** -- a grant with any delete sign is still refused (the
  existing tests in ``test_service_grants.py`` and
  ``test_api_v1_service_grants.py`` stay green; this file adds the vendor
  action name and the delete step), and ``sandbox_delete_grant`` is never
  written.
* **Mode on, console** -- a tenant admin creates a delete grant of each
  kind (level, verb, tag, name, vendor action name, delete step) with an
  expiry within 30 days; it is stored in ``sandbox_delete_grant``, never in
  ``service_principal_grant``. No expiry, or beyond 30 days, raises
  ``GrantValidationError`` and the console shows the error.
* **Console mark** -- only the mark the server sets counts; a request
  field, a header or a token claim that claims the console gets 403.
* **Mode on, REST** -- a person's token gets 403
  ``delete_grant_console_only``; an agent or service token gets 403
  ``human_principal_required``. A console session that holds a machine
  token gets the same 403 on the create and the renew routes.
* **Other operations** keep today's rules (expiry optional) in every mode.
* **Renewal as a set** -- the live delete grants of one service principal
  are renewed together, each checked like a new grant; one failed check
  renews nothing. Only the console renews, and only with the mode on.
* **List, get, revoke, the orphan check and the console page** cover both
  tables.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import tests.test_ui_agent_grants as agent_grants_test
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    SandboxDeleteGrant,
    ServicePrincipalGrant,
    Target,
)
from meho_backplane.main import app
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    DeleteGrantConsoleOnlyError,
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.sandbox_mode import SANDBOX_DELETE_GRANT_MAX_DAYS
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _ISSUER
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._sandbox_mode_helpers import (
    rows_in,
    seed_delete_grant,
    seed_grant_row,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)
from tests.test_ui_agent_grants import (
    _OP_A,
    _TENANT_A,
    _admin_token,
    _authenticated_client,
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_session_sync,
    _seed_tenant,
)

_OTHER_PRINCIPAL = "svc:other-runner"
_RENEW_URL = "/ui/grants/service/renew-delete-grants"

_TENANT = _TENANT_A
_PRINCIPAL = "svc:test-runner"
_CONNECTOR = "vmware-rest-9.0"
_CREATOR = "op-admin"
_BASE = "/api/v1/service-principals/grants"

#: One op id per delete sign. The level, verb and tag signs need a stored
#: descriptor; the name, the vendor action name and the delete step need
#: only the op id.
_DELETE_SIGNS: dict[str, tuple[str, dict[str, Any] | None]] = {
    "level": (
        "vmware.composite.vm.destroy",
        {"source_kind": "composite", "safety_level": "destructive", "tags": ["write"]},
    ),
    "verb": (
        "DELETE:/vcenter/vm/{vm}",
        {"source_kind": "ingested", "method": "DELETE", "safety_level": "dangerous"},
    ),
    "tag": (
        "vault.sys.seal",
        {
            "source_kind": "typed",
            "method": "POST",
            "safety_level": "dangerous",
            "tags": ["write", "destructive"],
        },
    ),
    "name": ("vault.sys.policy.delete", None),
    "vendor_action_name": ("POST:/VirtualMachine/{moId}/Destroy_Task", None),
    "delete_step": ("DELETE:/vcenter/namespaces/instances/{namespace}", None),
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_descriptor(op_id: str, fields: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=_TENANT,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id=op_id,
                is_enabled=True,
                requires_approval=False,
                parameter_schema={},
                **fields,
            )
        )
        await session.commit()


def _payload(op_id: str, **overrides: Any) -> ServiceGrantCreate:
    fields: dict[str, Any] = {
        "principal_sub": _PRINCIPAL,
        "op_id": op_id,
        "connector_id": _CONNECTOR,
        "reason": "disposable test run",
        "expires_at": soon(),
    }
    fields.update(overrides)
    return ServiceGrantCreate(**fields)


async def _grant_rows(model: Any, op_id: str) -> int:
    async with get_sessionmaker()() as session:
        return len(
            (await session.execute(select(model).where(model.op_id == op_id))).scalars().all()
        )


# ---------------------------------------------------------------------------
# Mode off: every delete sign is still refused; the delete-grant table stays empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", sorted(_DELETE_SIGNS))
async def test_mode_off_refuses_every_delete_sign(
    monkeypatch: pytest.MonkeyPatch, sign: str
) -> None:
    turn_sandbox_mode_off(monkeypatch)
    op_id, fields = _DELETE_SIGNS[sign]
    if fields is not None:
        await _seed_descriptor(op_id, fields)
    with pytest.raises(GrantValidationError):
        await ServicePrincipalGrantService().create(
            _TENANT, _CREATOR, _payload(op_id), from_console=True
        )
    assert await rows_in(SandboxDeleteGrant) == 0
    assert await rows_in(ServicePrincipalGrant) == 0


# ---------------------------------------------------------------------------
# Mode on, console: one delete grant per sign, stored in the delete-grant table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", sorted(_DELETE_SIGNS))
async def test_console_creates_a_delete_grant_of_each_kind(
    monkeypatch: pytest.MonkeyPatch, sign: str
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    op_id, fields = _DELETE_SIGNS[sign]
    if fields is not None:
        await _seed_descriptor(op_id, fields)
    entry = await ServicePrincipalGrantService().create(
        _TENANT, _CREATOR, _payload(op_id), from_console=True
    )
    assert entry.delete_grant is True
    assert entry.op_id == op_id
    assert await _grant_rows(SandboxDeleteGrant, op_id) == 1
    assert await _grant_rows(ServicePrincipalGrant, op_id) == 0


@pytest.mark.asyncio
async def test_delete_grant_needs_an_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    with pytest.raises(GrantValidationError, match="needs an expiry"):
        await ServicePrincipalGrantService().create(
            _TENANT,
            _CREATOR,
            _payload("vault.sys.policy.delete", expires_at=None),
            from_console=True,
        )
    assert await rows_in(SandboxDeleteGrant) == 0


@pytest.mark.asyncio
async def test_delete_grant_expiry_within_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    too_late = soon(SANDBOX_DELETE_GRANT_MAX_DAYS) + timedelta(hours=1)
    with pytest.raises(GrantValidationError, match="must expire within 30 days"):
        await ServicePrincipalGrantService().create(
            _TENANT,
            _CREATOR,
            _payload("vault.sys.policy.delete", expires_at=too_late),
            from_console=True,
        )
    assert await rows_in(SandboxDeleteGrant) == 0
    entry = await ServicePrincipalGrantService().create(
        _TENANT,
        _CREATOR,
        _payload("vault.sys.policy.delete", expires_at=soon(SANDBOX_DELETE_GRANT_MAX_DAYS - 1)),
        from_console=True,
    )
    assert entry.delete_grant is True


@pytest.mark.asyncio
async def test_delete_grant_needs_the_console_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    with pytest.raises(DeleteGrantConsoleOnlyError):
        await ServicePrincipalGrantService().create(
            _TENANT, _CREATOR, _payload("vault.sys.policy.delete")
        )
    assert await rows_in(SandboxDeleteGrant) == 0


@pytest.mark.asyncio
async def test_ordinary_grant_keeps_todays_rules_with_the_mode_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No delete sign: expiry optional, stored in the ordinary table, console mark not needed."""
    turn_sandbox_mode_on(monkeypatch)
    entry = await ServicePrincipalGrantService().create(
        _TENANT, _CREATOR, _payload("vmware.composite.vm.power", expires_at=None)
    )
    assert entry.delete_grant is False
    assert entry.expires_at is None
    assert await rows_in(ServicePrincipalGrant) == 1
    assert await rows_in(SandboxDeleteGrant) == 0


# ---------------------------------------------------------------------------
# Renewal: the delete grants of one service principal are renewed as one set
# ---------------------------------------------------------------------------


async def _all_delete_grants() -> dict[uuid.UUID, SandboxDeleteGrant]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(SandboxDeleteGrant))).scalars().all()
    return {row.id: row for row in rows}


async def _seed_renewal_set() -> dict[str, uuid.UUID]:
    """Three live delete grants for the principal, plus the rows a renewal must skip."""
    await _seed_descriptor(*_DELETE_SIGNS["verb"])
    live_name = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="vault.sys.policy.delete",
        connector_id=_CONNECTOR,
        expires_at=soon(2),
    )
    live_verb = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="DELETE:/vcenter/vm/{vm}",
        connector_id=_CONNECTOR,
        expires_at=soon(3),
    )
    live_selector = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="POST:/VirtualMachine/{moId}/Destroy_Task",
        connector_id=_CONNECTOR,
        target_product="vmware",
        target_name_pattern="disposable-*",
        expires_at=soon(4),
    )
    expired = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="DELETE:/vcenter/namespaces/instances/{namespace}",
        connector_id=_CONNECTOR,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    revoked = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="vault.sys.seal",
        connector_id=_CONNECTOR,
        revoked_at=datetime.now(UTC) - timedelta(hours=1),
    )
    other = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_OTHER_PRINCIPAL,
        op_id="vault.sys.policy.delete",
        connector_id=_CONNECTOR,
        expires_at=soon(2),
    )
    return {
        "live_name": live_name,
        "live_verb": live_verb,
        "live_selector": live_selector,
        "expired": expired,
        "revoked": revoked,
        "other": other,
    }


async def _assert_set_unchanged(seeded: dict[str, uuid.UUID]) -> None:
    rows = await _all_delete_grants()
    assert set(rows) == set(seeded.values())
    for key in ("live_name", "live_verb", "live_selector", "expired", "other"):
        assert rows[seeded[key]].revoked_at is None
    assert await rows_in(ServicePrincipalGrant) == 0


@pytest.mark.asyncio
async def test_renewing_a_set_renews_every_live_delete_grant_of_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three live grants get one new expiry; the expired, revoked and other rows stay."""
    turn_sandbox_mode_on(monkeypatch)
    seeded = await _seed_renewal_set()
    new_expiry = soon(20)

    renewed = await ServicePrincipalGrantService().renew_delete_grants(
        _TENANT, _PRINCIPAL, "op-renewer", expires_at=new_expiry, from_console=True
    )

    assert len(renewed) == 3
    assert {entry.op_id for entry in renewed} == {
        "vault.sys.policy.delete",
        "DELETE:/vcenter/vm/{vm}",
        "POST:/VirtualMachine/{moId}/Destroy_Task",
    }
    assert all(entry.delete_grant for entry in renewed)
    assert all(entry.created_by_sub == "op-renewer" for entry in renewed)
    assert all(entry.expires_at is not None for entry in renewed)
    assert {entry.expires_at.replace(tzinfo=UTC) for entry in renewed if entry.expires_at} == {
        new_expiry
    }
    by_op = {entry.op_id: entry for entry in renewed}
    selector = by_op["POST:/VirtualMachine/{moId}/Destroy_Task"]
    assert (selector.target_product, selector.target_name_pattern) == ("vmware", "disposable-*")
    assert by_op["vault.sys.policy.delete"].reason == "seeded by a sandbox-mode test"

    rows = await _all_delete_grants()
    assert len(rows) == 9
    for key in ("live_name", "live_verb", "live_selector"):
        old = rows[seeded[key]]
        assert old.revoked_at is not None
        assert old.revoked_by_sub == "op-renewer"
        assert old.revoked_reason == "renewed"
    assert rows[seeded["other"]].revoked_at is None
    assert rows[seeded["expired"]].revoked_at is None
    assert await rows_in(ServicePrincipalGrant) == 0

    live = await ServicePrincipalGrantService().list_(
        _TENANT, principal_sub=_PRINCIPAL, include_expired=False
    )
    assert {entry.id for entry in live} == {entry.id for entry in renewed}


@pytest.mark.asyncio
async def test_renewing_a_set_replaces_the_reason_when_one_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await _seed_renewal_set()
    renewed = await ServicePrincipalGrantService().renew_delete_grants(
        _TENANT,
        _PRINCIPAL,
        _CREATOR,
        expires_at=soon(10),
        reason="monthly review",
        from_console=True,
    )
    assert {entry.reason for entry in renewed} == {"monthly review"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["no_expiry", "too_late", "lost_delete_sign"])
async def test_renewing_a_set_checks_each_grant_like_a_new_one(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """One failed check refuses the whole set and changes nothing."""
    turn_sandbox_mode_on(monkeypatch)
    seeded = await _seed_renewal_set()
    expires_at = soon(10)
    if fault == "no_expiry":
        expires_at = None
        expected = "needs an expiry"
    elif fault == "too_late":
        expires_at = soon(SANDBOX_DELETE_GRANT_MAX_DAYS) + timedelta(hours=1)
        expected = "must expire within 30 days"
    else:
        # With other name patterns, ``vault.sys.policy.delete`` has no delete
        # sign any more. The verb grant still has one. The set is refused.
        monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", "acme.custom.nuke")
        get_settings.cache_clear()
        expected = "no longer has a delete sign"
    with pytest.raises(GrantValidationError, match=expected):
        await ServicePrincipalGrantService().renew_delete_grants(
            _TENANT, _PRINCIPAL, _CREATOR, expires_at=expires_at, from_console=True
        )
    await _assert_set_unchanged(seeded)


@pytest.mark.asyncio
async def test_renewing_a_set_needs_the_console_the_mode_and_a_live_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    seeded = await _seed_renewal_set()
    svc = ServicePrincipalGrantService()
    with pytest.raises(DeleteGrantConsoleOnlyError):
        await svc.renew_delete_grants(_TENANT, _PRINCIPAL, _CREATOR, expires_at=soon(10))
    with pytest.raises(GrantValidationError, match="no live delete grant"):
        await svc.renew_delete_grants(
            _TENANT, "svc:nobody", _CREATOR, expires_at=soon(10), from_console=True
        )
    await _assert_set_unchanged(seeded)

    turn_sandbox_mode_off(monkeypatch)
    with pytest.raises(GrantValidationError, match="not in sandbox mode"):
        await svc.renew_delete_grants(
            _TENANT, _PRINCIPAL, _CREATOR, expires_at=soon(10), from_console=True
        )
    turn_sandbox_mode_on(monkeypatch)
    await _assert_set_unchanged(seeded)


@pytest.mark.asyncio
async def test_renewing_a_set_writes_nothing_on_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live ordinary-table row cannot collide, but a second live delete row can."""
    turn_sandbox_mode_on(monkeypatch)
    seeded = await _seed_renewal_set()
    # A second live selector row with the same scope, written past the CRUD
    # check: the renewal's own duplicate check refuses it, after the first
    # two grants of the set were already written in the same transaction.
    duplicate = await seed_grant_row(
        model=SandboxDeleteGrant,
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="POST:/VirtualMachine/{moId}/Destroy_Task",
        connector_id=_CONNECTOR,
        target_product="vmware",
        target_name_pattern="disposable-*",
        expires_at=soon(5),
    )
    seeded["duplicate"] = duplicate
    with pytest.raises(GrantValidationError, match="already exists"):
        await ServicePrincipalGrantService().renew_delete_grants(
            _TENANT, _PRINCIPAL, _CREATOR, expires_at=soon(10), from_console=True
        )
    rows = await _all_delete_grants()
    assert set(rows) == set(seeded.values())
    assert all(rows[seeded[k]].revoked_at is None for k in ("live_name", "live_verb"))


# ---------------------------------------------------------------------------
# List, get, revoke and the orphan check cover both tables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_get_revoke_cover_both_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    svc = ServicePrincipalGrantService()
    ordinary = await svc.create(_TENANT, _CREATOR, _payload("vmware.composite.vm.power"))
    delete = await svc.create(
        _TENANT, _CREATOR, _payload("vault.sys.policy.delete"), from_console=True
    )

    listed = {entry.id: entry for entry in await svc.list_(_TENANT)}
    assert set(listed) == {ordinary.id, delete.id}
    assert listed[delete.id].delete_grant is True
    assert listed[ordinary.id].delete_grant is False
    assert [entry.id for entry in await svc.list_(_TENANT, limit=1)] == [delete.id]
    assert [entry.id for entry in await svc.list_(_TENANT, limit=1, offset=1)] == [ordinary.id]

    shown = await svc.get(_TENANT, delete.id)
    assert shown is not None and shown.delete_grant is True

    assert await svc.revoke(_TENANT, delete.id, _CREATOR, reason="done") is True
    assert await svc.revoke(_TENANT, delete.id, _CREATOR) is False
    assert [entry.id for entry in await svc.list_(_TENANT)] == [ordinary.id]
    history = await svc.get(_TENANT, delete.id)
    assert history is not None and history.revoked_reason == "done"


@pytest.mark.asyncio
async def test_mode_off_never_reads_the_delete_grant_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delete grant row exists; with the mode off, list and get do not see it."""
    turn_sandbox_mode_on(monkeypatch)
    delete_id = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="vault.sys.policy.delete",
        connector_id=_CONNECTOR,
    )
    turn_sandbox_mode_off(monkeypatch)
    svc = ServicePrincipalGrantService()
    assert await svc.list_(_TENANT) == []
    assert await svc.get(_TENANT, delete_id) is None
    assert await svc.revoke(_TENANT, delete_id, _CREATOR) is False


@pytest.mark.asyncio
async def test_orphan_check_covers_a_delete_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    gone = uuid.uuid4()
    delete_id = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_PRINCIPAL,
        op_id="vault.sys.policy.delete",
        connector_id=_CONNECTOR,
        target_id=gone,
    )
    (entry,) = await ServicePrincipalGrantService().list_(_TENANT)
    assert entry.id == delete_id
    assert entry.orphaned is True
    assert entry.orphan_remediation is not None


# ---------------------------------------------------------------------------
# REST: a delete grant is refused with the right code for each token kind
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)


_DELETE_BODY = {
    "principal_sub": _PRINCIPAL,
    "op_id": "vault.sys.policy.delete",
    "connector_id": _CONNECTOR,
    "reason": "disposable test run",
}


def _person_token(key: Any) -> str:
    return mint_token(
        key, sub="op-person", tenant_role=TenantRole.OPERATOR.value, tenant_id=str(_TENANT)
    )


def _post(client: TestClient, token: str, body: dict[str, Any], **headers: str) -> Any:
    return client.post(_BASE, headers={"Authorization": f"Bearer {token}", **headers}, json=body)


def test_rest_person_gets_delete_grant_console_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    key = make_rsa_keypair("kid-person")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = _post(
            client, _person_token(key), {**_DELETE_BODY, "expires_at": soon().isoformat()}
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("delete_grant_console_only")
    assert asyncio.run(rows_in(SandboxDeleteGrant)) == 0


@pytest.mark.parametrize("kind", ["agent", "service"])
def test_rest_machine_token_gets_human_principal_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    key = make_rsa_keypair(f"kid-{kind}")
    token = mint_token(
        key,
        sub=f"machine-{kind}",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT),
        principal_kind=kind,
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = _post(client, token, {**_DELETE_BODY, "expires_at": soon().isoformat()})
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("human_principal_required")


def test_rest_mode_off_still_answers_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_off(monkeypatch)
    key = make_rsa_keypair("kid-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = _post(client, _person_token(key), _DELETE_BODY)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "claim",
    ["request_field", "header", "token_claim"],
)
def test_rest_cannot_claim_the_console(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    """Only the mark the server sets counts. Nothing a caller sends stands in for it."""
    turn_sandbox_mode_on(monkeypatch)
    key = make_rsa_keypair(f"kid-{claim}")
    body: dict[str, Any] = {**_DELETE_BODY, "expires_at": soon().isoformat()}
    headers: dict[str, str] = {}
    extra_claims: dict[str, Any] = {}
    if claim == "request_field":
        body["from_console"] = True
    elif claim == "header":
        headers["X-MEHO-From-Console"] = "true"
    else:
        extra_claims = {"from_console": True, "web_session": True}
    token = mint_token(
        key,
        sub="op-person",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT),
        extra_claims=extra_claims or None,
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = _post(client, token, body, **headers)
    # An unknown body field is a 422 at the boundary; the other two reach
    # the service and get the console-only refusal. Either way: no grant.
    assert response.status_code in (403, 422), response.text
    if response.status_code == 403:
        assert response.json()["detail"].startswith("delete_grant_console_only")
    assert asyncio.run(rows_in(SandboxDeleteGrant)) == 0


def test_rest_ordinary_grant_without_expiry_with_the_mode_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    key = make_rsa_keypair("kid-ordinary")
    body = {**_DELETE_BODY, "op_id": "vmware.composite.vm.power"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = _post(client, _person_token(key), body)
    assert response.status_code == 201, response.text
    assert response.json()["delete_grant"] is False
    assert response.json()["expires_at"] is None


# ---------------------------------------------------------------------------
# Console: the grant page creates, lists and refuses delete grants
# ---------------------------------------------------------------------------


@pytest.fixture
def _console_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from agent_grants_test._bff_env.__wrapped__(monkeypatch)


def _admin_client(*, csrf: bool = False) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=_admin_token(keypair), operator_sub=_OP_A
    )
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=csrf)


def _machine_admin_client(kind: str, *, csrf: bool = False) -> Any:
    """A console session whose access token is a machine login with the admin role."""
    keypair, jwks = _make_keypair_and_jwks()
    sub = f"machine-{kind}"
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        principal_kind=kind,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=sub)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=csrf)


def _renew_form(**overrides: str) -> dict[str, str]:
    form = {"principal_sub": _PRINCIPAL, "expires_at": soon(20).isoformat(), "reason": ""}
    form.update(overrides)
    return form


def _console_form(**overrides: str) -> dict[str, str]:
    form = {
        "kind": "service",
        "principal_sub": _PRINCIPAL,
        "op": "vault.sys.policy.delete",
        "connector_id": _CONNECTOR,
        "target_id": "",
        "target_product": "",
        "target_name_pattern": "",
        "reason": "disposable test run",
        "expires_at": soon().isoformat(),
    }
    form.update(overrides)
    return form


@pytest.mark.usefixtures("_console_env")
def test_console_creates_and_lists_a_delete_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, token = _admin_client(csrf=True)
    try:
        created = client.post(
            "/ui/grants/create", data=_console_form(), headers=_csrf_headers(token)
        )
        assert created.status_code == 204, created.text
        page = client.get("/ui/grants")
    finally:
        mock.stop()
    assert page.status_code == 200
    assert 'data-delete-grant="true"' in page.text
    assert "delete grant" in page.text
    assert "data-sandbox-banner" in page.text
    assert asyncio.run(rows_in(SandboxDeleteGrant)) == 1
    assert asyncio.run(rows_in(ServicePrincipalGrant)) == 0


@pytest.mark.usefixtures("_console_env")
@pytest.mark.parametrize("expires_at", ["", "far"])
def test_console_shows_the_expiry_error(monkeypatch: pytest.MonkeyPatch, expires_at: str) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    value = (
        (soon(SANDBOX_DELETE_GRANT_MAX_DAYS) + timedelta(days=1)).isoformat()
        if expires_at == "far"
        else ""
    )
    client, mock, token = _admin_client(csrf=True)
    try:
        response = client.post(
            "/ui/grants/create", data=_console_form(expires_at=value), headers=_csrf_headers(token)
        )
    finally:
        mock.stop()
    assert response.status_code == 422
    assert "delete grant" in response.text
    assert "30 days" in response.text
    # The refusal shows at the top of the form, not only in the sandbox note.
    assert '<div class="alert alert-error" role="alert">' in response.text
    assert asyncio.run(rows_in(SandboxDeleteGrant)) == 0


@pytest.mark.usefixtures("_console_env")
@pytest.mark.parametrize("kind", ["agent", "service"])
@pytest.mark.parametrize("route", ["create", "renew"])
def test_console_refuses_a_machine_session(
    monkeypatch: pytest.MonkeyPatch, kind: str, route: str
) -> None:
    """A tenant-admin role on a machine token does not make or renew a grant."""
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    asyncio.run(_seed_renewal_set())
    before = asyncio.run(rows_in(SandboxDeleteGrant))
    client, mock, token = _machine_admin_client(kind, csrf=True)
    try:
        if route == "create":
            response = client.post(
                "/ui/grants/create", data=_console_form(), headers=_csrf_headers(token)
            )
        else:
            response = client.post(_RENEW_URL, data=_renew_form(), headers=_csrf_headers(token))
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert "human_principal_required" in response.text
    assert asyncio.run(rows_in(SandboxDeleteGrant)) == before
    assert asyncio.run(rows_in(ServicePrincipalGrant)) == 0
    rows = asyncio.run(_all_delete_grants())
    assert sum(1 for row in rows.values() if row.revoked_reason == "renewed") == 0


@pytest.mark.usefixtures("_console_env")
def test_console_refuses_an_ordinary_grant_from_a_machine_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_off(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, token = _machine_admin_client("service", csrf=True)
    try:
        response = client.post(
            "/ui/grants/create",
            data=_console_form(op="vmware.composite.vm.power", expires_at=""),
            headers=_csrf_headers(token),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert asyncio.run(rows_in(ServicePrincipalGrant)) == 0


@pytest.mark.usefixtures("_console_env")
def test_console_renews_the_delete_grants_of_one_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    seeded = asyncio.run(_seed_renewal_set())
    client, mock, token = _admin_client(csrf=True)
    try:
        page = client.get(f"/ui/grants?principal={_PRINCIPAL}")
        assert "data-renew-delete-grants" in page.text
        assert "4 delete grants expire within 7 days" in page.text
        renewed = client.post(_RENEW_URL, data=_renew_form(), headers=_csrf_headers(token))
        after = client.get(f"/ui/grants?principal={_PRINCIPAL}")
    finally:
        mock.stop()
    assert renewed.status_code == 204, renewed.text
    assert renewed.headers["HX-Redirect"] == "/ui/grants?principal=svc%3Atest-runner"
    # Only the other principal's grant still expires within 7 days.
    assert "1 delete grant expires within 7 days" in after.text
    rows = asyncio.run(_all_delete_grants())
    assert len(rows) == 9
    for key in ("live_name", "live_verb", "live_selector"):
        assert rows[seeded[key]].revoked_reason == "renewed"
    new_rows = [row for row in rows.values() if row.id not in seeded.values()]
    assert len(new_rows) == 3
    assert all(row.created_by_sub == _OP_A for row in new_rows)
    assert all(str(row.id) in after.text for row in new_rows)


@pytest.mark.usefixtures("_console_env")
@pytest.mark.parametrize("fault", ["far", "blank", "not_a_time", "no_principal"])
def test_console_shows_the_renewal_error(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    seeded = asyncio.run(_seed_renewal_set())
    form = _renew_form()
    if fault == "far":
        form["expires_at"] = (soon(SANDBOX_DELETE_GRANT_MAX_DAYS) + timedelta(days=1)).isoformat()
    elif fault == "blank":
        form["expires_at"] = ""
    elif fault == "not_a_time":
        form["expires_at"] = "next month"
    else:
        form["principal_sub"] = " "
    client, mock, token = _admin_client(csrf=True)
    try:
        response = client.post(_RENEW_URL, data=form, headers=_csrf_headers(token))
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "data-renew-delete-grants" in response.text
    assert 'role="alert"' in response.text
    asyncio.run(_assert_set_unchanged(seeded))


@pytest.mark.usefixtures("_console_env")
def test_console_mode_off_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_off(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, token = _admin_client(csrf=True)
    try:
        response = client.post(
            "/ui/grants/create", data=_console_form(), headers=_csrf_headers(token)
        )
        page = client.get("/ui/grants")
        renewed = client.post(_RENEW_URL, data=_renew_form(), headers=_csrf_headers(token))
    finally:
        mock.stop()
    assert response.status_code == 422
    assert "delete-shaped" in response.text
    assert "data-sandbox-banner" not in page.text
    assert "data-renew-delete-grants" not in page.text
    assert renewed.status_code == 422
    assert "not in sandbox mode" in renewed.text
    assert asyncio.run(rows_in(SandboxDeleteGrant)) == 0


@pytest.mark.usefixtures("_console_env")
def test_console_revokes_a_delete_grant_and_counts_expiring_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    delete_id = asyncio.run(
        seed_delete_grant(
            tenant_id=_TENANT_A,
            principal_sub=_PRINCIPAL,
            op_id="vault.sys.policy.delete",
            connector_id=_CONNECTOR,
            expires_at=soon(3),
        )
    )
    asyncio.run(
        seed_delete_grant(
            tenant_id=_TENANT_A,
            principal_sub=_PRINCIPAL,
            op_id="DELETE:/vcenter/vm/{vm}",
            connector_id=_CONNECTOR,
            expires_at=soon(20),
        )
    )
    client, mock, token = _admin_client(csrf=True)
    try:
        page = client.get("/ui/grants")
        assert "1 delete grant expires within 7 days" in page.text
        revoked = client.post(
            f"/ui/grants/service/{delete_id}/revoke", headers=_csrf_headers(token)
        )
        after = client.get("/ui/grants")
    finally:
        mock.stop()
    assert revoked.status_code == 204
    assert "expires within 7 days" not in after.text

    async def _row() -> SandboxDeleteGrant:
        async with get_sessionmaker()() as session:
            return (
                await session.execute(
                    select(SandboxDeleteGrant).where(SandboxDeleteGrant.id == delete_id)
                )
            ).scalar_one()

    assert asyncio.run(_row()).revoked_at is not None


@pytest.mark.usefixtures("_console_env")
def test_console_marks_an_orphaned_delete_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")

    async def _seed() -> uuid.UUID:
        target_id = uuid.uuid4()
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                Target(
                    id=target_id,
                    tenant_id=_TENANT_A,
                    name="gone-vcenter",
                    aliases=[],
                    product="vmware",
                    host="gone.test",
                    port=443,
                )
            )
        return target_id

    target_id = asyncio.run(_seed())
    asyncio.run(
        seed_delete_grant(
            tenant_id=_TENANT_A,
            principal_sub=_PRINCIPAL,
            op_id="vault.sys.policy.delete",
            connector_id=_CONNECTOR,
            target_id=target_id,
        )
    )

    async def _delete_target() -> None:
        from datetime import UTC, datetime

        async with get_sessionmaker()() as session, session.begin():
            row = await session.get(Target, target_id)
            assert row is not None
            row.deleted_at = datetime.now(UTC)

    asyncio.run(_delete_target())
    client, mock, _token = _admin_client()
    try:
        page = client.get("/ui/grants")
    finally:
        mock.stop()
    assert "orphaned" in page.text
    assert "data-grant-remediation" in page.text
