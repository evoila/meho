# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Salt changes: accept, reject and delete minion keys; run a state or a command; stop a job.

Drives the six changes through the real dispatch stack against a
respx-mocked RaaS server. No live system and no Vault: the credential
loader is a stub and the connector instance is put into the dispatcher's
cache. All names and ids are made up.

What is proven:

* each op has the risk level of the task table, and every change needs
  approval;
* each change waits for approval when a person calls it, runs with the
  right RPC call after approval, and passes a vendor error on to the answer;
* the destructive changes (delete keys, run a state, run a command) are
  refused without a matching ``preview_hash`` and for an agent; their
  approval card names every key or minion, and refuses a missing key or a
  target that matches nothing; the full flow (preview, wait, a second
  person approves, run) works;
* the approved run stops with ``precondition_failed`` when the keys or the
  matched minions differ from the card, and a run reaches only the minions
  on the card (a ``list`` target);
* a job that already runs on the same minions is not started again;
* a command that looks like it holds a secret is refused;
* the flight recorder stores no vendor body of a run, and only the job
  state fields and the stop's job id of a job stop;
* the status read ``salt.job.result`` gives the job state, says
  ``not reachable`` when the server does not answer, and logs in again
  after a ``401``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.salt import (
    SALT_CONNECTOR_ID,
    SALT_IMPL_ID,
    SALT_PRODUCT,
    SALT_TYPED_OPS,
    SALT_VERSION,
    SaltConnector,
    register_salt_typed_operations,
)
from meho_backplane.connectors.salt.job_writes import command_secret_reason, minions_sha256
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    reset_handler_cache,
)
from meho_backplane.operations._preview import approved_preview_var
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004518")
_TARGET_NAME = "salt-writes"
_HOST = "salt-writes.test.invalid"
_BASE_URL = f"https://{_HOST}"
_LOGIN = "/account/login"
_RPC = "/rpc"
_PASSWORD = "salt-writes-password-value"
_JID = "20260101000000000042"
_STOP_JID = "20260101000000000043"
_SAVED_TARGET = "11111111-2222-3333-4444-555555555555"

#: The task table: op id -> (risk level, needs approval).
_RISK_TABLE: dict[str, tuple[str, bool]] = {
    "salt.key.accept": ("caution", True),
    "salt.key.reject": ("dangerous", True),
    "salt.key.delete": ("destructive", True),
    "salt.job.run_state": ("destructive", True),
    "salt.job.run_command": ("destructive", True),
    "salt.job.cancel": ("dangerous", True),
}

_Answers = dict[tuple[str, str], Any]


def _operator(sub: str = "salt-requester", kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Salt Writes Operator",
        email=None,
        raw_jwt="<salt-writes-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<salt-user>", "password": _PASSWORD}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=SALT_PRODUCT, version=SALT_VERSION, impl_id=SALT_IMPL_ID, cls=SaltConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
