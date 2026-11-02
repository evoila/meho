# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler tests of the host local-user and Active Directory operations.

The handlers run against a recording fake of the connector's vim seam
(:class:`AccessVim`) with a stub of the sub-op gate and of the Vault read.
Every answer is checked against the operation's response schema.

* the two reads: the users with their role, a failed role read, the domain
  state on a vCenter and on an ESXi target, and the refusals;
* each change: nothing is sent when the host already is as asked, the success
  path (read back after the call or the task), a vendor fault (the fault type
  and text reach the answer, a password in the text never does), and a parked
  gate that sends nothing;
* the local-user changes refuse a vCenter target before any call;
* ``host.local_user.delete``: root is protected, and the preview fills the
  blast radius and refuses a user that does not exist.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors._shared.soap_fault import SoapFault, attach_soap_fault
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vmware_rest import typed_ops_host_access as typed
from meho_backplane.connectors.vmware_rest.composites import _host_ad, _host_local_users, _write
from meho_backplane.connectors.vmware_rest.composites._host_access_schemas import (
    AD_JOIN_RESPONSE_SCHEMA,
    AD_LEAVE_RESPONSE_SCHEMA,
    LOCAL_USER_CREATE_RESPONSE_SCHEMA,
    LOCAL_USER_DELETE_RESPONSE_SCHEMA,
    LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA,
)
from meho_backplane.operations import jsonflux_reducer as reducer_module
from meho_backplane.operations import result_query as rq
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryResultStore
from tests._vmware_host_access_fake import (
    AD_STORE,
    DOMAIN,
    ESXI_HOST,
    HOST,
    PASSWORD,
    AccessVim,
    EsxiTarget,
    VcenterTarget,
    soap_fault_error,
)
from tests._vmware_teardown_fake import GateRecorder, http_status_error, operator, parked

_TYPED = {op.op_id: op for op in typed.TYPED_OPS}
_LIST_SCHEMA = _TYPED["vmware.host.local_user.list"].response_schema
_STATUS_SCHEMA = _TYPED["vmware.host.ad.status"].response_schema
_VAULT = "<vault-path-of-the-password>"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_host_ad, "_TASK_TIMEOUT_SECONDS", 0.0)

    async def _secret(_operator: Any, _target: Any, params: Any, *, prefix: str) -> str:
        assert params[f"{prefix}_secret_ref"] == _VAULT
        return PASSWORD

    monkeypatch.setattr(_host_local_users, "read_secret_param", _secret)
    monkeypatch.setattr(_host_ad, "read_secret_param", _secret)
    return recorder


def _check(schema: Any, out: Any) -> dict[str, Any]:
    assert isinstance(out, dict), out
    Draft202012Validator(schema).validate(out)
    return out


def _esxi() -> AccessVim:
    conn = AccessVim()
    conn.seed(ESXI_HOST)
    return conn


def _no_password(out: Any, conn: AccessVim, gate: GateRecorder) -> None:
    """The password is in no answer, no gate params (the approval data) and no read."""
    assert PASSWORD not in json.dumps(out)
    assert PASSWORD not in json.dumps([call["params"] for call in gate.calls])


async def _user_op(handler: Any, conn: AccessVim, target: Any = None, **params: Any) -> Any:
    return await handler(
        operator=operator(),
        target=target if target is not None else EsxiTarget(),
        params={"user_name": "svc-new", **params},
        connector=conn,
    )


# ===========================================================================
# vmware.host.local_user.list
# ===========================================================================


async def test_local_user_list_gives_the_users_with_their_role() -> None:
    conn = _esxi()
    out = _check(
        _LIST_SCHEMA,
        await typed.host_local_user_list_impl(conn, operator(), EsxiTarget(), {}),  # type: ignore[arg-type]
    )
    assert out["status"] == "ok"
    assert out["host"] == ESXI_HOST
    assert out["users"] == [
        {
            "user": "root",
            "full_name": None,
            "uid": 0,
            "shell_access": True,
            "access_mode": "accessAdmin",
        },
        {
            "user": "svc-old",
            "full_name": "<old service account>",
            "uid": 1001,
            "shell_access": True,
            "access_mode": None,
        },
    ]
    assert out["read_notes"] == []
    [(path, body)] = conn.calls_to("RetrieveUserGroups")
    assert path == "/UserDirectory/ha-user-directory/RetrieveUserGroups"
    assert body == {"searchStr": "", "exactMatch": False, "findUsers": True, "findGroups": False}


