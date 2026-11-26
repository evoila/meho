# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the caller reads when the Keycloak realm answer check stops a call.

Through ``dispatch``, ``preview_operation``, ``approve_request`` and the run
after approval, against a fake Keycloak:

* An HTTP error or an answer that is not JSON for the check read before the
  policy gate: the text names the target and what to check, and holds no
  text of the answer. It does not ask to call again or to fix the call.
* A check read in the handler that times out, also in a run after an
  approval: a failed read that says nothing was changed. A timeout of the
  operation's own write keeps ``execution_outcome: uncertain``.
* A realm read that Keycloak answers through a redirect: the stored trace
  and the agent copy keep the address MEHO asked for.
* A run after approval whose rebuilt card reads another realm: the text
  says that Keycloak did not answer with the realm the target manages.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, DispatchTraceSpan
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.agent_view import agent_view_attributes
from meho_backplane.operations import _park_check, dispatch
from meho_backplane.operations._errors import result_park_check_refused
from meho_backplane.operations.approval_queue import (
    approve_request,
    refuse_resume_if_preview_changed,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings
from tests.test_keycloak_realm_answer_check import (
    _ANSWER_MARKER,
    _ID,
    _KC_BASE_URL,
    _MANAGED,
    _PARAMS,
    _REALM_PATH,
    _TARGET_NAME,
    _approval_rows,
    _audit_text,
    _call,
    _Keycloak,
    _preview,
    _resolved_target,
    cards,  # noqa: F401 -- the fixture of the module above
    seeded,  # noqa: F401 -- the fixture of the module above
)

pytestmark = pytest.mark.usefixtures("real_keycloak_realm_check")

_CARDS = pytest.mark.parametrize(
    "cards", [False, True], ids=["cards-off", "cards-on"], indirect=True
)
_ANSWER_TEXT = "made-up-answer-text-qv"
_STATUSES: list[int | str] = [401, 403, 404, 500, "html"]


class _ErrorAnswer(_Keycloak):
    """The managed realm read answers *status* with a body of made-up text, or HTML."""

    def __init__(self, status: int | str) -> None:
        super().__init__(None)
        self.error = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == _REALM_PATH:
            self.calls.append(f"GET {request.url.path}")
            if self.error == "html":
                return httpx.Response(
                    200,
                    content=f"<html><body>{_ANSWER_TEXT}</body></html>".encode(),
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(int(self.error), json={"error": _ANSWER_TEXT})
        return super().__call__(request)


def _assert_says_what_to_check(error: str | None, status: int | str) -> None:
    assert error is not None
    assert repr(_TARGET_NAME) in error
    assert "'managed_realm'" in error
    assert "the address of the Keycloak server" in error
    assert "the rights of MEHO's admin account in Keycloak" in error
    assert (f"HTTP {status}" if status != "html" else "the answer is not JSON") in error
    assert "call the operation again" not in error
    assert "fix the call" not in error
    assert "approval" not in error
    assert _ANSWER_TEXT not in error


@_CARDS
@pytest.mark.parametrize("status", _STATUSES)
@pytest.mark.parametrize("op_id", ["keycloak.realm.get", "keycloak.user.disable"])
async def test_a_check_read_error_says_what_to_check(
    seeded: Operator,  # noqa: F811
    op_id: str,
    status: int | str,
) -> None:
    keycloak = _ErrorAnswer(status)
    with capture_logs() as logs:
        result = await _call(seeded, op_id, keycloak)
    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    _assert_says_what_to_check(result.error, status)
    assert _ANSWER_TEXT not in json.dumps(result.model_dump(mode="json"), default=str)
    assert set(keycloak.calls) == {f"GET {_REALM_PATH}"}
    assert await _approval_rows() == []
    assert _ANSWER_TEXT not in await _audit_text(seeded)
    assert _ANSWER_TEXT not in json.dumps(logs, default=str)

    keycloak = _ErrorAnswer(status)
    envelope = await _preview(seeded, op_id, keycloak)
    assert envelope["error"] == result.error
    assert envelope["policy_verdict"] == "deny"
    assert _ANSWER_TEXT not in json.dumps(envelope, default=str)
    assert set(keycloak.calls) == {f"GET {_REALM_PATH}"}
    assert await _approval_rows() == []


@_CARDS
@pytest.mark.parametrize("status", [401, 403, 500])
@pytest.mark.parametrize("op_id", ["keycloak.realm.create", "keycloak.realm.delete"])
async def test_a_check_read_error_of_realm_create_and_delete_says_what_to_check(
    seeded: Operator,  # noqa: F811
    op_id: str,
    status: int,
) -> None:
    keycloak = _ErrorAnswer(status)
    result = await _call(seeded, op_id, keycloak)
    assert result.extras["error_code"] == "refused_before_approval"
    _assert_says_what_to_check(result.error, status)
    assert set(keycloak.calls) == {f"GET {_REALM_PATH}"}
    assert await _approval_rows() == []


async def test_a_check_before_the_wait_for_approval_keeps_its_own_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raises(_ctx: Any) -> str | None:
        raise RuntimeError("made-up failure")

    monkeypatch.setitem(_park_check._PARK_CHECKS, "example.op.raises", _raises)
    ctx = SimpleNamespace(
        descriptor=SimpleNamespace(op_id="example.op.raises"), operator=None, target=None
    )
    reason = await _park_check.run_park_check(ctx)  # type: ignore[arg-type]
    assert reason == (
        "the check that runs before the wait for approval failed (RuntimeError); "
        "call the operation again"
    )
    refused = result_park_check_refused("example.op.raises", reason, 0.0)
    assert refused.error is not None
    assert refused.error.endswith(
        "MEHO does not store this call for approval; fix the call and send it again"
    )


# ---------------------------------------------------------------------------
# A check read in the handler that times out
# ---------------------------------------------------------------------------


class _Armed(_Keycloak):
    """Answers with the managed realm; once armed, a request times out as told."""

    def __init__(self) -> None:
        super().__init__({"realm": _MANAGED})
        self.realm_reads_left: int | None = None
        self.put_times_out = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == _REALM_PATH and self.realm_reads_left is not None:
            self.calls.append(f"GET {path}")
            if self.realm_reads_left == 0:
                raise httpx.ReadTimeout("timed out", request=request)
            self.realm_reads_left -= 1
            return httpx.Response(200, json=self.answer)
        if request.method == "GET" and path.endswith(f"/users/{_ID}"):
            self.calls.append(f"GET {path}")
            return httpx.Response(200, json={"id": _ID, "username": "alice", "enabled": True})
        if request.method == "PUT" and self.put_times_out:
            self.calls.append(f"PUT {path}")
            raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def _approver(requester: Operator) -> Operator:
    return Operator(
        sub="keycloak-answer-approver",
        name="Keycloak answer approver",
        email=None,
        raw_jwt="<keycloak-answer-approver-jwt>",
        tenant_id=requester.tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        web_session=True,
    )


async def _approve_and_run(requester: Operator, keycloak: _Keycloak) -> Any:
    (request,) = await _approval_rows()
    approver = _approver(requester)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        async with get_sessionmaker()() as session:
            await approve_request(session, request.id, operator=approver)
            await session.commit()
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request.id)
            assert row is not None
            return await resume_dispatch_after_approval(operator=approver, request=row)


def _is_uncertain(result: Any) -> bool:
    return (
        "execution_outcome" in result.extras
        or result.extras.get("retry") is False
        or "does not know if the change happened" in (result.error or "")
    )


@_CARDS
async def test_a_check_read_timeout_after_approval_says_nothing_was_changed(
    seeded: Operator,  # noqa: F811
) -> None:
    op_id = "keycloak.user.disable"
    keycloak = _Armed()
    waiting = await _call(seeded, op_id, keycloak)
    assert waiting.extras.get("error_code") == "awaiting_approval", waiting.error
    # At the run: the rebuilt card's realm read passes, the handler's check read times out.
    keycloak.calls.clear()
    keycloak.realm_reads_left = 1
    result = await _approve_and_run(seeded, keycloak)
    assert result.status == "error", result
    assert result.extras["error_code"] == "connector_timeout"
    assert not _is_uncertain(result)
    assert "nothing was changed" in (result.error or "")
    assert keycloak.calls[-1] == f"GET {_REALM_PATH}"
    assert all(call.startswith("GET ") for call in keycloak.calls)


@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
async def test_a_check_read_timeout_of_a_read_is_a_plain_timeout(
    seeded: Operator,  # noqa: F811
) -> None:
    keycloak = _Armed()
    # The check read before the policy gate passes, the handler's check read times out.
    keycloak.realm_reads_left = 1
    result = await _call(seeded, "keycloak.realm.get", keycloak)
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_timeout"
    assert not _is_uncertain(result)
    assert result.error == "connector_timeout: ReadTimeout"


@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
async def test_a_timeout_of_the_write_itself_stays_uncertain(
    seeded: Operator,  # noqa: F811
) -> None:
    keycloak = _Armed()
    waiting = await _call(seeded, "keycloak.user.disable", keycloak)
    assert waiting.extras.get("error_code") == "awaiting_approval", waiting.error
    keycloak.put_times_out = True
    result = await _approve_and_run(seeded, keycloak)
    assert result.extras["error_code"] == "connector_timeout"
    assert result.extras.get("execution_outcome") == "uncertain"
    assert result.extras.get("retry") is False
    assert any(call.startswith("PUT ") for call in keycloak.calls)


# ---------------------------------------------------------------------------
# A realm read that Keycloak answers through a redirect
# ---------------------------------------------------------------------------

_OTHER_REALM_NAME = "made-up-other-realm-kx"
_OTHER_PATH = f"/admin/realms/{_OTHER_REALM_NAME}"


class _Redirects(_Keycloak):
    """The checks pass; then the managed realm read is redirected to another realm's path."""

    def __init__(self, redirect: int, other_status: int, *, checks: int) -> None:
        super().__init__({"realm": _MANAGED})
        self.redirect = redirect
        self.other_status = other_status
        self.checks_left = checks

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == _REALM_PATH:
            self.calls.append(f"GET {path}")
            if self.checks_left > 0:
                self.checks_left -= 1
                return httpx.Response(200, json=self.answer)
            return httpx.Response(self.redirect, headers={"Location": _OTHER_PATH})
        if path == _OTHER_PATH:
            self.calls.append(f"GET {path}")
            if self.other_status != 200:
                return httpx.Response(self.other_status, json={"realm": _OTHER_REALM_NAME})
            return httpx.Response(200, json={"realm": _OTHER_REALM_NAME, "id": "other"})
        return super().__call__(request)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record every dispatch, as for a tenant with the flight recorder on."""

    async def _on(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(capture, "should_capture", _on)


async def _trace_text(tenant_id: UUID) -> str:
    """Every stored span of the tenant, and its agent copy, as text."""
    async with get_sessionmaker()() as session:
        spans = (await session.execute(select(DispatchTraceSpan))).scalars().all()
    rows = [
        {c.name: getattr(span, c.name) for c in span.__table__.columns}
        | {"agent_copy": agent_view_attributes(dict(span.attributes or {}))}
        for span in spans
    ]
    assert rows, f"no stored span for tenant {tenant_id}"
    return json.dumps(rows, default=str)


@pytest.mark.usefixtures("recorded")
@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
@pytest.mark.parametrize("switch", [False, True], ids=["switch-off", "switch-on"])
@pytest.mark.parametrize("redirect", [302, 307])
@pytest.mark.parametrize("other_status", [200, 500])
@pytest.mark.parametrize("op_id", ["keycloak.realm.get", "keycloak.realm.update"])
async def test_a_redirected_realm_read_keeps_the_address_meho_asked_for(
    seeded: Operator,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    redirect: int,
    other_status: int,
    switch: bool,
) -> None:
    monkeypatch.setenv("KEYCLOAK_TRACE_RETRY_STATUS_ONLY", "true" if switch else "false")
    get_settings.cache_clear()
    # A read runs the gate check and the handler check first; an approved
    # write runs only the handler check.
    approved = op_id != "keycloak.realm.get"
    keycloak = _Redirects(redirect, other_status, checks=1 if approved else 2)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        result = await dispatch(
            operator=seeded,
            connector_id="keycloak-admin-26.x",
            op_id=op_id,
            target=await _resolved_target(seeded),
            params=dict(_PARAMS.get(op_id, {})),
            _approved=approved,
        )
    assert result.status != "ok", result
    assert f"GET {_OTHER_PATH}" in keycloak.calls
    assert not any(call.startswith("PUT ") for call in keycloak.calls)
    trace = await _trace_text(seeded.tenant_id)
    assert _OTHER_REALM_NAME not in trace
    assert f"{_KC_BASE_URL}{_REALM_PATH}" in trace


@pytest.mark.usefixtures("recorded")
@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
async def test_a_realm_read_with_no_redirect_keeps_its_full_record(
    seeded: Operator,  # noqa: F811
) -> None:
    answer = {"realm": _MANAGED, "displayName": "shown-in-the-trace"}
    result = await _call(seeded, "keycloak.realm.get", _Keycloak(answer))
    assert result.status == "ok", result.error
    trace = await _trace_text(seeded.tenant_id)
    assert "shown-in-the-trace" in trace
    assert f"GET {_KC_BASE_URL}{_REALM_PATH}" in trace


# ---------------------------------------------------------------------------
# A run after approval whose rebuilt card reads another realm
# ---------------------------------------------------------------------------


class _LaterRealm(_Keycloak):
    """The managed realm read answers with the managed realm until *later* is set."""

    def __init__(self) -> None:
        super().__init__({"realm": _MANAGED})
        self.later: Any = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == _REALM_PATH:
            self.calls.append(f"GET {request.url.path}")
            return httpx.Response(200, json=self.later or self.answer)
        if self.later is not None and request.method == "GET":
            self.calls.append(f"GET {request.url.path}")
            return httpx.Response(500)
        return super().__call__(request)


async def _resume_refusal(seeded: Operator, op_id: str, keycloak: _Keycloak) -> Any:  # noqa: F811
    (request,) = await _approval_rows()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        return await refuse_resume_if_preview_changed(
            seeded, request, await _resolved_target(seeded), dict(_PARAMS[op_id])
        )


@_CARDS
@pytest.mark.parametrize(
    "later",
    [
        {"realm": "made-up-other", "displayName": _ANSWER_MARKER},
        {"realm": _MANAGED.upper(), "displayName": _ANSWER_MARKER},
    ],
    ids=["other-name", "other-case"],
)
@pytest.mark.parametrize("op_id", ["keycloak.client.create", "keycloak.group.member.add"])
async def test_a_rebuilt_card_of_another_realm_says_to_check_the_managed_realm(
    seeded: Operator,  # noqa: F811
    op_id: str,
    later: dict[str, Any],
) -> None:
    keycloak = _LaterRealm()
    waiting = await _call(seeded, op_id, keycloak)
    assert waiting.extras.get("error_code") == "awaiting_approval", waiting.error
    keycloak.calls.clear()
    keycloak.later = later
    refusal = await _resume_refusal(seeded, op_id, keycloak)
    assert refusal is not None
    assert refusal.extras["error_code"] == "object_changed_since_approval"
    error = refusal.error or ""
    assert f"Keycloak did not answer with the realm {_MANAGED!r}" in error
    assert "'managed_realm' of the target" in error
    assert "The operation did not run." in error
    assert "could not read the login or group" not in error
    dumped = json.dumps(refusal.model_dump(mode="json"), default=str)
    assert _ANSWER_MARKER not in dumped
    assert later["realm"] not in dumped.replace(repr(_MANAGED), "")
    # Nothing was sent after the read that answered with another realm.
    assert keycloak.calls[-1] == f"GET {_REALM_PATH}"


@_CARDS
async def test_a_rebuilt_card_that_cannot_be_read_keeps_its_text(
    seeded: Operator,  # noqa: F811
) -> None:
    op_id = "keycloak.group.member.add"
    keycloak = _LaterRealm()
    waiting = await _call(seeded, op_id, keycloak)
    assert waiting.extras.get("error_code") == "awaiting_approval", waiting.error
    # The realm answers right; every other read of the card answers HTTP 500.
    keycloak.later = {"realm": _MANAGED}
    refusal = await _resume_refusal(seeded, op_id, keycloak)
    assert refusal is not None
    assert refusal.extras["error_code"] == "object_changed_since_approval"
    assert "did not answer with the realm" not in (refusal.error or "")