async def salt() -> AsyncIterator[SaltConnector]:
    set_default_reducer(PassThroughReducer())
    await register_salt_typed_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product=SALT_PRODUCT,
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="salt/raas",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "8.18.0"},
                notes="seeded by test_connectors_salt_writes",
            )
        )
        await session.commit()
    connector = SaltConnector(session_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[SaltConnector] = connector
    yield connector
    await connector.aclose()


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


def _login() -> httpx.Response:
    return httpx.Response(
        200,
        headers=[("set-cookie", "_xsrf=made-up-xsrf; Path=/")],
        json={"authenticated": True},
    )


def _fake_raas(answers: _Answers, calls: list[dict[str, Any]]) -> Callable[..., httpx.Response]:
    """Answer each (resource, method) from *answers*; a callable answer gets the kwarg."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        key = (body["resource"], body["method"])
        if key not in answers:
            error = {"message": f"no answer for {key}"}
            return httpx.Response(200, json={"riq": 1, "ret": None, "error": error})
        answer = answers[key]
        if callable(answer):
            answer = answer(body["kwarg"])
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"riq": 1, "ret": answer, "error": None, "warnings": []})

    return _handler


class _Raas:
    """A respx-mocked RaaS server; ``answers`` may change while a test runs."""

    def __init__(self, answers: _Answers) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []
        self._mock = respx.mock(base_url=_BASE_URL, assert_all_called=False)

    def __enter__(self) -> _Raas:
        mock = self._mock.__enter__()
        mock.get(_LOGIN).mock(return_value=_login())
        mock.post(_RPC).mock(side_effect=_fake_raas(self.answers, self.calls))
        return self

    def __exit__(self, *exc: object) -> None:
        self._mock.__exit__(*exc)

    def called(self, resource: str, method: str) -> list[dict[str, Any]]:
        return [
            call["kwarg"]
            for call in self.calls
            if (call["resource"], call["method"]) == (resource, method)
        ]


def _key_answer(states: dict[str, str | None]) -> Callable[[dict[str, Any]], Any]:
    """``minions.get_minion_key_state``: the key of the asked minion, from *states*."""

    def _answer(kwarg: dict[str, Any]) -> Any:
        state = states.get(kwarg["minion_id"])
        if state is None:
            return {"count": 0, "results": []}
        row = {"minion": kwarg["minion_id"], "master": kwarg["master_id"], "key_state": state}
        return {"count": 1, "results": [row]}

    return _answer


def _set_keys(states: dict[str, str | None], new_state: str | None) -> Callable[..., Any]:
    """``minions.set_minion_key_state``: change *states* the way RaaS would."""

    def _answer(kwarg: dict[str, Any]) -> Any:
        for _master, minion in kwarg["minions"]:
            states[minion] = new_state
        return {}

    return _answer


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": SALT_CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }


async def _run(
    op_id: str,
    params: dict[str, Any],
    *,
    approved: bool = True,
    card: dict[str, Any] | None = None,
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Dispatch an op; ``approved`` runs it as approved, with *card* as the approval card."""
    token = approved_preview_var.set((op_id, card)) if card is not None else None
    try:
        result = await dispatch(
            operator=operator or _operator(),
            connector_id=SALT_CONNECTOR_ID,
            op_id=op_id,
            target=await _target(),
            params=params,
            _approved=approved,
        )
    finally:
        if token is not None:
            approved_preview_var.reset(token)
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


async def _approve_and_resume(call: dict[str, Any]) -> Any:
    """A second person approves the waiting call; then MEHO runs it."""
    request_id = uuid.UUID(call["extras"]["approval_request_id"])
    requester = _operator("salt-requester")
    approver = _operator("salt-approver")
    async with get_sessionmaker()() as session:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(session, request_id, operator=requester, params=None)
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


async def _parked_effect(call: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return dict(row.proposed_effect)


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------


def test_risk_table_matches_the_task() -> None:
    """Each change has the risk level of the task table and needs approval."""
    table = {
        op.op_id: (op.safety_level, op.requires_approval)
        for op in SALT_TYPED_OPS
        if op.op_id in _RISK_TABLE
    }
    assert table == _RISK_TABLE


async def test_registered_rows_carry_the_risk_levels(salt: SaltConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_(list(_RISK_TABLE))
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id: (row.safety_level, row.requires_approval) for row in rows} == _RISK_TABLE
    assert all(row.source_kind == "typed" for row in rows)
    assert all("write" in row.tags for row in rows)


# ---------------------------------------------------------------------------
# salt.key.accept / salt.key.reject
# ---------------------------------------------------------------------------

_ONE_KEY = {"minions": [{"master_id": "<master-1>", "minion_id": "<minion-new>"}]}


async def test_accept_waits_for_approval(salt: SaltConnector) -> None:
    states: dict[str, str | None] = {"<minion-new>": "pending"}
    with _Raas({("minions", "get_minion_key_state"): _key_answer(states)}) as raas:
        call = await call_operation(_operator(), _args("salt.key.accept", _ONE_KEY))
    assert call["status"] == "awaiting_approval", call
    assert raas.called("minions", "set_minion_key_state") == []
    assert await _approval_rows() == 1


async def test_accept_sets_the_key_state_and_reads_it_back(salt: SaltConnector) -> None:
    states: dict[str, str | None] = {"<minion-new>": "pending"}
    answers: _Answers = {
        ("minions", "get_minion_key_state"): _key_answer(states),
        ("minions", "set_minion_key_state"): _set_keys(states, "accepted"),
    }
    with _Raas(answers) as raas:
        result = await _run("salt.key.accept", {**_ONE_KEY, "include_rejected": True})
    assert result["status"] == "ok", result
    assert raas.called("minions", "set_minion_key_state") == [
        {
            "state": "accept",
            "minions": [["<master-1>", "<minion-new>"]],
            "include_rejected": True,
        }
    ]
    assert result["result"] == {
        "status": "requested",
        "action": "accept",
        "reason": None,
        "changed_count": 1,
        "keys": [
            {
                "master_id": "<master-1>",
                "minion_id": "<minion-new>",
                "key_state_before": "pending",
                "key_state_after": "accepted",
            }
        ],
    }
    assert _PASSWORD not in json.dumps(result)


async def test_reject_sends_include_accepted(salt: SaltConnector) -> None:
    states: dict[str, str | None] = {"<minion-new>": "accepted"}
    answers: _Answers = {
        ("minions", "get_minion_key_state"): _key_answer(states),
        ("minions", "set_minion_key_state"): _set_keys(states, "rejected"),
    }
    with _Raas(answers) as raas:
        call = await call_operation(_operator(), _args("salt.key.reject", _ONE_KEY))
        assert call["status"] == "awaiting_approval", call
        result = await _run("salt.key.reject", {**_ONE_KEY, "include_accepted": True})
    assert result["status"] == "ok", result
    assert raas.called("minions", "set_minion_key_state") == [
        {"state": "reject", "minions": [["<master-1>", "<minion-new>"]], "include_accepted": True}
    ]
    assert result["result"]["keys"][0]["key_state_after"] == "rejected"


async def test_key_change_refuses_a_missing_key(salt: SaltConnector) -> None:
    with _Raas({("minions", "get_minion_key_state"): _key_answer({})}) as raas:
        result = await _run("salt.key.accept", _ONE_KEY)
    assert result["status"] == "error", result
    assert "no key for minion <minion-new> on master <master-1>" in json.dumps(result)
    assert raas.called("minions", "set_minion_key_state") == []


@pytest.mark.parametrize("minion_id", ["web*", "a,b", "web[12]", "two words", "web-1\n"])
async def test_key_change_refuses_a_glob_or_list_id(salt: SaltConnector, minion_id: str) -> None:
    params = {"minions": [{"master_id": "<master-1>", "minion_id": minion_id}]}
    with _Raas({}) as raas:
        result = await _run("salt.key.accept", params)
    assert result["status"] != "ok", result
    assert result["extras"]["error_code"] == "invalid_params"
    assert raas.calls == []


async def test_key_change_vendor_error_reaches_the_answer(salt: SaltConnector) -> None:
    error = {"message": "Permission denied", "detail": "minions.set_minion_key_state"}
    answers: _Answers = {
        ("minions", "get_minion_key_state"): _key_answer({"<minion-new>": "pending"}),
        ("minions", "set_minion_key_state"): httpx.Response(
            200, json={"riq": 1, "ret": None, "error": error}
        ),
    }
    with _Raas(answers):
        result = await _run("salt.key.reject", _ONE_KEY)
    assert result["status"] == "error", result
    assert "Permission denied" in json.dumps(result)


# ---------------------------------------------------------------------------
# salt.key.delete
# ---------------------------------------------------------------------------

_TWO_KEYS = {
    "minions": [
        {"master_id": "<master-1>", "minion_id": "<minion-a>"},
        {"master_id": "<master-1>", "minion_id": "<minion-b>"},
    ]
}


def _two_key_states() -> dict[str, str | None]:
    return {"<minion-a>": "accepted", "<minion-b>": "rejected"}


async def test_delete_without_preview_hash_is_refused(salt: SaltConnector) -> None:
    states = _two_key_states()
    with _Raas({("minions", "get_minion_key_state"): _key_answer(states)}) as raas:
        call = await call_operation(_operator(), _args("salt.key.delete", _TWO_KEYS))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert raas.called("minions", "set_minion_key_state") == []
    assert await _approval_rows() == 0


async def test_delete_is_refused_for_an_agent(salt: SaltConnector) -> None:
    states = _two_key_states()
    with _Raas({("minions", "get_minion_key_state"): _key_answer(states)}) as raas:
        result = await _run(
            "salt.key.delete",
            _TWO_KEYS,
            approved=False,
            operator=_operator("salt-agent", PrincipalKind.AGENT),
        )
    assert result["status"] == "denied", result
    assert raas.called("minions", "set_minion_key_state") == []
    assert await _approval_rows() == 0


async def test_delete_card_refuses_a_missing_key(salt: SaltConnector) -> None:
    states: dict[str, str | None] = {"<minion-a>": "accepted"}
    with _Raas({("minions", "get_minion_key_state"): _key_answer(states)}):
        preview = await preview_operation(_operator(), _args("salt.key.delete", _TWO_KEYS))
        call = await call_operation(
            _operator(),
            {**_args("salt.key.delete", _TWO_KEYS), "preview_hash": preview.get("preview_hash")},
        )
    assert call["status"] == "denied", call
    assert "no key for minion <minion-b>" in json.dumps(call)
    assert await _approval_rows() == 0


async def test_delete_waits_for_a_second_person_then_runs(salt: SaltConnector) -> None:
    """Preview lists each key; a second person approves; then the keys are deleted once."""
    states = _two_key_states()
    answers: _Answers = {
        ("minions", "get_minion_key_state"): _key_answer(states),
        ("minions", "set_minion_key_state"): _set_keys(states, None),
    }
    args = _args("salt.key.delete", _TWO_KEYS)
    with _Raas(answers) as raas:
        preview = await preview_operation(_operator(), args)
        assert preview["status"] == "ok", preview
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        effect = await _parked_effect(call)
        assert effect["safety_level"] == "destructive"
        block = effect["blast_radius"]
        assert block["object"]["kind"] == "minion_keys"
        assert block["object"]["count"] == 2
        assert block["children"] == [
            {
                "kind": "minion_key",
                "master_id": "<master-1>",
                "minion_id": "<minion-a>",
                "key_state": "accepted",
            },
            {
                "kind": "minion_key",
                "master_id": "<master-1>",
                "minion_id": "<minion-b>",
                "key_state": "rejected",
            },
        ]
        assert block["irreversibility"]
        assert raas.called("minions", "set_minion_key_state") == []
        resumed = await _approve_and_resume(call)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "requested"
    assert [key["key_state_after"] for key in resumed.result["keys"]] == [None, None]
    assert raas.called("minions", "set_minion_key_state") == [
        {"state": "delete", "minions": [["<master-1>", "<minion-a>"], ["<master-1>", "<minion-b>"]]}
    ]


async def test_delete_stops_when_a_key_changed_after_the_approval(salt: SaltConnector) -> None:
    states = _two_key_states()
    answers: _Answers = {
        ("minions", "get_minion_key_state"): _key_answer(states),
        ("minions", "set_minion_key_state"): _set_keys(states, None),
    }
    args = _args("salt.key.delete", _TWO_KEYS)
    with _Raas(answers) as raas:
        preview = await preview_operation(_operator(), args)
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        states["<minion-b>"] = "accepted"
        resumed = await _approve_and_resume(call)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "precondition_failed"
    assert "changed since the approver saw them" in resumed.result["reason"]
    assert raas.called("minions", "set_minion_key_state") == []


async def test_delete_without_an_approval_card_changes_nothing(salt: SaltConnector) -> None:
    states = _two_key_states()
    answers: _Answers = {("minions", "get_minion_key_state"): _key_answer(states)}
    with _Raas(answers) as raas:
        result = await _run("salt.key.delete", _TWO_KEYS)
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "precondition_failed"
    assert raas.called("minions", "set_minion_key_state") == []


# ---------------------------------------------------------------------------
# salt.job.run_state / salt.job.run_command
# ---------------------------------------------------------------------------

_MATCHED = {"<master-1>": ["<web-1>", "<web-2>"], "<master-2>": ["<web-3>"]}
_STATE_PARAMS = {"tgt": "<web-glob>", "states": ["<state-a>", "<state-b>"], "test": True}


def _run_answers(**extra: Any) -> _Answers:
    answers: _Answers = {
        ("tgt", "get_target_group_minions"): dict(_MATCHED),
        ("cmd", "get_cmds"): {"count": 0, "results": []},
        ("cmd", "route_cmd"): _JID,
    }
    answers.update({tuple(key.split(".")): value for key, value in extra.items()})
    return answers


_LIST_TARGET = {
    "<master-1>": {"tgt": "<web-1>,<web-2>", "tgt_type": "list"},
    "<master-2>": {"tgt": "<web-3>", "tgt_type": "list"},
}


async def test_run_state_without_preview_hash_is_refused(salt: SaltConnector) -> None:
    with _Raas(_run_answers()) as raas:
        call = await call_operation(_operator(), _args("salt.job.run_state", _STATE_PARAMS))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert raas.called("cmd", "route_cmd") == []


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("salt.job.run_state", _STATE_PARAMS),
        ("salt.job.run_command", {"tgt": "<web-glob>", "command": "<command>"}),
    ],
)
async def test_run_is_refused_for_an_agent(
    salt: SaltConnector, op_id: str, params: dict[str, Any]
) -> None:
    with _Raas(_run_answers()) as raas:
        result = await _run(
            op_id, params, approved=False, operator=_operator("salt-agent", PrincipalKind.AGENT)
        )
    assert result["status"] == "denied", result
    assert raas.called("cmd", "route_cmd") == []
    assert await _approval_rows() == 0


