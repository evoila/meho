# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho-host reset-second-factor`` in the core pod (#4746).

The module runs against saved Keycloak Admin REST answers (a fake
:class:`~meho_backplane.core.keycloak_api.Admin`) and the test database.
Cases: a user with a one-time code and recovery codes, a user without them,
an unknown user, a failure part-way, a write whose reply was lost, a user in
two tenants, a database that cannot be reached, and an audit row that cannot
be written. No output holds the client secret.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.audit_query import query_audit
from meho_backplane.audit_query.schemas import AuditQueryFilters
from meho_backplane.core import reset_second_factor as rsf
from meho_backplane.core.keycloak_api import RealmStageError, Reply
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.settings import get_settings

_SECRET = "made-up-admin-secret-5678"
_TENANT = uuid.UUID("11111111-2222-4333-8444-555555555555")
_USERS = "/admin/realms/meho/users"
_ID = "0b6c2f6e-1d7a-4c39-9f4e-2a1b3c4d5e6f"
_USER = f"{_USERS}/{_ID}"
_CREDS = f"{_USER}/credentials"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var the settings need, so the test database opens."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _user(required: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": _ID,
        "username": "admin",
        "enabled": True,
        "attributes": {"note": ["kept"]},
        "requiredActions": required or [],
    }


class FakeAdmin:
    """Answers from saved Keycloak replies and records each call."""

    def __init__(self, credentials: list[dict[str, Any]], required: list[str] | None = None):
        self.login_ok = True
        self.calls: list[tuple[str, str, Any]] = []
        self.secrets_seen: list[str] = []
        self.replies: dict[tuple[str, str], Reply | Exception] = {
            ("GET", _USERS): Reply(200, [{"id": _ID, "username": "admin"}]),
            ("GET", _USER): Reply(200, _user(required)),
            ("GET", f"{_USER}/groups"): Reply(
                200,
                [
                    {
                        "name": "MEHO-Admins",
                        "attributes": {"meho_tenant": [f"{_TENANT}:acme:Admins"]},
                    }
                ],
            ),
            ("GET", _CREDS): Reply(200, credentials),
            ("PUT", _USER): Reply(204, None),
        }
        for credential in credentials:
            self.replies[("DELETE", f"{_CREDS}/{credential['id']}")] = Reply(204, None)

    async def realm_ready(self) -> bool:
        return True

    async def login(self, realm: str, client_id: str, client_secret: str) -> bool:
        assert (realm, client_id) == ("meho", "meho-admin")
        self.secrets_seen.append(client_secret)
        return self.login_ok

    async def check_login(self, realm: str, client_id: str, client_secret: str) -> bool:
        raise AssertionError("not used")

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Reply:
        self.calls.append((method, path, json if json is not None else dict(params or {})))
        reply = self.replies[(method, path)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


_OTP = {"id": "cred-otp", "type": "otp", "userLabel": "phone"}
_CODES = {"id": "cred-codes", "type": "recovery-authn-codes"}
_PASSWORD = {"id": "cred-password", "type": "password"}


def _args(user: str = "admin") -> argparse.Namespace:
    return argparse.Namespace(user=user, by="alice", host="meho-node-1")


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(AuditLog))).scalars().all())


async def _run(admin: FakeAdmin, user: str = "admin") -> int:
    return await rsf._run(admin, _args(user), _SECRET, "meho-admin")


