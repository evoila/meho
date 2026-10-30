# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of step 2 of a vCenter install: the setup, its status and its questions.

``vmware.composite.vcenter.install``, ``vmware.vcenter.deployment.status`` and
``vmware.composite.vcenter.deployment.answer`` run on the new appliance as its
own target. These tests use the real ``VmwareRestConnector`` over respx (a
fake appliance on port 5480) and saved answers written by hand from the vendor
API reference. All names and ids are made up. No live system is used.

The end-to-end tests at the bottom go through the real dispatcher: a person's
change waits for approval, a wrong root password fails the sign-in without
the password in the result, the audit row or a log line, and an expired login
between the start and a status read is renewed.
"""

from __future__ import annotations

import json
import logging
import ssl
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest import vcenter_deployment as deployment_mod
from meho_backplane.connectors.vmware_rest.composites import (
    _vcenter_answer,
    _vcenter_install,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    register_vmware_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_HOST = "new-vcenter.test.invalid"
_BASE = f"https://{_HOST}:5480"
_SESSION = "appliance-session-token"
_ROOT_PASSWORD = "Root-canary-3b9d-must-not-leak"
_SSO_PASSWORD = 'Sso-canary"5e1c-must-not-leak'
_SSO_REF = "appliance/new-vcenter/sso"

_DEPLOYMENT = "/api/vcenter/deployment"
_QUESTION = "/api/vcenter/deployment/question"
_CHECK = "/api/vcenter/deployment/install"


def _msg(text: str) -> dict[str, Any]:
    return {"id": "vendor.message.id", "default_message": text, "args": []}


def _info(state: str, status: str = "PENDING", **extra: Any) -> dict[str, Any]:
    """A ``GET /vcenter/deployment`` answer (``Vcenter.Deployment.Info``)."""
    return {
        "state": state,
        "status": status,
        "cancelable": False,
        "service": "com.vmware.vcenter.deployment",
        "operation": "install",
        "description": _msg("Appliance deployment"),
        **extra,
    }


_RUNNING = _info(
    "CONFIG_IN_PROGRESS",
    "RUNNING",
    start_time="2026-10-30T08:00:00.000Z",
    progress={"total": 100, "completed": 40, "message": _msg("Starting services")},
    subtask_order=["<step-network>", "<step-sso>"],
    subtasks={
        "<step-network>": {"status": "SUCCEEDED", "description": _msg("Network")},
        "<step-sso>": {
            "status": "RUNNING",
            "progress": {"total": 10, "completed": 5, "message": _msg("SSO")},
        },
    },
)


def _error(*texts: str) -> dict[str, Any]:
    """A vendor error object: ``error_type`` and a stack of messages (the error, then its cause)."""
    return {"error_type": "ERROR", "messages": [_msg(text) for text in texts]}


_FAILED = _info(
    "FAILED",
    "FAILED",
    error=_error("The setup failed.", "A step did not finish."),
    subtask_order=["<step-sso>"],
    subtasks={
        "<step-sso>": {
            "status": "FAILED",
            "error": _error("SSO setup failed."),
            "result": {"errors": [{"id": "e1", "message": _msg("Port 389 is in use.")}]},
        }
    },
)
_QUESTIONS = {
    "questions": [
        {
            "id": "<question-id>",
            "question": _msg("Trust the certificate of the partner vCenter?"),
            "type": "YES_NO",
            "default_answer": "yes",
            "possible_answers": ["yes", "no"],
        }
    ]
}
_CHECK_OK = {"status": "SUCCESS", "result": {"warnings": [{"id": "w1", "message": _msg("NTP")}]}}
_CHECK_DNS_FAILED = {
    "status": "FAILED",
    "result": {
        "errors": [
            {
                "id": "dns",
                "message": _msg("The host name new-vcenter.example.test does not resolve."),
                "resolution": _msg("Add a DNS record for it."),
            }
        ]
    },
}
_STANDALONE = {
    "standalone": {"sso_domain_name": "vsphere.local"},
    "sso_admin_password_secret_ref": _SSO_REF,
}
_REPLICATED = {
    "replicated": {"partner_hostname": "partner-vcenter.example.test"},
    "sso_admin_password_secret_ref": _SSO_REF,
}


@dataclass
class _Target:
    name: str = "new-vcenter"
    host: str = _HOST
    port: int | None = 5480
    secret_ref: str = "targets/new-vcenter"
    auth_model: str | None = "shared_service_account"
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=11))


def _operator() -> Operator:
    return Operator(
        sub="op-vcenter-install",
        name="Install Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID(int=11),
        tenant_role=TenantRole.OPERATOR,
    )


async def _root_login(target: Any, operator: Any) -> dict[str, str]:
    return {"username": "root", "password": _ROOT_PASSWORD}


@pytest.fixture
def connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_root_login)


@pytest.fixture(autouse=True)
def _sso_vault(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []

    async def _load(target: Any, operator: Any, *, param: str, mount: str) -> dict[str, Any]:
        reads.append(target.secret_ref)
        return {"password": _SSO_PASSWORD}

    monkeypatch.setattr(deployment_mod, "load_param_secret_data", _load)
    return reads


@pytest.fixture(autouse=True)
def gate(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _gate(**kwargs: Any) -> OperationResult | None:
        calls.append(kwargs)
        return None

    monkeypatch.setattr(_vcenter_install, "enforce_subop_policy", _gate)
    monkeypatch.setattr(_vcenter_answer, "enforce_subop_policy", _gate)
    return calls


def _mock() -> respx.MockRouter:
    router = respx.mock(base_url=_BASE, assert_all_called=False)
    return router


def _assert_no_secret(value: Any) -> None:
    blob = json.dumps(value, default=str)
    for secret in (_ROOT_PASSWORD, _SSO_PASSWORD, json.dumps(_SSO_PASSWORD)[1:-1]):
        assert secret not in blob


async def _install(connector: VmwareRestConnector, params: dict[str, Any]) -> Any:
    return await _vcenter_install.vcenter_install_composite(
        operator=_operator(), target=_Target(), params=params, connector=connector
    )


async def _status(connector: VmwareRestConnector) -> dict[str, Any]:
    return await connector.vcenter_deployment_status(_operator(), _Target(), {})


async def _answer(connector: VmwareRestConnector, params: dict[str, Any]) -> Any:
    return await _vcenter_answer.vcenter_deployment_answer_composite(
        operator=_operator(), target=_Target(), params=params, connector=connector
    )


def _steps(out: dict[str, Any]) -> dict[str, str]:
    return {row["step"]: row["result"] for row in out["steps"]}


# ---------------------------------------------------------------------------
# vcenter.install
# ---------------------------------------------------------------------------


async def test_install_checks_then_starts_and_answers_accepted(
    connector: VmwareRestConnector, gate: list[dict[str, Any]]
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        check = mock.post(_CHECK, params={"action": "check"}).respond(200, json=_CHECK_OK)
        start = mock.post(_CHECK, params={"action": "start"}).respond(204)
        out = await _install(connector, dict(_STANDALONE))

    assert out["status"] == "accepted"
    assert out["mode"] == "new_sso_domain"
    assert out["state"] == "ready"
    assert out["check"]["warnings"] == ["NTP"]
    assert out["status_read"] == {
        "op_id": "vmware.vcenter.deployment.status",
        "target": "new-vcenter",
    }
    assert out["job"] == {"kind": "install", "target": "new-vcenter"}
    assert _steps(out) == {
        "read_state": "done",
        "read_sso_password": "done",
        "check": "done",
        "start": "done",
    }
    assert out["issues"] == []
    assert "not done yet" in out["guidance"]
    body = {
        "auto_answer": False,
        "vcsa_embedded": {
            "ceip_enabled": False,
            "standalone": {"sso_admin_password": _SSO_PASSWORD, "sso_domain_name": "vsphere.local"},
        },
    }
    assert json.loads(check.calls[0].request.content) == body
    assert json.loads(start.calls[0].request.content) == body
    (start_gate,) = gate
    assert start_gate["op_id"] == "POST:/vcenter/deployment/install?action=start"
    assert start_gate["params"]["sso_domain_name"] == "vsphere.local"
    _assert_no_secret(out)
    _assert_no_secret(start_gate["params"])


async def test_install_check_fails_returns_the_messages_and_starts_nothing(
    connector: VmwareRestConnector, gate: list[dict[str, Any]]
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        mock.post(_CHECK, params={"action": "check"}).respond(200, json=_CHECK_DNS_FAILED)
        start = mock.post(_CHECK, params={"action": "start"}).respond(204)
        out = await _install(connector, dict(_STANDALONE))

    assert out["status"] == "check_failed"
    assert out["check"]["status"] == "FAILED"
    assert out["check"]["errors"] == [
        "The host name new-vcenter.example.test does not resolve. "
        "Resolution: Add a DNS record for it."
    ]
    assert out["issues"][0]["severity"] == "error"
    assert _steps(out)["check"] == "failed"
    assert _steps(out)["start"] == "not_run"
    assert start.call_count == 0
    assert gate == []


@pytest.mark.parametrize(
    ("vendor_state", "status", "severity"),
    [
        ("NOT_INITIALIZED", "not_ready", "error"),
        ("CONFIGURED", "already_done", "error"),
        ("FAILED", "failed_before", "error"),
        ("CONFIG_IN_PROGRESS", "already_running", "warning"),
        ("QUESTION_RAISED", "waiting_for_answer", "warning"),
    ],
)
async def test_install_stops_and_says_the_state_when_the_appliance_is_not_ready(
    connector: VmwareRestConnector,
    _sso_vault: list[str],
    vendor_state: str,
    status: str,
    severity: str,
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info(vendor_state))
        posts = mock.post(_CHECK)
        out = await _install(connector, dict(_STANDALONE))

    assert out["status"] == status
    assert out["vendor_state"] == vendor_state
    assert out["issues"][0]["severity"] == severity
    assert _steps(out) == {
        "read_state": "done",
        "read_sso_password": "not_run",
        "check": "not_run",
        "start": "not_run",
    }
    assert posts.call_count == 0
    assert _sso_vault == []


async def test_join_with_a_wrong_partner_address_fails_the_check(
    connector: VmwareRestConnector,
) -> None:
    failed = {
        "status": "FAILED",
        "result": {
            "errors": [{"id": "p", "message": _msg("Cannot reach partner-vcenter.example.test.")}]
        },
    }
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        check = mock.post(_CHECK, params={"action": "check"}).respond(200, json=failed)
        start = mock.post(_CHECK, params={"action": "start"}).respond(204)
        out = await _install(connector, dict(_REPLICATED))

    assert out["status"] == "check_failed"
    assert out["mode"] == "join_sso_domain"
    assert out["check"]["errors"] == ["Cannot reach partner-vcenter.example.test."]
    body = json.loads(check.calls[0].request.content)
    assert body["vcsa_embedded"]["replicated"] == {
        "partner_hostname": "partner-vcenter.example.test",
        "sso_admin_password": _SSO_PASSWORD,
    }
    assert start.call_count == 0


async def test_join_with_a_wrong_sso_password_fails_the_check_without_the_password(
    connector: VmwareRestConnector,
) -> None:
    refused = {
        "error_type": "UNAUTHENTICATED",
        "messages": [_msg(f"Login failed for {_SSO_PASSWORD}")],
    }
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        mock.post(_CHECK, params={"action": "check"}).respond(401, json=refused)
        start = mock.post(_CHECK, params={"action": "start"}).respond(204)
        out = await _install(connector, dict(_REPLICATED))

    assert out["status"] == "check_failed"
    assert "partner vCenter refused the SSO login" in out["issues"][0]["message"]
    assert start.call_count == 0
    _assert_no_secret(out)


async def test_a_401_on_the_check_of_a_new_domain_is_raised_without_the_body(
    connector: VmwareRestConnector,
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        mock.post(_CHECK, params={"action": "check"}).respond(
            401, json={"error_type": "UNAUTHENTICATED"}
        )
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await _install(connector, dict(_STANDALONE))

    assert caught.value.response.status_code == 401
    assert caught.value.request.content == b""
    assert caught.value.__context__ is None


async def test_a_lost_reply_of_the_start_says_unknown_and_points_to_the_status_read(
    connector: VmwareRestConnector,
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        mock.post(_CHECK, params={"action": "check"}).respond(200, json={"status": "SUCCESS"})
        mock.post(_CHECK, params={"action": "start"}).mock(side_effect=httpx.ReadTimeout("lost"))
        out = await _install(connector, dict(_STANDALONE))

    assert out["status"] == "start_unknown"
    assert _steps(out)["start"] == "unknown"
    assert "Do not start again blindly" in out["guidance"]
    assert "vmware.vcenter.deployment.status" in out["guidance"]


async def test_a_refused_start_keeps_the_vendor_text_but_never_the_password(
    connector: VmwareRestConnector,
) -> None:
    refused = {
        "error_type": "INVALID_ARGUMENT",
        "messages": [_msg(f"bad password {_SSO_PASSWORD}")],
    }
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        mock.post(_CHECK, params={"action": "check"}).respond(200, json={"status": "SUCCESS"})
        mock.post(_CHECK, params={"action": "start"}).respond(400, json=refused)
        out = await _install(connector, dict(_STANDALONE))

    assert out["status"] == "start_failed"
    assert _steps(out)["check"] == "done"
    assert _steps(out)["start"] == "failed"
    assert out["issues"][0]["message"] == "the appliance refused the start: HTTP 400"
    _assert_no_secret(out)


async def test_a_parked_start_gate_comes_back_and_sends_no_start(
    connector: VmwareRestConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    parked = OperationResult(status="awaiting_approval", op_id="x", duration_ms=0.0)

    async def _park(**_kw: Any) -> OperationResult:
        return parked

    monkeypatch.setattr(_vcenter_install, "enforce_subop_policy", _park)
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("INITIALIZED"))
        mock.post(_CHECK, params={"action": "check"}).respond(200, json={"status": "SUCCESS"})
        start = mock.post(_CHECK, params={"action": "start"}).respond(204)
        out = await _install(connector, dict(_STANDALONE))

    assert out is parked
    assert start.call_count == 0


async def test_install_preview_shows_the_plan_and_no_password() -> None:
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_Target(),
        params=dict(_REPLICATED),
    )
    preview = await _vcenter_install.vcenter_install_preview(ctx)
    assert preview is not None
    assert preview["mode"] == "join_sso_domain"
    assert preview["partner_hostname"] == "partner-vcenter.example.test"
    assert preview["sso_admin_password_secret_ref"] == _SSO_REF
    assert await _vcenter_install.vcenter_install_preview(replace(ctx, params={})) is None


# ---------------------------------------------------------------------------
# vmware.vcenter.deployment.status
# ---------------------------------------------------------------------------


async def test_status_of_a_running_setup_gives_progress_and_steps(
    connector: VmwareRestConnector,
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_RUNNING)
        out = await _status(connector)

    assert out["reachable"] is True
    assert out["state"] == "running"
    assert out["task_status"] == "RUNNING"
    assert out["progress_percent"] == 40
    assert out["progress_message"] == "Starting services"
    assert [(s["name"], s["status"], s["progress_percent"]) for s in out["steps"]] == [
        ("<step-network>", "SUCCEEDED", None),
        ("<step-sso>", "RUNNING", 50),
    ]
    assert out["questions"] == []
    assert out["target"] == "new-vcenter"
    assert out["checked_at"]


async def test_status_of_a_failed_setup_gives_each_step_with_its_error(
    connector: VmwareRestConnector,
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_FAILED)
        out = await _status(connector)

    assert out["state"] == "failed"
    assert out["error"] == "The setup failed. A step did not finish."
    assert out["steps"][0]["error"] == "SSO setup failed."
    assert out["steps"][0]["errors"] == ["Port 389 is in use."]


def test_error_text_reads_vendor_error_objects_and_plain_texts() -> None:
    assert deployment_mod.error_text(_error("The setup failed.")) == "The setup failed."
    assert deployment_mod.error_text({"error_type": "NOT_ALLOWED", "messages": []}) == "NOT_ALLOWED"
    assert deployment_mod.error_text("plain text") == "plain text"
    assert deployment_mod.error_text({}) is None
    assert deployment_mod.error_text("") is None
    assert deployment_mod.error_text(None) is None


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ConnectError("refused"), "connect_failed"),
        (httpx.ConnectTimeout("slow"), "timeout"),
    ],
)
async def test_status_of_an_appliance_that_does_not_answer_is_not_reachable(
    connector: VmwareRestConnector, exc: Exception, reason: str
) -> None:
    with _mock() as mock:
        mock.post("/api/session").mock(side_effect=exc)
        out = await _status(connector)

    assert out["reachable"] is False
    assert out["state"] == "not_reachable"
    assert out["unreachable_reason"] == reason
    assert out["target"] == "new-vcenter"
    assert out["checked_at"]
    assert "does not mean the setup failed" in out["guidance"]


async def test_status_says_the_tls_check_failed_when_the_certificate_changed(
    connector: VmwareRestConnector,
) -> None:
    exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    exc.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    with _mock() as mock:
        mock.post("/api/session").mock(side_effect=exc)
        out = await _status(connector)

    assert out["state"] == "not_reachable"
    assert out["unreachable_reason"] == "tls_verify_failed"


async def test_status_404_means_the_state_is_unknown(connector: VmwareRestConnector) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(404, json={"error_type": "NOT_FOUND"})
        out = await _status(connector)

    assert out["state"] == "unknown"
    assert out["reachable"] is True
    assert "404" in out["read_note"]


# ---------------------------------------------------------------------------
# The setup asks a question: the status shows it, the answer lets it go on
# ---------------------------------------------------------------------------


async def test_a_question_shows_in_the_status_and_the_answer_lets_the_setup_go_on(
    connector: VmwareRestConnector, gate: list[dict[str, Any]]
) -> None:
    asking = _info("QUESTION_RAISED", "BLOCKED")
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=asking)
        mock.get(_QUESTION).respond(200, json=_QUESTIONS)
        answer = mock.post(_QUESTION, params={"action": "answer"}).respond(204)
        status = await _status(connector)
        question = status["questions"][0]
        out = await _answer(connector, {"question_id": question["id"], "answer": "yes"})

    assert status["state"] == "waiting_for_answer"
    assert question == {
        "id": "<question-id>",
        "text": "Trust the certificate of the partner vCenter?",
        "type": "YES_NO",
        "default_answer": "yes",
        "possible_answers": ["yes", "no"],
    }
    assert out["status"] == "accepted"
    assert out["question"]["id"] == "<question-id>"
    assert json.loads(answer.calls[0].request.content) == {
        "question_id": "<question-id>",
        "answer_val": "yes",
    }
    assert gate[0]["op_id"] == "POST:/vcenter/deployment/question?action=answer"
    assert _steps(out) == {"read_state": "done", "read_question": "done", "answer": "done"}


@pytest.mark.parametrize(
    ("state", "params", "status"),
    [
        ("CONFIG_IN_PROGRESS", {"question_id": "<question-id>", "answer": "yes"}, "no_question"),
        ("QUESTION_RAISED", {"question_id": "<other-id>", "answer": "yes"}, "unknown_question"),
        (
            "QUESTION_RAISED",
            {"question_id": "<question-id>", "answer": "maybe"},
            "answer_not_allowed",
        ),
    ],
)
async def test_an_answer_that_does_not_fit_the_open_question_sends_nothing(
    connector: VmwareRestConnector,
    gate: list[dict[str, Any]],
    state: str,
    params: dict[str, str],
    status: str,
) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info(state))
        mock.get(_QUESTION).respond(200, json=_QUESTIONS)
        answer = mock.post(_QUESTION, params={"action": "answer"}).respond(204)
        out = await _answer(connector, params)

    assert out["status"] == status
    assert out["issues"][0]["severity"] == "error"
    assert _steps(out)["answer"] == "not_run"
    assert answer.call_count == 0
    assert gate == []


async def test_a_lost_reply_of_the_answer_says_unknown(connector: VmwareRestConnector) -> None:
    with _mock() as mock:
        mock.post("/api/session").respond(200, json=_SESSION)
        mock.get(_DEPLOYMENT).respond(200, json=_info("QUESTION_RAISED"))
        mock.get(_QUESTION).respond(200, json=_QUESTIONS)
        mock.post(_QUESTION, params={"action": "answer"}).mock(
            side_effect=httpx.ReadTimeout("lost")
        )
        out = await _answer(connector, {"question_id": "<question-id>", "answer": "no"})

    assert out["status"] == "answer_unknown"
    assert _steps(out)["answer"] == "unknown"
    assert "Do not answer again blindly" in out["guidance"]


# ---------------------------------------------------------------------------
# Registry: risk level and approval of each operation of the table
# ---------------------------------------------------------------------------

_EXPECTED_POLICY = {
    "vmware.composite.vcenter.appliance.deploy": ("dangerous", True),
    "vmware.composite.vcenter.install": ("dangerous", True),
    "vmware.composite.vcenter.deployment.answer": ("dangerous", True),
    "vmware.vcenter.deployment.status": ("safe", False),
}


def test_each_operation_has_the_risk_level_and_approval_of_the_table() -> None:
    found = {spec.op_id: (spec.safety_level, spec.requires_approval) for spec in _COMPOSITES}
    found.update({op.op_id: (op.safety_level, op.requires_approval) for op in VMWARE_TYPED_OPS})
    assert {op_id: found[op_id] for op_id in _EXPECTED_POLICY} == _EXPECTED_POLICY
    assert classify_op("vmware.composite.vcenter.appliance.deploy") == "credential_write"
    assert classify_op("vmware.composite.vcenter.install") == "credential_write"


# ---------------------------------------------------------------------------
# End to end through the dispatcher
# ---------------------------------------------------------------------------

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000043a4")
_TARGET_REF = "targets/new-vcenter/root"


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _vault_root_login(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    fake = install_fake_client(monkeypatch, secret={})

    def _read(path: str, mount_point: str = "secret", **_: Any) -> dict[str, Any]:
        assert path == _TARGET_REF
        data = {"username": "root", "password": password}
        return {"data": {"data": data, "metadata": {"version": 1, "path": path}}}

    monkeypatch.setattr(fake.secrets.kv.v2, "read_secret_version", _read)


async def _persist_target() -> TargetORM:
    target = TargetORM(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name="new-vcenter",
        product="vmware",
        version="9.0.0",
        host=_HOST,
        port=5480,
        secret_ref=_TARGET_REF,
        auth_model="shared_service_account",
        aliases=[],
    )
    async with get_sessionmaker()() as s:
        s.add(target)
        await s.commit()
        await s.refresh(target)
    return target


def _person() -> Operator:
    return Operator(
        sub="ops-requester",
        name="Requester",
        email=None,
        raw_jwt="ops-requester.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    await register_vmware_typed_operations(embedding_service=service)
    await register_vmware_composite_operations(embedding_service=service)


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_wrong_root_password_fails_the_sign_in_and_is_nowhere_in_the_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await _register()
    _vault_root_login(monkeypatch, _ROOT_PASSWORD)
    target = await _persist_target()
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            login = mock.post("/api/session").respond(401, json={"error_type": "UNAUTHENTICATED"})
            read = mock.get(_DEPLOYMENT).respond(200, json=_RUNNING)
            result = await dispatch(
                operator=_person(),
                connector_id=_CONNECTOR_ID,
                op_id="vmware.vcenter.deployment.status",
                target=target,
                params={},
            )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert login.call_count >= 1
    assert read.call_count == 0
    async with get_sessionmaker()() as s:
        audit_rows = (await s.execute(select(AuditLog))).scalars().all()
    assert audit_rows
    for label, blob in (
        ("the result", json.dumps(result.model_dump(mode="json"), default=str)),
        ("the audit rows", json.dumps([vars(r) for r in audit_rows], default=str)),
        ("the structlog lines", json.dumps(logs, default=str)),
        ("the std log lines", caplog.text),
    ):
        assert _ROOT_PASSWORD not in blob, f"the root password appears in {label}"


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_expired_login_between_start_and_status_logs_in_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register()
    _vault_root_login(monkeypatch, _ROOT_PASSWORD)
    target = await _persist_target()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        login = mock.post("/api/session").respond(200, json=_SESSION)
        mock.delete("/api/session").respond(204)
        mock.get(_DEPLOYMENT).mock(
            side_effect=[
                httpx.Response(200, json=_RUNNING),
                httpx.Response(401, json={"error_type": "UNAUTHENTICATED"}),
                httpx.Response(200, json=_RUNNING),
            ]
        )
        first = await dispatch(
            operator=_person(),
            connector_id=_CONNECTOR_ID,
            op_id="vmware.vcenter.deployment.status",
            target=target,
            params={},
        )
        second = await dispatch(
            operator=_person(),
            connector_id=_CONNECTOR_ID,
            op_id="vmware.vcenter.deployment.status",
            target=target,
            params={},
        )

    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert second.result["state"] == "running"
    assert login.call_count == 2


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("vmware.composite.vcenter.install", dict(_STANDALONE)),
        (
            "vmware.composite.vcenter.deployment.answer",
            {"question_id": "<question-id>", "answer": "yes"},
        ),
    ],
)
async def test_a_persons_change_waits_for_approval_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, op_id: str, params: dict[str, Any]
) -> None:
    await _register()
    _vault_root_login(monkeypatch, _ROOT_PASSWORD)
    target = await _persist_target()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        anything = mock.route()
        result = await dispatch(
            operator=_person(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=target,
            params=params,
        )

    assert result.status == "awaiting_approval", result.error
    assert anything.call_count == 0
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
    assert row is not None
    assert row.proposed_effect["preview"]["action"]
    _assert_no_secret(row.proposed_effect)