async def test_run_state_waits_for_a_second_person_then_runs_on_the_card_minions(
    salt: SaltConnector,
) -> None:
    args = _args("salt.job.run_state", _STATE_PARAMS)
    with _Raas(_run_answers()) as raas:
        preview = await preview_operation(_operator(), args)
        assert preview["status"] == "ok", preview
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        assert raas.called("tgt", "get_target_group_minions")[0] == {
            "tgt": {"*": {"tgt": "<web-glob>", "tgt_type": "glob"}}
        }
        block = (await _parked_effect(call))["blast_radius"]
        assert block["object"] == {
            "kind": "salt_job",
            "fun": "state.apply",
            "arg": {"kwarg": {"mods": "<state-a>,<state-b>", "test": True}},
            "target": {"master_id": "*", "tgt_type": "glob", "tgt": "<web-glob>"},
            "minion_count": 3,
            "minions_sha256": minions_sha256(
                [("<master-1>", "<web-1>"), ("<master-1>", "<web-2>"), ("<master-2>", "<web-3>")]
            ),
        }
        assert [child["minion_id"] for child in block["children"]] == [
            "<web-1>",
            "<web-2>",
            "<web-3>",
        ]
        assert raas.called("cmd", "route_cmd") == []
        resumed = await _approve_and_resume(call)
    assert resumed.status == "ok", resumed
    assert resumed.result == {
        "status": "accepted",
        "started": True,
        "jid": _JID,
        "fun": "state.apply",
        "minion_count": 3,
        "status_read": "salt.job.result",
        "reason": None,
    }
    assert raas.called("cmd", "route_cmd") == [
        {
            "cmd": "local",
            "fun": "state.apply",
            "tgt": _LIST_TARGET,
            "arg": {"kwarg": {"mods": "<state-a>,<state-b>", "test": True}},
        }
    ]