@pytest.fixture
def result_store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """The real result store on an in-memory stand-in for Valkey."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    store = MemoryResultStore()
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(reducer_module, "get_result_handle_store", lambda: store)
    return store


@pytest.mark.usefixtures("result_store")
async def test_local_user_list_of_many_users_is_a_handle_with_queryable_rows() -> None:
    """A long user list reduces to rows (not a document): paging and filters work."""
    conn = _esxi()
    conn.role_fault = soap_fault_error("NoPermission", "no access")
    for i in range(300):
        conn.add_user(f"svc-{i:03}", uid=2000 + i, role="accessReadOnly" if i % 2 else None)
    op = operator()
    out = _check(
        _LIST_SCHEMA,
        await typed.host_local_user_list_impl(conn, op, EsxiTarget(), {}),  # type: ignore[arg-type]
    )
    assert len(out["users"]) == 302
    descriptor = _TYPED["vmware.host.local_user.list"]
    assert descriptor.llm_instructions is not None
    context = {
        "op_id": descriptor.op_id,
        "tenant_id": str(op.tenant_id),
        "operator_sub": op.sub,
        "result_digest": descriptor.llm_instructions["result_digest"],
    }

    reduced, handle = await JsonFluxReducer().reduce(out, _LIST_SCHEMA, context)

    assert handle is not None
    assert handle.total_rows == 302
    assert "users" not in reduced
    assert reduced["status"] == "ok"
    assert reduced["host"] == ESXI_HOST
    assert reduced["status_counts"] == {"UNKNOWN": 302}
    assert reduced["read_notes"] == out["read_notes"]

    page = await run_result_query(op, handle.handle_id, ResultQuerySpec(limit=5))
    assert [row["user"] for row in page["rows"]] == [
        "root",
        "svc-000",
        "svc-001",
        "svc-002",
        "svc-003",
    ]
    one = await run_result_query(
        op,
        handle.handle_id,
        ResultQuerySpec(filter=[{"field": "user", "op": "=", "value": "svc-299"}]),
    )
    assert one["rows"] == [
        {
            "user": "svc-299",
            "full_name": None,
            "uid": 2299,
            "shell_access": True,
            "access_mode": None,
        }
    ]


async def test_local_user_list_keeps_the_users_when_the_role_read_fails() -> None:
    conn = _esxi()
    conn.role_fault = soap_fault_error("NoPermission", "no access")
    out = _check(
        _LIST_SCHEMA,
        await typed.host_local_user_list_impl(conn, operator(), EsxiTarget(), {}),  # type: ignore[arg-type]
    )
    assert [row["user"] for row in out["users"]] == ["root", "svc-old"]
    assert all(row["access_mode"] is None for row in out["users"])
    assert out["read_notes"] and "access roles" in out["read_notes"][0]


async def test_local_user_list_refuses_a_vcenter_target_before_any_call() -> None:
    conn = AccessVim()
    out = _check(
        _LIST_SCHEMA,
        await typed.host_local_user_list_impl(conn, operator(), VcenterTarget(), {}),  # type: ignore[arg-type]
    )
    assert out["status"] == "needs_esxi_target"
    assert out["users"] == []
    assert conn.calls == []


# ===========================================================================
# vmware.composite.host.local_user.create
# ===========================================================================


async def test_create_sends_the_spec_and_reads_the_user_back(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_CREATE_RESPONSE_SCHEMA,
        await _user_op(
            _host_local_users.local_user_create_composite,
            conn,
            description="<backup account>",
            password_secret_ref=_VAULT,
        ),
    )
    assert out["status"] == "created"
    assert out["user"]["user"] == "svc-new"
    [(path, body)] = conn.calls_to("CreateUser")
    assert path == "/HostLocalAccountManager/ha-localacctmgr/CreateUser"
    assert body == {
        "user": {
            "_typeName": "HostAccountSpec",
            "id": "svc-new",
            "password": PASSWORD,
            "description": "<backup account>",
        }
    }
    assert gate.calls[0]["op_id"] == "POST:/HostLocalAccountManager/{moId}/CreateUser"
    assert gate.calls[0]["params"]["password_secret_ref"] == _VAULT
    _no_password(out, conn, gate)


async def test_create_of_an_existing_user_sends_nothing(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_CREATE_RESPONSE_SCHEMA,
        await _user_op(
            _host_local_users.local_user_create_composite,
            conn,
            user_name="svc-old",
            password_secret_ref=_VAULT,
        ),
    )
    assert out["status"] == "already_exists"
    assert conn.calls_to("CreateUser") == []
    assert gate.calls == []


async def test_create_fault_reaches_the_answer_without_the_password(gate: GateRecorder) -> None:
    conn = _esxi()
    conn.faults["CreateUser"] = soap_fault_error(
        "InvalidArgument", f"password {PASSWORD} does not meet the rules"
    )
    out = _check(
        LOCAL_USER_CREATE_RESPONSE_SCHEMA,
        await _user_op(
            _host_local_users.local_user_create_composite, conn, password_secret_ref=_VAULT
        ),
    )
    assert out["status"] == "failed"
    assert out["error_type"] == "InvalidArgument"
    assert out["error_message"] and PASSWORD not in out["error_message"]
    assert out["issues"][0]["severity"] == "error"
    _no_password(out, conn, gate)


async def test_create_vi_json_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = _esxi()
    conn.faults["CreateUser"] = http_status_error(
        500,
        json.dumps({"_typeName": "AlreadyExists", "faultMessage": [{"message": "user exists"}]}),
    )
    out = await _user_op(
        _host_local_users.local_user_create_composite, conn, password_secret_ref=_VAULT
    )
    assert out["status"] == "failed"
    assert out["error_type"] == "AlreadyExists"


async def test_create_waits_when_the_gate_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _esxi()
    recorder = GateRecorder(parked("POST:/HostLocalAccountManager/{moId}/CreateUser"))
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)

    async def _secret(*_a: Any, **_k: Any) -> str:
        return PASSWORD

    monkeypatch.setattr(_host_local_users, "read_secret_param", _secret)
    out = await _user_op(
        _host_local_users.local_user_create_composite, conn, password_secret_ref=_VAULT
    )
    assert getattr(out, "status", None) == "awaiting_approval"
    assert conn.calls_to("CreateUser") == []


@pytest.mark.parametrize(
    "handler",
    [
        _host_local_users.local_user_create_composite,
        _host_local_users.local_user_password_set_composite,
        _host_local_users.local_user_delete_composite,
    ],
)
async def test_local_user_changes_refuse_a_vcenter_target(handler: Any, gate: GateRecorder) -> None:
    conn = AccessVim()
    out = await handler(
        operator=operator(),
        target=None,
        params={"user_name": "svc-new", "password_secret_ref": _VAULT},
        connector=conn,
    )
    assert out["status"] == "needs_esxi_target"
    assert conn.calls == []
    assert gate.calls == []


# ===========================================================================
# vmware.composite.host.local_user.password.set
# ===========================================================================


async def test_password_set_sends_the_new_password(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA,
        await _user_op(
            _host_local_users.local_user_password_set_composite,
            conn,
            user_name="svc-old",
            password_secret_ref=_VAULT,
        ),
    )
    assert out["status"] == "password_set"
    assert out["user"]["user"] == "svc-old"
    [(path, body)] = conn.calls_to("UpdateUser")
    assert path == "/HostLocalAccountManager/ha-localacctmgr/UpdateUser"
    assert body == {"user": {"_typeName": "HostAccountSpec", "id": "svc-old", "password": PASSWORD}}
    _no_password(out, conn, gate)


async def test_password_set_of_a_missing_user_sends_nothing(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA,
        await _user_op(
            _host_local_users.local_user_password_set_composite, conn, password_secret_ref=_VAULT
        ),
    )
    assert out["status"] == "not_found"
    assert gate.calls == []


async def test_password_set_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = _esxi()
    conn.faults["UpdateUser"] = soap_fault_error("InvalidArgument", f"bad password {PASSWORD}")
    out = _check(
        LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA,
        await _user_op(
            _host_local_users.local_user_password_set_composite,
            conn,
            user_name="svc-old",
            password_secret_ref=_VAULT,
        ),
    )
    assert out["status"] == "failed"
    assert out["error_type"] == "InvalidArgument"
    _no_password(out, conn, gate)


# ===========================================================================
# vmware.composite.host.local_user.delete
# ===========================================================================


async def test_delete_removes_the_user_and_reads_back(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_DELETE_RESPONSE_SCHEMA,
        await _user_op(_host_local_users.local_user_delete_composite, conn, user_name="svc-old"),
    )
    assert out["status"] == "deleted"
    assert out["user"]["user"] == "svc-old"
    [(path, body)] = conn.calls_to("RemoveUser")
    assert path == "/HostLocalAccountManager/ha-localacctmgr/RemoveUser"
    assert body == {"userName": "svc-old"}
    assert "svc-old" not in conn.users


async def test_delete_of_a_missing_user_is_unchanged(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_DELETE_RESPONSE_SCHEMA,
        await _user_op(_host_local_users.local_user_delete_composite, conn),
    )
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_delete_never_removes_root(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(
        LOCAL_USER_DELETE_RESPONSE_SCHEMA,
        await _user_op(_host_local_users.local_user_delete_composite, conn, user_name="root"),
    )
    assert out["status"] == "protected"
    assert conn.calls_to("RemoveUser") == []


async def test_delete_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = _esxi()
    conn.faults["RemoveUser"] = soap_fault_error("UserNotFound", "no such user")
    out = _check(
        LOCAL_USER_DELETE_RESPONSE_SCHEMA,
        await _user_op(_host_local_users.local_user_delete_composite, conn, user_name="svc-old"),
    )
    assert out["status"] == "failed"
    assert (out["error_type"], out["error_message"]) == ("UserNotFound", "no such user")


async def test_delete_that_does_not_take_is_still_present(gate: GateRecorder) -> None:
    conn = _esxi()
    conn.apply_writes = False
    out = _check(
        LOCAL_USER_DELETE_RESPONSE_SCHEMA,
        await _user_op(_host_local_users.local_user_delete_composite, conn, user_name="svc-old"),
    )
    assert out["status"] == "still_present"
    assert out["issues"][0]["severity"] == "error"


def _preview_ctx(params: dict[str, Any], conn: Any, target: Any) -> Any:
    from meho_backplane.operations._preview import PreviewContext

    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]
        connector_instance=conn,
        operator=operator(),
        target=target,
        params=params,
    )


async def test_delete_preview_fills_the_blast_radius() -> None:
    conn = _esxi()
    conn.roles["svc-old"] = "accessReadOnly"
    preview = await _host_local_users._delete_preview(
        _preview_ctx({"user_name": "svc-old"}, conn, EsxiTarget())
    )
    assert preview is not None
    assert blast_radius_missing_reason(preview) is None
    blast = preview["blast_radius"]
    assert blast["object"]["user"] == "svc-old"
    assert blast["object"]["present"] is True
    assert blast["children"] == [
        {"kind": "access_role", "user": "svc-old", "access_mode": "accessReadOnly"}
    ]
    assert blast["irreversibility"] == "permanent"
    assert conn.calls_to("RemoveUser") == []


async def test_delete_preview_refuses_a_missing_user() -> None:
    conn = _esxi()
    with pytest.raises(LookupError, match="no local user"):
        await _host_local_users._delete_preview(
            _preview_ctx({"user_name": "svc-none"}, conn, EsxiTarget())
        )


async def test_create_preview_names_the_vault_path_not_the_password() -> None:
    conn = _esxi()
    preview = await _host_local_users._create_preview(
        _preview_ctx({"user_name": "svc-new", "password_secret_ref": _VAULT}, conn, EsxiTarget())
    )
    assert preview is not None
    assert preview["password_secret_ref"] == _VAULT
    assert preview["current_user"] is None
    assert PASSWORD not in json.dumps(preview)


# ===========================================================================
# vmware.host.ad.status
# ===========================================================================


async def test_ad_status_on_a_vcenter_target_names_the_domain() -> None:
    conn = AccessVim()
    conn.seed(joined="CORP.EXAMPLE")
    out = _check(
        _STATUS_SCHEMA,
        await typed.host_ad_status_impl(conn, operator(), VcenterTarget(), {"host": HOST}),  # type: ignore[arg-type]
    )
    assert out == {
        "status": "ok",
        "host": HOST,
        "joined": True,
        "domain": "CORP.EXAMPLE",
        "trusted_domains": ["<trusted-domain>"],
        "membership_status": "ok",
    }


async def test_ad_status_on_an_esxi_target_reads_the_host_itself() -> None:
    conn = _esxi()
    out = _check(
        _STATUS_SCHEMA,
        await typed.host_ad_status_impl(conn, operator(), EsxiTarget(), {}),  # type: ignore[arg-type]
    )
    assert out["status"] == "ok"
    assert out["host"] == ESXI_HOST
    assert out["joined"] is False
    assert out["domain"] is None
    assert not [path for path, _ in conn.calls if path.startswith("GET")]


async def test_ad_status_needs_a_host_on_a_vcenter_target() -> None:
    conn = AccessVim()
    out = _check(
        _STATUS_SCHEMA,
        await typed.host_ad_status_impl(conn, operator(), VcenterTarget(), {}),  # type: ignore[arg-type]
    )
    assert out["status"] == "host_required"


# ===========================================================================
# vmware.composite.host.ad.join / leave
# ===========================================================================


async def _join(conn: AccessVim, target: Any = None, **params: Any) -> Any:
    return await _host_ad.ad_join_composite(
        operator=operator(),
        target=target,
        params={
            "host": HOST,
            "domain": DOMAIN,
            "user_name": "<join-account>",
            "password_secret_ref": _VAULT,
            **params,
        },
        connector=conn,
    )


async def _leave(conn: AccessVim, **params: Any) -> Any:
    return await _host_ad.ad_leave_composite(
        operator=operator(), target=None, params={"host": HOST, **params}, connector=conn
    )


async def test_join_sends_the_task_and_reads_the_domain_back(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed()
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "joined"
    assert out["domain"] == "CORP.EXAMPLE"
    assert out["task"] == "task-1"
    [(path, body)] = conn.calls_to("JoinDomain_Task")
    assert path == f"/HostActiveDirectoryAuthentication/{AD_STORE}/JoinDomain_Task"
    assert body == {"domainName": DOMAIN, "userName": "<join-account>", "password": PASSWORD}
    assert gate.calls[0]["op_id"] == (
        "POST:/HostActiveDirectoryAuthentication/{moId}/JoinDomain_Task"
    )
    _no_password(out, conn, gate)


async def test_join_on_an_esxi_target_uses_the_host_itself(gate: GateRecorder) -> None:
    conn = _esxi()
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn, target=EsxiTarget()))
    assert out["status"] == "joined"
    assert out["host"] == ESXI_HOST


async def test_join_of_the_same_domain_sends_nothing(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed(joined="CORP.EXAMPLE")
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_join_while_in_another_domain_sends_nothing(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed(joined="OTHER.EXAMPLE")
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "joined_other_domain"
    assert conn.calls_to("JoinDomain_Task") == []


async def test_join_task_fault_reaches_the_answer_without_the_password(
    gate: GateRecorder,
) -> None:
    conn = AccessVim()
    conn.seed()
    conn.task_faults["JoinDomain_Task"] = ("InvalidLogin", f"login with {PASSWORD} refused")
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "failed"
    assert out["error_type"] == "InvalidLogin"
    assert out["error_message"] and PASSWORD not in out["error_message"]
    assert out["issues"][0]["severity"] == "error"
    _no_password(out, conn, gate)


async def test_join_start_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed()
    conn.faults["JoinDomain_Task"] = http_status_error(
        500, json.dumps({"_typeName": "InvalidState", "faultMessage": [{"message": "busy"}]})
    )
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "failed"
    assert out["error_type"] == "InvalidState"


def _call_login_fault(transport: str, text: str) -> ConnectorAuthError:
    """The error each transport raises for an ``InvalidLogin`` fault of a call."""
    if transport == "soap":
        err = ConnectorAuthError(
            "rejected", status_code=401, cause="session_establish_InvalidLogin", stage="call"
        )
        attach_soap_fault(err, SoapFault("ServerFaultCode", text, "InvalidLogin"))
        return err
    err = ConnectorAuthError(
        "rejected", status_code=500, cause="vim_fault_InvalidLogin", stage="call"
    )
    err.__cause__ = http_status_error(
        500, json.dumps({"_typeName": "InvalidLogin", "faultMessage": [{"message": text}]})
    )
    return err


@pytest.mark.parametrize("transport", ["vi-json", "soap"])
async def test_join_refused_login_is_a_failed_join_not_a_target_login_problem(
    gate: GateRecorder, transport: str
) -> None:
    """``InvalidLogin`` of the join call means the join account, not the target's credential."""
    conn = AccessVim()
    conn.seed()
    conn.faults["JoinDomain_Task"] = _call_login_fault(transport, f"login with {PASSWORD} refused")
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "failed"
    assert out["error_type"] == "InvalidLogin"
    assert out["error_message"] and PASSWORD not in out["error_message"]
    assert "join account" in out["guidance"]
    assert out["issues"][0]["severity"] == "error"
    _no_password(out, conn, gate)


