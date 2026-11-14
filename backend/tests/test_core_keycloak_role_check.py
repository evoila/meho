# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The check before an upgrade that ``meho-admin`` has ``manage-realm`` (#6336).

A clear "no" stops the upgrade with a message that names the role and the
upgrade guide. When the check cannot tell, it says so and lets the upgrade
go on. No output holds the secret. The check runs against a fake Keycloak
here; ``tests/integration/test_core_bootstrap_keycloak.py`` runs it against
a real one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from meho_backplane.core import bootstrap
from meho_backplane.core.keycloak import ADMIN_SECRET_ENV, PLAN_ENV
from meho_backplane.core.keycloak_api import RealmStageError, Reply
from meho_backplane.core.keycloak_role_check import MISSING_ROLE, check, run

_SECRET = "made-up-admin-secret-1234"
_CLIENTS = "/admin/realms/meho/clients"
_ACCOUNT = f"{_CLIENTS}/admin-uuid/service-account-user"
_ROLES = "/admin/realms/meho/users/account-uuid/role-mappings/clients/rm-uuid/composite"


class FakeAdmin:
    """Answers the four reads of the check; a test changes one answer."""

    def __init__(self, roles: list[str]) -> None:
        self.ready: bool | Exception = True
        self.login_ok: bool | Exception = True
        self.secrets_seen: list[str] = []
        self.replies: dict[tuple[str, str], Reply | Exception] = {
            (_CLIENTS, "meho-admin"): Reply(200, [{"id": "admin-uuid", "clientId": "meho-admin"}]),
            (_CLIENTS, "realm-management"): Reply(
                200, [{"id": "rm-uuid", "clientId": "realm-management"}]
            ),
            (_ACCOUNT, ""): Reply(200, {"id": "account-uuid", "username": "x"}),
            (_ROLES, ""): Reply(200, [{"id": f"r-{n}", "name": n} for n in roles]),
        }

    async def realm_ready(self) -> bool:
        if isinstance(self.ready, Exception):
            raise self.ready
        return self.ready

    async def login(self, realm: str, client_id: str, client_secret: str) -> bool:
        assert (realm, client_id) == ("meho", "meho-admin")
        self.secrets_seen.append(client_secret)
        if isinstance(self.login_ok, Exception):
            raise self.login_ok
        return self.login_ok

    async def check_login(self, realm: str, client_id: str, client_secret: str) -> bool:
        raise AssertionError("the check only logs in once")

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Reply:
        assert method == "GET", "the check changes nothing"
        reply = self.replies[(path, (params or {}).get("clientId", ""))]
        if isinstance(reply, Exception):
            raise reply
        return reply


_ENV = {ADMIN_SECRET_ENV: _SECRET}