async def test_run_stops_when_the_target_matches_other_minions_after_the_approval(
    salt: SaltConnector,
) -> None:
    args = _args("salt.job.run_state", _STATE_PARAMS)
    answers = _run_answers()
    with _Raas(answers) as raas:
        preview = await preview_operation(_operator(), args)
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        answers[("tgt", "get_target_group_minions")] = {
            **_MATCHED,
            "<master-2>": ["<web-3>", "<web-new>"],
        }
        resumed = await _approve_and_resume(call)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "precondition_failed"
    assert resumed.result["started"] is False
    assert "matched 3 minions when it was approved" in resumed.result["reason"]
    assert raas.called("cmd", "route_cmd") == []


async def test_run_without_an_approval_card_starts_nothing(salt: SaltConnector) -> None:
    with _Raas(_run_answers()) as raas:
        result = await _run("salt.job.run_state", _STATE_PARAMS)
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "precondition_failed"
    assert raas.called("cmd", "route_cmd") == []


def _card_for(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """The approval card a preview of *params* gives against :data:`_MATCHED`."""
    from meho_backplane.connectors.salt import job_writes

    fun, arg = job_writes._call_for(op_id, params)
    pairs = sorted((master, minion) for master, minions in _MATCHED.items() for minion in minions)
    return job_writes._card(fun, arg, job_writes._target_expression(params), pairs)


async def test_run_does_not_start_a_job_that_already_runs(salt: SaltConnector) -> None:
    running = {
        "count": 1,
        "results": [
            {"jid": "20260101000000000007", "fun": "state.apply", "state": "retrieved"},
            {
                "jid": "20260101000000000008",
                "fun": "state.apply",
                "state": "new",
                "tgt": _LIST_TARGET,
            },
        ],
    }
    card = _card_for("salt.job.run_state", _STATE_PARAMS)
    with _Raas(_run_answers(**{"cmd.get_cmds": running})) as raas:
        result = await _run("salt.job.run_state", _STATE_PARAMS, card=card)
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "already_running"
    assert result["result"]["started"] is False
    assert result["result"]["jid"] == "20260101000000000008"
    assert raas.called("cmd", "get_cmds")[0]["state"] == [
        "new",
        "retrieved",
        "resuming",
        "resumed",
        "pausing",
        "paused",
        "stopping",
    ]
    assert raas.called("cmd", "route_cmd") == []


async def test_run_on_a_saved_target_resolves_it_by_id(salt: SaltConnector) -> None:
    params = {"tgt_uuid": _SAVED_TARGET}
    card = _card_for("salt.job.run_state", params)
    with _Raas(_run_answers()) as raas:
        result = await _run("salt.job.run_state", params, card=card)
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "accepted"
    assert raas.called("tgt", "get_target_group_minions") == [{"tgt_uuid": _SAVED_TARGET}]
    assert raas.called("cmd", "route_cmd") == [
        {"cmd": "local", "fun": "state.apply", "tgt": _LIST_TARGET}
    ]


@pytest.mark.parametrize(
    "params",
    [{"tgt": "<a>", "tgt_uuid": _SAVED_TARGET}, {"states": ["<state-a>"]}],
    ids=["both", "neither"],
)
async def test_run_needs_exactly_one_target(salt: SaltConnector, params: dict[str, Any]) -> None:
    with _Raas(_run_answers()) as raas:
        result = await _run("salt.job.run_state", params)
    assert result["extras"]["error_code"] == "invalid_params", result
    assert raas.calls == []


async def test_run_card_refuses_a_target_that_matches_nothing(salt: SaltConnector) -> None:
    args = _args("salt.job.run_state", _STATE_PARAMS)
    answers = _run_answers(**{"tgt.get_target_group_minions": {"<master-1>": []}})
    with _Raas(answers) as raas:
        preview = await preview_operation(_operator(), args)
        call = await call_operation(
            _operator(), {**args, "preview_hash": preview.get("preview_hash")}
        )
    assert call["status"] == "denied", call
    assert "matches no minion" in json.dumps(call)
    assert raas.called("cmd", "route_cmd") == []
    assert await _approval_rows() == 0


async def test_run_vendor_error_reaches_the_answer(salt: SaltConnector) -> None:
    card = _card_for("salt.job.run_state", _STATE_PARAMS)
    error = httpx.Response(
        200, json={"riq": 1, "ret": None, "error": {"message": "No master available"}}
    )
    with _Raas(_run_answers(**{"cmd.route_cmd": error})):
        result = await _run("salt.job.run_state", _STATE_PARAMS, card=card)
    assert result["status"] == "error", result
    assert "No master available" in json.dumps(result)


async def test_run_command_card_shows_the_command_in_full(salt: SaltConnector) -> None:
    params = {"tgt": "<web-glob>", "command": "systemctl restart <service-name>"}
    args = _args("salt.job.run_command", params)
    with _Raas(_run_answers()) as raas:
        preview = await preview_operation(_operator(), args)
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        block = (await _parked_effect(call))["blast_radius"]
        assert block["object"]["fun"] == "cmd.run"
        assert block["object"]["arg"] == {"kwarg": {"cmd": "systemctl restart <service-name>"}}
        resumed = await _approve_and_resume(call)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "accepted"
    assert raas.called("cmd", "route_cmd") == [
        {
            "cmd": "local",
            "fun": "cmd.run",
            "tgt": _LIST_TARGET,
            "arg": {"kwarg": {"cmd": "systemctl restart <service-name>"}},
        }
    ]


@pytest.mark.parametrize(
    ("op_id", "params", "arg"),
    [
        (
            "salt.job.run_command",
            {"tgt": "<web-glob>", "command": "LANG=C df -h"},
            {"kwarg": {"cmd": "LANG=C df -h"}},
        ),
        (
            "salt.job.run_state",
            {"tgt": "<web-glob>", "states": ["saltenv=dev"]},
            {"kwarg": {"mods": "saltenv=dev"}},
        ),
    ],
    ids=["command", "state"],
)
async def test_run_sends_a_name_value_text_as_a_keyword_value(
    salt: SaltConnector, op_id: str, params: dict[str, Any], arg: dict[str, Any]
) -> None:
    card = _card_for(op_id, params)
    assert card["blast_radius"]["object"]["arg"] == arg
    with _Raas(_run_answers()) as raas:
        result = await _run(op_id, params, card=card)
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "accepted"
    assert raas.called("cmd", "route_cmd")[0]["arg"] == arg


@pytest.mark.parametrize("minion_id", ["<web-1>,<web-9>", "two words", "web*", "web[12]", "web?"])
async def test_run_card_refuses_a_matched_id_it_cannot_name_alone(
    salt: SaltConnector, minion_id: str
) -> None:
    args = _args("salt.job.run_state", _STATE_PARAMS)
    answers = _run_answers(**{"tgt.get_target_group_minions": {"<master-1>": [minion_id]}})
    with _Raas(answers) as raas:
        preview = await preview_operation(_operator(), args)
        call = await call_operation(
            _operator(), {**args, "preview_hash": preview.get("preview_hash")}
        )
    assert call["status"] == "denied", call
    assert "cannot name them one by one" in json.dumps(call)
    assert raas.called("cmd", "route_cmd") == []
    assert await _approval_rows() == 0


@pytest.mark.parametrize(
    "command",
    [
        "mysql -u admin --password=<value> app",
        "export DB_PASSWORD=<value>",
        "app-cli --token <value> sync",
        "app-cli --api-key <value> sync",
        "curl -u <user>:<value> https://<host>/api",
        "curl --user=<user>:<value> https://<host>/api",
        "curl https://<user>:<value>@<host>/api",
        "sshpass -p <value> ssh <user>@<host> uptime",
        "mysql -u <user> -p<value> app",
        "echo <value> | passwd --stdin <user>",
        "echo '<user>:<value>' | sudo chpasswd",
        "chpasswd <<< '<user>:<value>'",
        "htpasswd -b /etc/nginx/htpasswd <user> <value>",
        "net user <user> <value>",
    ],
)
async def test_run_command_refuses_a_command_with_a_secret(
    salt: SaltConnector, command: str
) -> None:
    assert command_secret_reason(command) is not None
    args = _args("salt.job.run_command", {"tgt": "<web-glob>", "command": command})
    with _Raas(_run_answers()) as raas:
        preview = await preview_operation(_operator(), args)
        call = await call_operation(
            _operator(), {**args, "preview_hash": preview.get("preview_hash")}
        )
    assert call["status"] == "denied", call
    assert "Do not put a secret into a command" in json.dumps(call)
    assert raas.called("cmd", "route_cmd") == []
    assert await _approval_rows() == 0


@pytest.mark.parametrize(
    "command",
    [
        "systemctl restart nginx",
        "passwd -S <user>",
        "ls -la /etc/ssh",
        "uptime",
        "LANG=C df -h",
        "curl -u <user> https://<host>/api",
        "curl https://<host>:8443/api",
        "mysql -u <user> -p app",
        "ssh <user>@<host> uptime",
        "net user <user> /active:yes",
        "cat /etc/passwd | wc -l",
    ],
)
def test_plain_commands_are_not_taken_for_secrets(command: str) -> None:
    assert command_secret_reason(command) is None


async def _enable_trace_capture() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT_ID, slug="salt-writes", name="salt-writes", flight_recorder_enabled=True
            )
        )
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _recorded_bodies(audit_id: str) -> list[Any]:
    async with get_sessionmaker()() as session:
        trace = (
            await session.execute(
                select(DispatchTrace).where(DispatchTrace.audit_id == uuid.UUID(audit_id))
            )
        ).scalar_one()
        spans = (
            await session.execute(
                select(DispatchTraceSpan)
                .where(DispatchTraceSpan.trace_id == trace.id)
                .order_by(DispatchTraceSpan.seq.asc())
            )
        ).scalars()
        return [
            span.attributes.get("response_body")
            for span in spans
            if span.span_kind == "vendor_call"
        ]