async def test_join_session_login_problem_still_goes_to_the_dispatcher(
    gate: GateRecorder,
) -> None:
    """``InvalidLogin`` of the session login (the target's credential) is not a join fault."""
    conn = AccessVim()
    conn.seed()
    err = ConnectorAuthError("rejected", status_code=401, cause="session_establish_InvalidLogin")
    attach_soap_fault(err, SoapFault("ServerFaultCode", "login refused", "InvalidLogin"))
    conn.faults["JoinDomain_Task"] = err
    with pytest.raises(ConnectorAuthError):
        await _join(conn)


async def test_create_refused_login_still_goes_to_the_dispatcher(gate: GateRecorder) -> None:
    """Only the join carries a login of its own; a user create has no such fault."""
    conn = _esxi()
    conn.faults["CreateUser"] = _call_login_fault("soap", "login refused")
    with pytest.raises(ConnectorAuthError):
        await _user_op(
            _host_local_users.local_user_create_composite, conn, password_secret_ref=_VAULT
        )


async def test_join_timeout_is_an_error(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed()
    conn.running.add("JoinDomain_Task")
    out = _check(AD_JOIN_RESPONSE_SCHEMA, await _join(conn))
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"


async def test_join_waits_when_the_gate_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AccessVim()
    conn.seed()
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked("join")))

    async def _secret(*_a: Any, **_k: Any) -> str:
        return PASSWORD

    monkeypatch.setattr(_host_ad, "read_secret_param", _secret)
    out = await _join(conn)
    assert getattr(out, "status", None) == "awaiting_approval"
    assert conn.calls_to("JoinDomain_Task") == []