async def test_role_present_passes(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin(["manage-clients", "manage-realm", "manage-users"])
    await check(admin, _ENV)
    out = capfd.readouterr().out
    assert "role check: meho-admin has manage-realm" in out
    assert admin.secrets_seen == [_SECRET]
    assert _SECRET not in out


async def test_role_through_a_composite_passes(capfd: pytest.CaptureFixture[str]) -> None:
    # The composite endpoint lists realm-admin's parts, manage-realm too.
    await check(FakeAdmin(["realm-admin", "manage-realm", "view-realm"]), _ENV)
    assert "has manage-realm" in capfd.readouterr().out


@pytest.mark.parametrize(
    "roles", [["manage-clients", "manage-users"], [], ["view-realm"], ["MANAGE-REALM"]]
)
async def test_role_missing_stops_with_the_role_and_the_guide(
    roles: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(RealmStageError) as raised:
        await check(FakeAdmin(roles), _ENV)
    message = str(raised.value)
    assert message == MISSING_ROLE
    assert "manage-realm" in message
    assert "The bootstrap Job needs manage-realm" in message
    assert "changes nothing" in message
    assert _SECRET not in message + capfd.readouterr().out


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda a: setattr(a, "ready", False), "does not serve realm meho"),
        (
            lambda a: setattr(a, "ready", RealmStageError("Keycloak request failed: ConnectError")),
            "ConnectError",
        ),
        (lambda a: setattr(a, "login_ok", False), "refused the login of meho-admin"),
        (
            lambda a: setattr(a, "login_ok", RealmStageError("token request failed: HTTP 503")),
            "HTTP 503",
        ),
        (
            lambda a: a.replies.__setitem__((_CLIENTS, "meho-admin"), Reply(403, None)),
            "cannot look up client meho-admin: HTTP 403",
        ),
        (
            lambda a: a.replies.__setitem__((_CLIENTS, "meho-admin"), Reply(200, [])),
            "cannot look up client meho-admin: not found",
        ),
        (
            # A fuzzy match on another client is not meho-admin.
            lambda a: a.replies.__setitem__(
                (_CLIENTS, "meho-admin"), Reply(200, [{"id": "x", "clientId": "meho-admin-2"}])
            ),
            "not found",
        ),
        (
            lambda a: a.replies.__setitem__((_ACCOUNT, ""), Reply(404, {"error": "x"})),
            "service account of meho-admin: HTTP 404",
        ),
        (
            lambda a: a.replies.__setitem__((_ACCOUNT, ""), Reply(200, {})),
            "service account of meho-admin: no id",
        ),
        (
            lambda a: a.replies.__setitem__((_CLIENTS, "realm-management"), Reply(500, None)),
            "cannot look up client realm-management: HTTP 500",
        ),
        (
            lambda a: a.replies.__setitem__((_ROLES, ""), Reply(403, None)),
            "cannot read the roles of meho-admin: HTTP 403",
        ),
        (
            lambda a: a.replies.__setitem__((_ROLES, ""), Reply(200, {"name": "manage-realm"})),
            "not a list",
        ),
        (
            lambda a: a.replies.__setitem__(
                (_ROLES, ""), RealmStageError("Keycloak request failed: ReadTimeout")
            ),
            "ReadTimeout",
        ),
    ],
)
async def test_cannot_tell_lets_the_upgrade_go_on(
    change: Any, reason: str, capfd: pytest.CaptureFixture[str]
) -> None:
    admin = FakeAdmin(["manage-realm"])
    change(admin)
    await check(admin, _ENV)
    out = capfd.readouterr().out
    assert out.startswith("role check: unknown (")
    assert reason in out
    assert "the bootstrap Job checks again" in out
    assert "has manage-realm" not in out
    assert _SECRET not in out


async def test_no_secret_lets_the_upgrade_go_on_without_a_login(
    capfd: pytest.CaptureFixture[str],
) -> None:
    admin = FakeAdmin(["manage-realm"])
    await check(admin, {})
    assert admin.secrets_seen == []
    assert "role check: unknown (no secret for meho-admin yet)" in capfd.readouterr().out


async def test_run_without_keycloak_lets_the_upgrade_go_on(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The first upgrade with the built-in Keycloak: its Service is not there yet."""
    plan = (
        '{"keycloak_url":"http://localhost:9/auth","realm_file":"/x.json",'
        '"first_login_secret":"rel-core-first-login","admin_username":"admin",'
        '"approver_username":"approver","console_admin_secret":"rel-core-console-admin",'
        '"console_admin_username":"console-admin"}'
    )
    await run({PLAN_ENV: plan, ADMIN_SECRET_ENV: _SECRET})
    out = capfd.readouterr().out
    assert "role check: unknown (Keycloak does not serve realm meho)" in out
    assert _SECRET not in out


def test_bootstrap_entry_point_exits_1_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    async def missing() -> None:
        raise RealmStageError(MISSING_ROLE)

    monkeypatch.setitem(bootstrap.STAGES, "keycloak-role-check", missing)  # type: ignore[arg-type]
    assert bootstrap.main(["keycloak-role-check"]) == 1
    err = capfd.readouterr().err
    assert err.startswith("core_bootstrap_failed: meho-admin lacks the realm-management role")


def test_the_stage_is_registered() -> None:
    from meho_backplane.core import keycloak_role_check

    assert bootstrap.STAGES["keycloak-role-check"] is keycloak_role_check.run