async def test_run_trace_stores_no_vendor_body(salt: SaltConnector) -> None:
    """A destructive run stores no body: not the command, not the minions."""
    await _enable_trace_capture()
    params = {"tgt": "<web-glob>", "command": "systemctl restart <service-name>"}
    card = _card_for("salt.job.run_command", params)
    with _Raas(_run_answers()):
        result = await _run("salt.job.run_command", params, card=card)
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "accepted"
    bodies = await _recorded_bodies(result["audit_id"])
    assert len(bodies) == 3
    rendered = json.dumps(bodies)
    for text in ("<service-name>", "<web-1>", _JID, _PASSWORD):
        assert text not in rendered


async def test_cancel_trace_keeps_only_the_job_state_and_the_stop_jid(
    salt: SaltConnector,
) -> None:
    await _enable_trace_capture()
    row = {**_cmds_row("retrieved")["results"][0], "arg": ["plain-extra-value-41"]}
    answers: _Answers = {
        ("cmd", "get_cmds"): {"count": 1, "results": [row]},
        ("cmd", "stop_job"): _STOP_JID,
    }
    with _Raas(answers):
        result = await _run("salt.job.cancel", {"jid": _JID})
    assert result["status"] == "ok", result
    bodies = await _recorded_bodies(result["audit_id"])
    assert [body["ret"] for body in bodies][1] == _STOP_JID
    rendered = json.dumps(bodies)
    assert "retrieved" in rendered
    assert "plain-extra-value" not in rendered
    assert _PASSWORD not in rendered