async def test_leave_sends_force_and_reads_back(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed(joined="CORP.EXAMPLE")
    out = _check(AD_LEAVE_RESPONSE_SCHEMA, await _leave(conn, force=True))
    assert out["status"] == "left"
    assert out["joined"] is False
    [(path, body)] = conn.calls_to("LeaveCurrentDomain_Task")
    assert path == f"/HostActiveDirectoryAuthentication/{AD_STORE}/LeaveCurrentDomain_Task"
    assert body == {"force": True}
    assert gate.calls[0]["params"]["domain"] == "CORP.EXAMPLE"


async def test_leave_when_not_joined_sends_nothing(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed()
    out = _check(AD_LEAVE_RESPONSE_SCHEMA, await _leave(conn))
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_leave_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = AccessVim()
    conn.seed(joined="CORP.EXAMPLE")
    conn.task_faults["LeaveCurrentDomain_Task"] = ("AuthMinimumAdminPermission", "perms exist")
    out = _check(AD_LEAVE_RESPONSE_SCHEMA, await _leave(conn))
    assert out["status"] == "failed"
    assert (out["error_type"], out["error_message"]) == (
        "AuthMinimumAdminPermission",
        "perms exist",
    )
    assert out["joined"] is True


async def test_ad_preview_shows_the_state_and_the_vault_path() -> None:
    conn = AccessVim()
    conn.seed()

    class _Descriptor:
        op_id = _host_ad.OP_ID_AD_JOIN

    from meho_backplane.operations._preview import PreviewContext

    preview = await _host_ad._ad_preview(
        PreviewContext(
            descriptor=_Descriptor(),  # type: ignore[arg-type]
            connector_instance=conn,
            operator=operator(),
            target=None,
            params={
                "host": HOST,
                "domain": DOMAIN,
                "user_name": "<join-account>",
                "password_secret_ref": _VAULT,
            },
        )
    )
    assert preview is not None
    assert preview["current"]["joined"] is False
    assert preview["password_secret_ref"] == _VAULT
    assert "what_happens_next" in preview
    assert PASSWORD not in json.dumps(preview)
