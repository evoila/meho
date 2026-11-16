# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``harbor.robot.create`` with ``vault_target``: the new robot secret goes to Vault.

Harbor is faked with respx and Vault with the in-process fake. The secret
Harbor returns is a made-up value; each test checks that it reaches no
output, only the fake Vault write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import hvac.exceptions
import pytest
import respx
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops_robot_vault import (
    ROBOT_SECRET_NOT_STORED,
    RobotVaultTargetError,
    check_robot_vault_target,
    parse_vault_target,
    robot_vault_preflight,
    secret_destination,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel, OperationResult
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_NEW_SECRET = "made-up-robot-secret-must-not-leak"
_TENANT = UUID("00000000-0000-0000-0000-00000000a0a3")
_DEST_PATH = f"tenants/{_TENANT}/ci/harbor"
_VAULT_TARGET = f"vault:{_DEST_PATH}#secret"
_ROBOTS_URL = "https://harbor.test.invalid/api/v2.0/robots"
_PARAMS: dict[str, Any] = {
    "name": "ci-push",
    "project": "myproject",
    "duration": -1,
    "vault_target": _VAULT_TARGET,
}
_HARBOR_ANSWER = {"id": 42, "name": "robot$myproject+ci-push", "secret": _NEW_SECRET}


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="harbor-test",
    host="harbor.test.invalid",
    port=443,
    secret_ref="targets/harbor/harbor-test",
)


def _operator() -> Operator:
    return Operator(
        sub="agent-robot-vault",
        name="Agent",
        email=None,
        raw_jwt="op.robot.vault.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _robot_read(mock: respx.MockRouter) -> None:
    mock.get(f"{_ROBOTS_URL}/42").mock(
        return_value=respx.MockResponse(
            200,
            json={
                "id": 42,
                "name": "robot$myproject+ci-push",
                "level": "project",
                "permissions": [{"kind": "project", "namespace": "myproject", "access": []}],
            },
        )
    )


def _install_vault(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    return install_fake_client(
        monkeypatch, secret={"username": "svc", "password": "svc-pass"}, **kwargs
    )


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_goes_to_vault_and_the_answer_has_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock, capture_logs() as logs:
        mock.post(_ROBOTS_URL).mock(return_value=respx.MockResponse(201, json=_HARBOR_ANSWER))
        result = await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))

    assert fake.secrets.kv.v2.patch_calls == [
        {"path": _DEST_PATH, "secret": {"secret": _NEW_SECRET}, "mount_point": "secret"}
    ]
    assert result["id"] == 42
    assert result["name"] == "robot$myproject+ci-push"
    assert result["status"] == "stored"
    assert result["stored_to"]["path"] == _DEST_PATH
    assert result["stored_to"]["field"] == "secret"
    assert result["length"] == len(_NEW_SECRET)
    assert len(result["value_fingerprint"]) == 64
    assert "secret" not in result
    assert _NEW_SECRET not in repr(result)
    assert _NEW_SECRET not in repr(logs)
    await connector.aclose()