async def test_user_with_code_and_recovery_codes(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_PASSWORD, _OTP, _CODES], required=["VERIFY_EMAIL"])

    assert await _run(admin) == rsf.EXIT_DONE

    assert admin.writes() == [
        ("DELETE", f"{_CREDS}/cred-otp"),
        ("DELETE", f"{_CREDS}/cred-codes"),
        ("PUT", _USER),
    ]
    put_body = admin.calls[-1][2]
    assert put_body["requiredActions"] == ["VERIFY_EMAIL", "UPDATE_PASSWORD", "CONFIGURE_TOTP"]
    assert put_body["attributes"] == {"note": ["kept"]}
    assert admin.calls[0][2] == {"username": "admin", "exact": "true"}
    out = capfd.readouterr()
    assert "[ok]   removed the one-time code of admin" in out.out
    assert "[ok]   removed the recovery codes of admin" in out.out
    assert "asks for a new password and a new one-time code app" in out.out
    assert "alice on meho-node-1 reset the second factor of admin" in out.out
    assert _SECRET not in out.out + out.err
    assert admin.secrets_seen == [_SECRET]

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_class == AuditEventClass.OPERATION_EXECUTION
    assert row.operator_sub == rsf.OPERATOR_SUB
    assert row.path == rsf.AUDIT_PATH
    assert row.status_code == 200
    assert row.tenant_id == _TENANT
    assert row.occurred_at is not None
    assert row.payload["linux_user"] == "alice"
    assert row.payload["host"] == "meho-node-1"
    assert row.payload["keycloak_user"] == "admin"
    assert row.payload["keycloak_user_id"] == _ID
    assert row.payload["removed_credentials"] == ["otp", "recovery-authn-codes"]
    assert row.payload["required_actions_set"] == ["UPDATE_PASSWORD", "CONFIGURE_TOTP"]
    assert row.payload["maybe_changed"] == []
    assert row.payload["result"] == "ok"
    assert _SECRET not in str(row.payload)


async def test_user_without_a_second_factor(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_PASSWORD], required=["CONFIGURE_TOTP"])

    assert await _run(admin) == rsf.EXIT_DONE

    assert admin.writes() == [("PUT", _USER)]
    assert admin.calls[-1][2]["requiredActions"] == ["CONFIGURE_TOTP", "UPDATE_PASSWORD"]
    assert "[note] admin had no one-time code and no recovery codes." in capfd.readouterr().out
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["removed_credentials"] == []


async def test_user_name_is_matched_without_case() -> None:
    admin = FakeAdmin([_OTP])

    assert await _run(admin, user="Admin") == rsf.EXIT_DONE
    assert ("DELETE", f"{_CREDS}/cred-otp") in admin.writes()


async def test_unknown_user_changes_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_OTP])
    # Keycloak's exact search can still return a user whose name only starts the same.
    admin.replies[("GET", _USERS)] = Reply(200, [{"id": "other", "username": "nobody2"}])

    assert await _run(admin, user="nobody") == rsf.EXIT_FAILED

    assert admin.writes() == []
    err = capfd.readouterr().err
    assert "[FAIL] there is no user nobody in realm meho. Nothing was changed." in err
    assert await _audit_rows() == []


async def test_refused_login_changes_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_OTP])
    admin.login_ok = False

    assert await _run(admin) == rsf.EXIT_FAILED

    assert admin.calls == []
    err = capfd.readouterr().err
    assert "Keycloak refused the login of meho-admin. Nothing was changed." in err
    assert _SECRET not in err
    assert await _audit_rows() == []


async def test_failed_read_changes_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_OTP])
    admin.replies[("GET", _CREDS)] = Reply(500, None)

    assert await _run(admin) == rsf.EXIT_FAILED

    assert admin.writes() == []
    err = capfd.readouterr().err
    assert "cannot read the credentials of admin: Keycloak answered HTTP 500" in err
    assert "Nothing was changed." in err
    assert await _audit_rows() == []


async def test_database_down_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    async def down() -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(rsf, "_check_database", down)
    admin = FakeAdmin([_OTP])

    assert await _run(admin) == rsf.EXIT_FAILED

    assert admin.writes() == []
    assert "cannot reach the MEHO database" in capfd.readouterr().err


async def test_failure_part_way_is_audited(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_OTP, _CODES])
    admin.replies[("PUT", _USER)] = RealmStageError("Keycloak request failed: ReadTimeout")

    assert await _run(admin) == rsf.EXIT_FAILED

    out = capfd.readouterr()
    assert "[ok]   removed the one-time code of admin" in out.out
    assert "wrote audit row" in out.out
    assert "cannot set what the next login asks for" in out.err
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].status_code == 500
    assert rows[0].payload["result"] == "error"
    assert rows[0].payload["removed_credentials"] == ["otp", "recovery-authn-codes"]
    assert rows[0].payload["required_actions_set"] == []