# ---------------------------------------------------------------------------
# salt.job.cancel
# ---------------------------------------------------------------------------


def _cmds_row(state: str) -> dict[str, Any]:
    return {"count": 1, "results": [{"jid": _JID, "fun": "state.apply", "state": state}]}


async def test_cancel_waits_for_approval(salt: SaltConnector) -> None:
    with _Raas({("cmd", "get_cmds"): _cmds_row("retrieved")}) as raas:
        call = await call_operation(_operator(), _args("salt.job.cancel", {"jid": _JID}))
    assert call["status"] == "awaiting_approval", call
    assert raas.called("cmd", "stop_job") == []


async def test_cancel_stops_a_running_job(salt: SaltConnector) -> None:
    answers: _Answers = {
        ("cmd", "get_cmds"): _cmds_row("retrieved"),
        ("cmd", "stop_job"): _STOP_JID,
    }
    with _Raas(answers) as raas:
        result = await _run("salt.job.cancel", {"jid": _JID})
    assert result["status"] == "ok", result
    assert raas.called("cmd", "get_cmds") == [{"jid": _JID, "limit": 1}]
    assert raas.called("cmd", "stop_job") == [{"jid": _JID}]
    assert result["result"] == {
        "status": "accepted",
        "stop_requested": True,
        "jid": _JID,
        "job_state": "retrieved",
        "job_status": "running",
        "stop_jid": _STOP_JID,
        "status_read": "salt.job.result",
    }


