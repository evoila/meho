# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Keycloak operation checks which realm Keycloak answers with.

* Each operation first reads the target's managed realm and compares the
  ``realm`` field of the answer with the managed realm of the target.
* A different name, a different letter case, a space at the end, a missing
  name or an answer that is not an object gives an error. Nothing else is
  sent to Keycloak: no read, no write.
* The error names the target and says what to check. It holds no value from
  the answer.
* The same name lets the operation run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_authflow import APPROVAL_FLOW_OPS
from meho_backplane.connectors.keycloak.ops_authflow_passkey_schemas import PASSKEY_CODE_SKIP_OPS
from meho_backplane.connectors.keycloak.ops_authflow_touch_schemas import TOUCH_OPS
from meho_backplane.connectors.keycloak.ops_events import EVENT_OPS
from meho_backplane.connectors.keycloak.ops_flow_reads import FLOW_READ_OPS
from meho_backplane.connectors.keycloak.ops_identity_provider import IDENTITY_PROVIDER_OPS
from meho_backplane.connectors.keycloak.ops_lock_status import LOCK_STATUS_OPS
from meho_backplane.connectors.keycloak.ops_login_check import LOGIN_CHECK_OPS
from meho_backplane.connectors.keycloak.ops_read import READ_OPS
from meho_backplane.connectors.keycloak.ops_user_required_actions import USER_REQUIRED_ACTION_OPS
from meho_backplane.connectors.keycloak.ops_user_state import USER_STATE_OPS
from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS
from meho_backplane.connectors.keycloak.realm_answer import KeycloakRealmAnswerError

pytestmark = pytest.mark.usefixtures("real_keycloak_realm_check")

_CHECK_LOGIN = "keycloak.user.check_login"
_ALL_OPS = (
    *READ_OPS,
    *WRITE_OPS,
    *LOGIN_CHECK_OPS,
    *APPROVAL_FLOW_OPS,
    *PASSKEY_CODE_SKIP_OPS,
    *TOUCH_OPS,
    *IDENTITY_PROVIDER_OPS,
    *USER_REQUIRED_ACTION_OPS,
    *USER_STATE_OPS,
    *LOCK_STATUS_OPS,
    *FLOW_READ_OPS,
    *EVENT_OPS,
)
_CHECKED_OPS = [op for op in _ALL_OPS if op.op_id != _CHECK_LOGIN]
_MANAGED = "example"
_ANSWER_MARKER = "answer-realm-marker"


@dataclass
class _Target:
    name: str = "keycloak-a"
    id: UUID = field(default_factory=uuid4)
    extras: dict[str, Any] = field(default_factory=lambda: {"managed_realm": _MANAGED})


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="operator.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


class _Recorder:
    """Records every request the connector would send to Keycloak."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.reads: list[str] = []
        self.sent: list[str] = []

    def install(self, connector: KeycloakConnector, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _get_json(_target: Any, path: str, **_kw: Any) -> Any:
            self.reads.append(path)
            return self.answer

        async def _other(*args: Any, **_kw: Any) -> Any:
            self.sent.append(str(args))
            raise AssertionError("a request was sent after the realm check refused")

        monkeypatch.setattr(connector, "_get_admin_json", _get_json)
        monkeypatch.setattr(connector, "_get_admin_list", _other)
        monkeypatch.setattr(connector, "_write_admin", _other)
        monkeypatch.setattr(connector, "_http_client", _other)


def test_every_op_but_the_login_test_is_wrapped() -> None:
    for op in _ALL_OPS:
        handler = getattr(KeycloakConnector, op.handler_attr)
        assert hasattr(handler, "__wrapped__") is (op.op_id != _CHECK_LOGIN), op.op_id


@pytest.mark.parametrize("op", _CHECKED_OPS, ids=[op.op_id for op in _CHECKED_OPS])
@pytest.mark.parametrize(
    "answer",
    [
        {"realm": _ANSWER_MARKER},
        {"realm": "Example"},
        {"realm": "example "},
        {"enabled": True},
        {"realm": None},
        ["example"],
    ],
    ids=["other-name", "other-case", "trailing-space", "no-name", "null-name", "not-object"],
)
async def test_mismatch_refuses_and_sends_nothing_more(
    op: Any, answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = KeycloakConnector()
    recorder = _Recorder(answer)
    recorder.install(connector, monkeypatch)
    handler = getattr(connector, op.handler_attr)
    with pytest.raises(KeycloakRealmAnswerError) as caught:
        await handler(_operator(), _Target(), {})
    text = str(caught.value)
    assert "keycloak-a" in text
    assert "managed_realm" in text
    assert _ANSWER_MARKER not in text
    assert recorder.reads == [f"/admin/realms/{_MANAGED}"]
    assert recorder.sent == []


async def test_same_name_lets_the_operation_run(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KeycloakConnector()
    recorder = _Recorder({"realm": _MANAGED, "enabled": True})
    recorder.install(connector, monkeypatch)
    result = await connector.realm_get(_operator(), _Target(), {})
    assert recorder.reads[0] == f"/admin/realms/{_MANAGED}"
    assert result is not None