async def test_no_success_without_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    async def broken(*_: Any, **__: Any) -> uuid.UUID:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rsf, "write_audit_rows", broken)
    admin = FakeAdmin([_OTP])

    assert await _run(admin) == rsf.EXIT_FAILED

    out = capfd.readouterr()
    assert "wrote audit row" not in out.out
    assert "the audit row could not be written (RuntimeError)" in out.err


async def test_lost_reply_of_the_first_delete_is_audited(
    capfd: pytest.CaptureFixture[str],
) -> None:
    admin = FakeAdmin([_OTP, _CODES])
    admin.replies[("DELETE", f"{_CREDS}/cred-otp")] = RealmStageError(
        "Keycloak request failed: ReadTimeout"
    )

    assert await _run(admin) == rsf.EXIT_FAILED

    assert admin.writes() == [("DELETE", f"{_CREDS}/cred-otp")]
    out = capfd.readouterr()
    assert "Nothing was changed" not in out.out + out.err
    assert "the one-time code of admin, so it may be removed" in out.out
    assert "wrote audit row" in out.out
    assert "what may have changed" in out.err
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].status_code == 500
    assert rows[0].payload["removed_credentials"] == []
    assert rows[0].payload["maybe_changed"] == ["otp"]
    assert "ReadTimeout" in rows[0].payload["error"]


async def test_lost_reply_of_the_put_is_audited(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_PASSWORD])
    admin.replies[("PUT", _USER)] = RealmStageError("Keycloak request failed: ReadTimeout")

    assert await _run(admin) == rsf.EXIT_FAILED

    out = capfd.readouterr()
    assert "Nothing was changed" not in out.out + out.err
    assert "what the next login of admin asks for, so it may be changed" in out.out
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["required_actions_set"] == []
    assert rows[0].payload["maybe_changed"] == ["required_actions"]


async def test_refused_first_delete_changes_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    admin = FakeAdmin([_OTP])
    admin.replies[("DELETE", f"{_CREDS}/cred-otp")] = Reply(403, None)

    assert await _run(admin) == rsf.EXIT_FAILED

    assert "Keycloak answered HTTP 403. Nothing was changed." in capfd.readouterr().err
    assert await _audit_rows() == []


async def test_user_in_two_tenants_is_found_by_the_audit_query_of_each() -> None:
    admin = FakeAdmin([_OTP])
    other = uuid.UUID("99999999-2222-4333-8444-555555555555")
    admin.replies[("GET", f"{_USER}/groups")] = Reply(
        200,
        [
            {"attributes": {"meho_tenant": [f"{_TENANT}:acme:Admins"]}},
            {"attributes": {"meho_tenant": [f"{other}:beta:Readers", "not-a-uuid:x:Readers"]}},
        ],
    )

    assert await _run(admin) == rsf.EXIT_DONE

    filters = AuditQueryFilters(op_id=rsf.AUDIT_PATH)
    async with get_sessionmaker()() as session:
        for tenant in (_TENANT, other):
            found = await query_audit(filters, tenant_id=tenant, session=session)
            assert len(found.rows) == 1
            assert found.rows[0].payload["keycloak_user"] == "admin"
            assert found.rows[0].op_id == rsf.AUDIT_PATH
        unrelated = uuid.UUID("77777777-2222-4333-8444-555555555555")
        assert (await query_audit(filters, tenant_id=unrelated, session=session)).rows == []
    assert len(await _audit_rows()) == 2


def test_main_without_admin_login(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    class NoAdmin:
        keycloak_admin_url = ""
        keycloak_admin_client_id = ""
        keycloak_admin_client_secret = ""

    monkeypatch.setattr(rsf, "get_settings", NoAdmin)

    assert rsf.main(["--by", "alice", "--host", "h", "admin"]) == rsf.EXIT_FAILED
    assert "has no Keycloak admin login for realm meho" in capfd.readouterr().err


def test_main_wrong_command_line() -> None:
    assert rsf.main(["admin"]) == 2
    assert rsf.main(["--by", "alice", "--host", "h", " "]) == 2