@pytest.mark.parametrize(
    ("state", "status"),
    [("completed_all_successful", "not_running"), ("stopping", "already_stopping")],
)
async def test_cancel_sends_nothing_for_a_job_that_does_not_run(
    salt: SaltConnector, state: str, status: str
) -> None:
    with _Raas({("cmd", "get_cmds"): _cmds_row(state)}) as raas:
        result = await _run("salt.job.cancel", {"jid": _JID})
    assert result["status"] == "ok", result
    assert result["result"]["status"] == status
    assert result["result"]["stop_requested"] is False
    assert raas.called("cmd", "stop_job") == []


@pytest.mark.parametrize(
    "rows",
    [[], [{"fun": "state.apply", "state": "retrieved"}]],
    ids=["no-row", "row-without-jid"],
)
async def test_cancel_refuses_an_unknown_job(salt: SaltConnector, rows: list[Any]) -> None:
    with _Raas({("cmd", "get_cmds"): {"count": len(rows), "results": rows}}) as raas:
        result = await _run("salt.job.cancel", {"jid": _JID})
    assert result["status"] == "error", result
    assert "no job run with jid" in json.dumps(result)
    assert raas.called("cmd", "stop_job") == []


async def test_cancel_vendor_error_reaches_the_answer(salt: SaltConnector) -> None:
    answers: _Answers = {
        ("cmd", "get_cmds"): _cmds_row("retrieved"),
        ("cmd", "stop_job"): httpx.Response(
            200, json={"riq": 1, "ret": None, "error": {"message": "Job is not running"}}
        ),
    }
    with _Raas(answers):
        result = await _run("salt.job.cancel", {"jid": _JID})
    assert result["status"] == "error", result
    assert "Job is not running" in json.dumps(result)