@pytest.mark.asyncio
async def test_other_fields_of_the_vault_item_stay_by_merging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write is a merge (patch), never a whole-item replace."""
    fake = _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(return_value=respx.MockResponse(201, json=_HARBOR_ANSWER))
        await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))
    assert len(fake.secrets.kv.v2.patch_calls) == 1
    assert fake.secrets.kv.v2.put_calls == []
    await connector.aclose()


@pytest.mark.asyncio
async def test_missing_vault_item_is_created_only_when_still_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch, patch_exc=hvac.exceptions.InvalidPath("none"))
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(return_value=respx.MockResponse(201, json=_HARBOR_ANSWER))
        result = await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))
    assert fake.secrets.kv.v2.put_calls[0]["cas"] == 0
    assert fake.secrets.kv.v2.put_calls[0]["secret"] == {"secret": _NEW_SECRET}
    assert result["status"] == "stored"
    await connector.aclose()


@pytest.mark.asyncio
async def test_failed_vault_write_deletes_the_new_robot_and_hides_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch, patch_exc=hvac.exceptions.Forbidden(f"denied {_NEW_SECRET}"))
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(return_value=respx.MockResponse(201, json=_HARBOR_ANSWER))
        _robot_read(mock)
        delete_route = mock.delete(f"{_ROBOTS_URL}/42").mock(return_value=respx.MockResponse(200))
        result = await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))
    assert delete_route.called
    assert isinstance(result, OperationResult)
    assert result.status == "error"
    assert result.extras["error_code"] == ROBOT_SECRET_NOT_STORED
    assert result.extras["exception_class"] == "Forbidden"
    assert result.extras["robot_id"] == 42
    assert result.extras["robot_deleted"] is True
    assert "was deleted" in (result.error or "")
    assert _NEW_SECRET not in result.model_dump_json()
    await connector.aclose()


@pytest.mark.asyncio
async def test_failed_vault_write_says_when_the_robot_could_not_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch, patch_exc=hvac.exceptions.VaultDown("down"))
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(return_value=respx.MockResponse(201, json=_HARBOR_ANSWER))
        _robot_read(mock)
        mock.delete(f"{_ROBOTS_URL}/42").mock(return_value=respx.MockResponse(500))
        result = await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))
    assert isinstance(result, OperationResult)
    assert result.extras["robot_deleted"] is False
    assert "could not be deleted" in (result.error or "")
    assert "harbor.robot.delete" in result.extras["remediation"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_harbor_answer_without_a_secret_deletes_the_new_robot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(
            return_value=respx.MockResponse(201, json={"id": 42, "name": "robot$myproject+ci-push"})
        )
        _robot_read(mock)
        delete_route = mock.delete(f"{_ROBOTS_URL}/42").mock(return_value=respx.MockResponse(200))
        result = await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))
    assert delete_route.called
    assert isinstance(result, OperationResult)
    assert result.extras["error_code"] == ROBOT_SECRET_NOT_STORED
    assert result.extras["robot_deleted"] is True
    assert fake.secrets.kv.v2.patch_calls == []
    await connector.aclose()


@pytest.mark.asyncio
async def test_harbor_answer_without_an_id_says_the_robot_was_not_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(
            return_value=respx.MockResponse(201, json={"secret": _NEW_SECRET})
        )
        result = await connector.robot_create(_operator(), _TARGET, dict(_PARAMS))
    assert isinstance(result, OperationResult)
    assert result.extras["robot_id"] is None
    assert result.extras["robot_deleted"] is False
    assert _NEW_SECRET not in result.model_dump_json()
    await connector.aclose()


@pytest.mark.asyncio
async def test_without_vault_target_the_secret_is_returned_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch)
    connector = HarborConnector()
    params = {k: v for k, v in _PARAMS.items() if k != "vault_target"}
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_ROBOTS_URL).mock(return_value=respx.MockResponse(201, json=_HARBOR_ANSWER))
        result = await connector.robot_create(_operator(), _TARGET, params)
    assert result == {"id": 42, "name": "robot$myproject+ci-push", "secret": _NEW_SECRET}
    assert fake.secrets.kv.v2.patch_calls == []
    assert fake.secrets.kv.v2.put_calls == []
    await connector.aclose()


# ---------------------------------------------------------------------------
# A bad target stops the call before a robot exists
# ---------------------------------------------------------------------------

_BAD_TARGETS = [
    pytest.param("secret/ci#secret", id="no-kind"),
    pytest.param("vault:ci/harbor", id="no-field"),
    pytest.param("vault:#secret", id="no-path"),
    pytest.param("vault:ci/harbor#", id="empty-field"),
    pytest.param("vault:ci/../other#secret", id="dotdot"),
    pytest.param("vault:/ci#secret", id="leading-slash"),
    pytest.param("vault:ci//x#secret", id="double-slash"),
    pytest.param("vault:secret/ci#secret", id="mount-in-path"),
    pytest.param("linux:host:/etc/x#secret", id="other-kind"),
    pytest.param(f"vault:tenants/{uuid4()}/ci#secret", id="other-tenant"),
    pytest.param("vault:ci/harbor#secret", id="outside-tenant-scope"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _BAD_TARGETS)
async def test_bad_vault_target_makes_no_harbor_call(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    fake = _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_ROBOTS_URL).mock(
            return_value=respx.MockResponse(201, json=_HARBOR_ANSWER)
        )
        with pytest.raises(RobotVaultTargetError):
            await connector.robot_create(_operator(), _TARGET, {**_PARAMS, "vault_target": target})
    assert not route.called
    assert fake.secrets.kv.v2.patch_calls == []
    await connector.aclose()


@pytest.mark.parametrize(
    "target",
    [
        p
        for p in _BAD_TARGETS
        if p.id
        in {
            "no-kind",
            "no-field",
            "no-path",
            "empty-field",
            "dotdot",
            "leading-slash",
            "double-slash",
            "mount-in-path",
            "other-kind",
        }
    ],
)
def test_params_check_refuses_a_bad_form_before_the_call_can_park(target: str) -> None:
    problem = check_robot_vault_target({**_PARAMS, "vault_target": target})
    assert problem is not None
    assert problem["path"] == "vault_target"
    assert problem["validator"] == "params_check"


def test_params_check_accepts_a_good_form_and_a_missing_target() -> None:
    assert check_robot_vault_target(dict(_PARAMS)) is None
    assert check_robot_vault_target({"name": "x", "project": "p", "duration": 1}) is None


def test_secret_destination_without_a_target_says_the_answer_has_it() -> None:
    dest = secret_destination({"name": "x", "project": "p", "duration": 1})
    assert dest["kind"] == "answer"
    assert "returned in the answer" in dest["note"]


def test_parse_returns_none_without_a_target() -> None:
    assert parse_vault_target(_operator(), {"name": "x"}) is None


# ---------------------------------------------------------------------------
# Park-time check: a write Vault would deny is refused before it parks
# ---------------------------------------------------------------------------


def _ctx(params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=None,
        params=params,
    )


@pytest.mark.asyncio
async def test_preflight_says_denied_when_vault_would_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch)
    fake.sys.default_capabilities = ["read"]
    result = await robot_vault_preflight(_ctx(dict(_PARAMS)))
    assert result is not None
    assert result["will_be_denied"] is True
    assert result["path"] == f"secret/data/{_DEST_PATH}"


@pytest.mark.asyncio
async def test_preflight_passes_when_vault_grants_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch)
    fake.sys.default_capabilities = ["create", "read", "update"]
    result = await robot_vault_preflight(_ctx(dict(_PARAMS)))
    assert result is not None
    assert result["will_be_denied"] is False


@pytest.mark.asyncio
async def test_preflight_counts_a_path_outside_the_tenant_as_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_vault(monkeypatch)
    result = await robot_vault_preflight(
        _ctx({**_PARAMS, "vault_target": "vault:elsewhere/x#secret"})
    )
    assert result is not None
    assert result["will_be_denied"] is True
    assert fake.sys.get_capabilities_calls == []


@pytest.mark.asyncio
async def test_preflight_does_nothing_without_a_target() -> None:
    assert await robot_vault_preflight(_ctx({"name": "x"})) is None


@pytest.mark.asyncio
async def test_denied_preflight_refuses_the_park(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatcher refuses a park whose preflight says ``will_be_denied``."""
    from meho_backplane.operations.dispatcher import _preflight_denied_refusal

    fake = _install_vault(monkeypatch)
    fake.sys.default_capabilities = []
    preflight = await robot_vault_preflight(_ctx(dict(_PARAMS)))
    refusal = _preflight_denied_refusal(
        op_id="harbor.robot.create",
        proposed_effect={"permission_preflight": preflight},
        duration_ms=1.0,
    )
    assert refusal is not None
    assert refusal.status == "denied"
    assert "write_capability_denied" in (refusal.error or "")


@pytest.mark.asyncio
async def test_refused_vault_login_refuses_the_park(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Vault login that is refused counts as denied, so no approval request is made."""
    from meho_backplane.operations.dispatcher import _preflight_denied_refusal

    _install_vault(monkeypatch, login_exc=hvac.exceptions.Forbidden("role denied"))
    with capture_logs() as logs:
        preflight = await robot_vault_preflight(_ctx(dict(_PARAMS)))
    assert [e["event"] for e in logs] == ["vault_capability_preflight_failed"]
    assert preflight is not None
    assert preflight["will_be_denied"] is True
    assert preflight["reason"] == "probe_failed:VaultRoleDeniedError"
    refusal = _preflight_denied_refusal(
        op_id="harbor.robot.create",
        proposed_effect={"permission_preflight": preflight},
        duration_ms=1.0,
    )
    assert refusal is not None
    assert refusal.status == "denied"
    assert "write_capability_denied" in (refusal.error or "")


def test_both_hooks_are_registered_for_the_op() -> None:
    from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS
    from meho_backplane.operations._validate import params_check_errors

    assert _PERMISSION_PREFLIGHTS["harbor.robot.create"] is robot_vault_preflight
    assert params_check_errors("harbor.robot.create", {"vault_target": "nope"})