# ---------------------------------------------------------------------------
# salt.job.result as the status read of a started job
# ---------------------------------------------------------------------------

_NO_RETURNS = {"count": 0, "results": []}


async def test_status_read_gives_the_job_state_and_progress(salt: SaltConnector) -> None:
    row = {"jid": _JID, "fun": "state.apply", "state": "retrieved", "expected": 3, "returned": 1}
    answers: _Answers = {
        ("cmd", "get_cmds"): {"count": 1, "results": [row]},
        ("ret", "get_returns"): _NO_RETURNS,
    }
    with _Raas(answers):
        result = await _run("salt.job.result", {"jid": _JID}, approved=False)
    assert result["status"] == "ok", result
    body = result["result"]
    assert body["reachable"] is True
    assert (body["job_state"], body["job_status"], body["job_ended"]) == (
        "retrieved",
        "running",
        False,
    )
    assert (body["expected"], body["returned"]) == (3, 1)


@pytest.mark.parametrize(
    "rows",
    [[], [{"fun": "state.apply", "state": "retrieved"}]],
    ids=["no-row", "row-without-jid"],
)
async def test_status_read_of_an_unknown_job_says_unknown(
    salt: SaltConnector, rows: list[Any]
) -> None:
    answers: _Answers = {
        ("cmd", "get_cmds"): {"count": len(rows), "results": rows},
        ("ret", "get_returns"): _NO_RETURNS,
    }
    with _Raas(answers):
        result = await _run("salt.job.result", {"jid": _JID}, approved=False)
    assert result["status"] == "ok", result
    assert result["result"]["job_status"] == "unknown"
    assert result["result"]["job_ended"] is False


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("connection refused"), httpx.Response(503, text="upgrading")],
    ids=["no-connection", "http-503"],
)
async def test_status_read_says_not_reachable(salt: SaltConnector, failure: Any) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_LOGIN).mock(return_value=_login())
        if isinstance(failure, httpx.Response):
            mock.post(_RPC).mock(return_value=failure)
        else:
            mock.post(_RPC).mock(side_effect=failure)
        result = await _run("salt.job.result", {"jid": _JID}, approved=False)
    assert result["status"] == "ok", result
    body = result["result"]
    assert body["jid"] == _JID
    assert body["reachable"] is False
    assert body["job_status"] == "not reachable"
    assert body["job_ended"] is False
    assert body["checked_at"]
    assert body["error"]
    assert body["results"] == []


async def test_status_read_logs_in_again_after_the_login_expired(salt: SaltConnector) -> None:
    """A 401 between the start and the status read: MEHO logs in again and reads the job."""
    row = {"jid": _JID, "state": "completed_all_successful", "expected": 1, "returned": 1}
    answers: _Answers = {
        ("cmd", "get_cmds"): {"count": 1, "results": [row]},
        ("ret", "get_returns"): _NO_RETURNS,
    }
    first_401 = {"sent": False}
    calls: list[dict[str, Any]] = []
    answer = _fake_raas(answers, calls)

    def _rpc(request: httpx.Request) -> httpx.Response:
        if not first_401["sent"]:
            first_401["sent"] = True
            return httpx.Response(401, json={"message": "session expired"})
        return answer(request)

    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.get(_LOGIN).mock(return_value=_login())
        mock.post(_RPC).mock(side_effect=_rpc)
        result = await _run("salt.job.result", {"jid": _JID}, approved=False)
    assert result["status"] == "ok", result
    assert result["result"]["job_status"] == "succeeded"
    assert result["result"]["job_ended"] is True
    assert login.call_count == 2
