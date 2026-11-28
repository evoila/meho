# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op class ``credential_use``: a call that carries or sends a secret.

A connector declares ``credential_use`` for an operation whose request
carries a secret, or that sends a stored secret to the target, but that
writes and makes no credential (for example a read inside a VM guest that
logs in with the stored guest password).

* **Hiding** -- the broadcast shows only an aggregate (also with a "full"
  request override or tenant rule), the flight recorder records no body,
  the vendor's error text is not kept, the reducer copies no scalars, and
  a waiting call keeps its params only in the one-time store.
* **Who waits** (whole path through ``dispatch``, ``approve_request`` and
  ``resume_dispatch_after_approval``, no approval mark on the row):

  - a person in the web console runs ``safe`` and ``caution`` at once;
  - a person with a token runs ``safe`` at once, and ``caution`` waits;
  - an agent waits, also with an ``auto-execute`` grant;
  - a service login waits, with a standing grant and with a run approval.

  After an Admin approves in the web console, the call runs once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from meho_backplane.api.v1.approvals import _view as _approval_view
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.announce_gate import (
    announce_gate_blocks,
    reset_announce_gate_cache_for_testing,
)
from meho_backplane.broadcast.events import classify_op, redact_payload, resolve_op_class
from meho_backplane.broadcast.history import WRITE_OP_CLASSES as BROADCAST_WRITE_OP_CLASSES
from meho_backplane.broadcast.overrides import compute_effective_broadcast_detail
from meho_backplane.broadcast.reflex import REFLEX_ADVISORY_EXTRAS_KEY, build_reflex_advisory
from meho_backplane.broadcast.target_advisory import (
    ADVISORY_EXTRAS_KEY,
    build_target_activity_advisory,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    RunApproval,
    ServicePrincipalGrant,
    Target,
    Tenant,
)
from meho_backplane.op_classes import (
    _DECLARED,
    CREDENTIAL_CHANGE_OP_CLASSES,
    CREDENTIAL_OP_CLASSES,
    DECLARABLE_OP_CLASSES,
    SECRET_REQUEST_OP_CLASSES,
    declare_op_classes,
)
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import agent_session_id_var, work_ref_var
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._request_preview import _ok_ingested_envelope
from meho_backplane.operations._vendor_text import sends_secret
from meho_backplane.operations.approval_context import _resolve_subject
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.composite_steps import STEP_DONE_NO_RESULT, StepJournal
from meho_backplane.operations.jsonflux_reducer import _is_credential_class
from meho_backplane.operations.run_approval_notes import _wait_reason
from meho_backplane.operations.service_grants import (
    CREDENTIAL_USE_CARD_LINE,
    credential_call_waits,
    credential_grant_reason,
    credential_refusal,
    descriptor_credential_card_line,
)
from meho_backplane.redaction.flight_recorder.families import classify_body_exclusion
from meho_backplane.reflex.adoption import WRITE_OP_CLASSES as ADOPTION_WRITE_OP_CLASSES
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._secret_class_standins import (  # noqa: F401 -- standin_ops is a fixture
    NOTE,
    STANDIN_OPS,
    made_up_value,
    standin_ops,
)
from tests.test_ui_approvals import _RESOLVE_OPERATOR as _RESOLVE_UI_OPERATOR
from tests.test_ui_approvals import _build_app as _build_console_app

from .broadcast_admission_helpers import seed_announcement

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c05e")
_PRODUCT = "creduse"
_CONNECTOR = "creduse-1.x"
_SAFE_OP = "creduse.guest.process.list"
_CAUTION_OP = "creduse.guest.file.read"
_SERVICE = "svc:creduse"
_AGENT = "agent:creduse"
_RUN = "run-creduse"
_SECRET = "made-up-guest-value-7"
_PARAMS: dict[str, Any] = {"path": "/tmp/notes.txt", "guest_password": _SECRET}

_RUNS: list[str] = []


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _declared() -> Iterator[None]:
    """Declare the two stand-in operations as ``credential_use``, as a connector would."""
    declare_op_classes(credential_use=(_SAFE_OP, _CAUTION_OP))
    try:
        yield
    finally:
        _DECLARED.pop(_SAFE_OP, None)
        _DECLARED.pop(_CAUTION_OP, None)


# ---------------------------------------------------------------------------
# The class and its sets
# ---------------------------------------------------------------------------


def test_the_class_is_declarable_and_in_the_hiding_sets_only() -> None:
    assert "credential_use" in DECLARABLE_OP_CLASSES
    assert "credential_use" in CREDENTIAL_OP_CLASSES
    assert "credential_use" in SECRET_REQUEST_OP_CLASSES
    assert "credential_use" not in CREDENTIAL_CHANGE_OP_CLASSES


def test_a_second_different_class_for_the_same_op_id_fails() -> None:
    with pytest.raises(ValueError, match="already declared"):
        declare_op_classes(credential_write=(_SAFE_OP,))


def test_the_declared_class_wins_and_a_route_class_never_replaces_it() -> None:
    assert classify_op(_SAFE_OP) == "credential_use"
    assert resolve_op_class(_SAFE_OP, "read") == "credential_use"


# ---------------------------------------------------------------------------
# Hiding
# ---------------------------------------------------------------------------


def test_the_broadcast_shows_only_an_aggregate() -> None:
    payload = redact_payload("credential_use", dict(_PARAMS), "ok")

    assert payload == {"op_class": "credential_use", "result_status": "ok"}


@pytest.mark.parametrize("op_id", [_SAFE_OP, _CAUTION_OP])
async def test_a_full_request_override_keeps_the_aggregate(op_id: str) -> None:
    op_class, detail, _origin = await compute_effective_broadcast_detail(
        op_id=op_id,
        tenant_id=_TENANT,
        raw_params=dict(_PARAMS),
        request_override="full",
    )

    assert (op_class, detail) == ("credential_use", "aggregate")


def test_the_flight_recorder_records_no_body() -> None:
    exclusion = classify_body_exclusion(_CAUTION_OP)

    assert exclusion.excluded is True
    assert exclusion.family == "secret-bearing"


def test_the_vendor_error_text_is_not_kept_and_the_reducer_copies_no_scalars() -> None:
    assert sends_secret(_CAUTION_OP, ()) is True
    assert _is_credential_class({"op_id": _CAUTION_OP}) is True


def test_no_grant_covers_the_call() -> None:
    assert credential_call_waits(_SAFE_OP) is True
    assert credential_grant_reason(_SAFE_OP) is not None


# ---------------------------------------------------------------------------
# The texts say that the call uses a credential
# ---------------------------------------------------------------------------

_OLD_GRANT_TEXT = (
    "A standing grant, an agent grant or a run approval never covers 'vault.kv.put': "
    "it reads, writes or makes a credential, so a person approves each call. Leave it out."
)
_OLD_CARD_LINE = (
    "This call waits for a person every time: it can read, write or make a credential, "
    "so no standing grant, agent grant or run approval covers it."
)
_OLD_RUN_NOTE = (
    "op 'vault.kv.put' reads, writes or makes a credential. A run approval never "
    "covers it: each call waits for a person"
)


def test_the_grant_refusal_says_the_call_uses_a_credential() -> None:
    assert credential_refusal(_SAFE_OP) == (
        f"A standing grant, an agent grant or a run approval never covers {_SAFE_OP!r}: "
        "it uses a credential, so a person approves each call. Leave it out."
    )
    assert credential_refusal("vault.kv.put") == _OLD_GRANT_TEXT


def test_the_approval_card_line_says_the_call_uses_a_credential() -> None:
    use = SimpleNamespace(op_id=_SAFE_OP, op_class=None, credential_call=False)
    write = SimpleNamespace(op_id="vault.kv.put", op_class=None, credential_call=False)

    assert descriptor_credential_card_line(use) == (  # type: ignore[arg-type]
        "This call waits for a person every time: it uses a credential, "
        "so no standing grant, agent grant or run approval covers it."
    )
    assert descriptor_credential_card_line(write) == _OLD_CARD_LINE  # type: ignore[arg-type]


def test_the_run_approval_note_says_the_call_uses_a_credential() -> None:
    assert _wait_reason(_SAFE_OP, None) == (
        f"op {_SAFE_OP!r} uses a credential. A run approval never covers it: "
        "each call waits for a person"
    )
    assert _wait_reason("vault.kv.put", None) == _OLD_RUN_NOTE


# ---------------------------------------------------------------------------
# Not a write: the announce gate and the two advisories after a run
# ---------------------------------------------------------------------------

_PEER_AGENT = "agent:creduse-peer"
_CLAIMED_TARGET = "creduse-target"


def test_the_class_is_in_neither_write_set() -> None:
    assert "credential_use" not in BROADCAST_WRITE_OP_CLASSES
    assert "credential_use" not in ADOPTION_WRITE_OP_CLASSES
    assert "credential_write" in BROADCAST_WRITE_OP_CLASSES
    assert "credential_write" in ADOPTION_WRITE_OP_CLASSES


@pytest_asyncio.fixture
async def _gate_on_and_peer_claim(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The tenant turned the announce gate on; another agent claims the target."""
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "30")
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(Tenant).where(Tenant.id == _TENANT).values(announce_gate_enabled=True)
        )
    reset_announce_gate_cache_for_testing()
    await seed_announcement(
        _TENANT,
        principal_sub=_PEER_AGENT,
        activity="working on this target",
        scope="all nodes",
        target=_CLAIMED_TARGET,
        ttl_minutes=60,
        ts=datetime.now(UTC) - timedelta(minutes=1),
    )
    yield
    reset_announce_gate_cache_for_testing()


@pytest.mark.usefixtures("_gate_on_and_peer_claim")
@pytest.mark.parametrize(
    ("op_class", "counts"), [("credential_use", False), ("credential_write", True)]
)
async def test_only_a_write_class_meets_the_announce_gate_and_the_advisories(
    op_class: str, counts: bool
) -> None:
    agent = _operator(_AGENT, kind=PrincipalKind.AGENT)

    block = await announce_gate_blocks(
        agent, op_class=op_class, safety_level="caution", target_name=_CLAIMED_TARGET
    )
    activity = await build_target_activity_advisory(
        agent, op_class=op_class, target_name=_CLAIMED_TARGET
    )
    session_token = agent_session_id_var.set(uuid.uuid4())
    try:
        with (
            patch(
                "meho_backplane.broadcast.reflex._session_read_broadcast",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "meho_backplane.broadcast.reflex._may_announce", new=AsyncMock(return_value=True)
            ),
            patch("meho_backplane.broadcast.reflex._claim", new=AsyncMock(return_value=True)),
        ):
            reflex = await build_reflex_advisory(
                agent, op_class=op_class, target_name=_CLAIMED_TARGET
            )
    finally:
        agent_session_id_var.reset(session_token)

    assert (block is not None) is counts
    assert (ADVISORY_EXTRAS_KEY in activity) is counts
    assert (REFLEX_ADVISORY_EXTRAS_KEY in reflex) is counts
    if not counts:
        assert activity == {}
        assert reflex == {}


# ---------------------------------------------------------------------------
# Hiding, step by step: the same steps hide a credential write and a use
# ---------------------------------------------------------------------------


def _bare_operator() -> Operator:
    return Operator(
        sub="person-steps",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.usefixtures("standin_ops")
@pytest.mark.parametrize("op_class", sorted(STANDIN_OPS))
async def test_the_approval_card_echoes_no_params(op_class: str) -> None:
    value = made_up_value()
    ctx = PreviewContext(
        descriptor=SimpleNamespace(op_id=STANDIN_OPS[op_class], op_class=None),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_bare_operator(),
        target=None,
        connector_id="standin-1.x",
        params={"path": "/notes", NOTE: value},
    )

    assert await build_proposed_effect(ctx) is None


@pytest.mark.usefixtures("standin_ops")
@pytest.mark.parametrize("op_class", sorted(STANDIN_OPS))
def test_the_approval_context_echoes_no_subject(op_class: str) -> None:
    value = made_up_value()
    request = ApprovalRequest(
        tenant_id=_TENANT, op_id=STANDIN_OPS[op_class], params={"name": value, NOTE: value}
    )

    assert _resolve_subject(request) is None


@pytest.mark.usefixtures("standin_ops")
@pytest.mark.parametrize("op_class", sorted(STANDIN_OPS))
def test_the_request_preview_shows_no_secret_value_from_the_body_or_the_query(
    op_class: str,
) -> None:
    in_query = made_up_value()
    in_body = made_up_value()
    request = SimpleNamespace(
        method="POST",
        path="/notes/fetch",
        query={"guest_password": in_query, "page": "1"},
        body={"guest_password": in_body, "path": "/notes"},
    )

    envelope = _ok_ingested_envelope(
        operator=_bare_operator(),
        connector_id="standin-1.x",
        op_id=STANDIN_OPS[op_class],
        descriptor=SimpleNamespace(source_kind="openapi"),  # type: ignore[arg-type]
        request=request,
    )

    shown = repr(envelope)
    assert in_query not in shown
    assert in_body not in shown
    assert envelope["query"]["page"] == "1"
    assert envelope["redacted_body"]["path"] == "/notes"


@pytest.mark.usefixtures("standin_ops")
@pytest.mark.parametrize("op_class", sorted(STANDIN_OPS))
def test_a_finished_step_of_the_class_stores_no_result(op_class: str) -> None:
    value = made_up_value()
    op_id = STANDIN_OPS[op_class]
    journal = StepJournal(composite_op_id="standin.composite.run")
    entry = journal.start("standin-1.x", op_id, None, {"path": "/notes"})

    journal.finish(entry, {NOTE: value})

    assert entry["status"] == STEP_DONE_NO_RESULT
    assert "result" not in entry
    assert value not in repr(journal.entries)


# ---------------------------------------------------------------------------
# Who waits: the whole path
# ---------------------------------------------------------------------------


class _UseConnector(Connector):
    """Lets the resolver pick a connector; the typed handlers do the work."""

    product = _PRODUCT
    version = "1.x"
    impl_id = _PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        raise NotImplementedError


async def _safe_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _RUNS.append(_SAFE_OP)
    return {"read": True}


async def _caution_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _RUNS.append(_CAUTION_OP)
    return {"read": True}


@pytest_asyncio.fixture(autouse=True)
async def _registered(_settings_env: None) -> AsyncIterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_UseConnector)
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    for op_id, level, handler in (
        (_SAFE_OP, "safe", _safe_handler),
        (_CAUTION_OP, "caution", _caution_handler),
    ):
        await register_typed_operation(
            product=_PRODUCT,
            version="1.x",
            impl_id=_PRODUCT,
            op_id=op_id,
            handler=handler,
            summary="A read inside a guest that logs in with a stored password.",
            description="Stand-in operation.",
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "guest_password": {"type": "string"},
                },
            },
            when_to_use=None,
            safety_level=level,
            requires_approval=False,
            embedding_service=embedding,
        )
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT, slug="tenant-creduse", name="Tenant creduse"))
    yield
    reset_dispatcher_caches()
    clear_registry()


async def _target() -> Target:
    """A stored target, so an approved call can load it again when it resumes."""
    async with get_sessionmaker()() as session:
        target = Target(
            tenant_id=_TENANT,
            name="creduse-target",
            aliases=[],
            product=_PRODUCT,
            host="creduse.example",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "1.0"},
            notes="stand-in target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _operator(
    sub: str,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.OPERATOR,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
    )


async def _call(operator: Operator, op_id: str) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=op_id,
        target=await _target(),
        params=dict(_PARAMS),
    )


async def _web_person(op_id: str) -> Any:
    return await _call(_operator("person-web", web_session=True), op_id)


async def _token_person(op_id: str) -> Any:
    return await _call(_operator("person-token"), op_id)


async def _agent(op_id: str) -> Any:
    return await _call(_operator(_AGENT, kind=PrincipalKind.AGENT), op_id)


async def _agent_with_grant(op_id: str) -> Any:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern=op_id,
                target_scope="*",
                verdict="auto-execute",
                created_by_sub="admin-person",
            )
        )
    return await _agent(op_id)


def _service() -> Operator:
    return _operator(_SERVICE, kind=PrincipalKind.SERVICE, role=TenantRole.TENANT_ADMIN)


async def _service_with_grant(op_id: str) -> Any:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                op_id=op_id,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="test: a standing grant made earlier",
                created_by_sub="admin-person",
            )
        )
    return await _call(_service(), op_id)


async def _service_in_approved_run(op_id: str) -> Any:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                work_ref=_RUN,
                entries=[{"connector_id": _CONNECTOR, "op_id": op_id}],
                reason="test: a run approval made earlier",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="admin-person",
            )
        )
    token = work_ref_var.set(_RUN)
    try:
        return await _call(_service(), op_id)
    finally:
        work_ref_var.reset(token)


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.scalars())


@pytest.mark.parametrize(
    ("call", "op_id"),
    [
        (_web_person, _SAFE_OP),
        (_web_person, _CAUTION_OP),
        (_token_person, _SAFE_OP),
    ],
    ids=["web-person-safe", "web-person-caution", "token-person-safe"],
)
async def test_a_person_runs_the_call_at_once_by_its_risk_level(call: Any, op_id: str) -> None:
    result = await call(op_id)

    assert result.status == "ok", result
    assert [op_id] == _RUNS
    assert await _requests() == []
    rows = await _audit_rows(op_id)
    assert rows, "the call writes an audit row"
    assert {row.payload["op_class"] for row in rows} == {"credential_use"}
    assert _SECRET not in repr([row.payload for row in rows])


_WAITING_CALLS = [
    (_token_person, _CAUTION_OP),
    (_agent, _SAFE_OP),
    (_agent, _CAUTION_OP),
    (_agent_with_grant, _SAFE_OP),
    (_agent_with_grant, _CAUTION_OP),
    (_service_with_grant, _SAFE_OP),
    (_service_with_grant, _CAUTION_OP),
    (_service_in_approved_run, _SAFE_OP),
    (_service_in_approved_run, _CAUTION_OP),
]
_WAITING_IDS = [
    "token-person-caution",
    "agent-safe",
    "agent-caution",
    "agent-grant-safe",
    "agent-grant-caution",
    "service-standing-grant-safe",
    "service-standing-grant-caution",
    "service-run-approval-safe",
    "service-run-approval-caution",
]


@pytest.mark.parametrize(("call", "op_id"), _WAITING_CALLS, ids=_WAITING_IDS)
async def test_the_call_waits_keeps_its_params_hidden_and_runs_once_after_approval(
    call: Any, op_id: str
) -> None:
    result = await call(op_id)

    assert result.status == "awaiting_approval", result
    assert _RUNS == []
    (request,) = await _requests()
    assert request.op_id == op_id
    assert request.params is None
    assert _SECRET not in repr(request.proposed_effect)
    assert request.proposed_effect["credential_wait"] == CREDENTIAL_USE_CARD_LINE

    approver = _operator("admin-web", role=TenantRole.TENANT_ADMIN, web_session=True)
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request.id, operator=approver)
        await session.commit()
    ran = await resume_dispatch_after_approval(operator=approver, request=approved)

    assert ran.status == "ok", ran
    assert [op_id] == _RUNS
    rows = await _audit_rows(op_id)
    assert _SECRET not in repr([row.payload for row in rows])


# ---------------------------------------------------------------------------
# A plain step of a multi-step credential write or use waits
# ---------------------------------------------------------------------------

_USE_COMPOSITE = "creduse.composite.fetch_then_step"
_WRITE_COMPOSITE = "creduse.composite.send_then_step"
_PARENTS = {"credential_use": _USE_COMPOSITE, "credential_write": _WRITE_COMPOSITE}
_PLAIN_STEP = "POST:/notes/plain-step"

#: The parent params each run of a stand-in multi-step operation got.
_PARENT_PARAMS_SEEN: list[dict[str, Any]] = []
#: The plain steps that ran after their gate let them go ahead.
_STEPS_RAN: list[str] = []


async def _composite_with_a_plain_step(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """A multi-step operation whose one plain step waits for a person."""
    _PARENT_PARAMS_SEEN.append(dict(params))
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=_PLAIN_STEP,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"name": "notes"},
    )
    if gated is not None:
        return gated
    _STEPS_RAN.append(_PLAIN_STEP)
    return {"done": True}


@pytest_asyncio.fixture
async def _parent_composites() -> AsyncIterator[None]:
    """Declare and store one multi-step operation of each class."""
    declare_op_classes(credential_use=(_USE_COMPOSITE,), credential_write=(_WRITE_COMPOSITE,))
    _PARENT_PARAMS_SEEN.clear()
    _STEPS_RAN.clear()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        for op_id in _PARENTS.values():
            session.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product=_PRODUCT,
                    version="1.x",
                    impl_id=_PRODUCT,
                    op_id=op_id,
                    source_kind="composite",
                    method=None,
                    path=None,
                    handler_ref=f"{__name__}._composite_with_a_plain_step",
                    summary="A multi-step stand-in.",
                    description="A multi-step stand-in.",
                    tags=[],
                    parameter_schema={"type": "object"},
                    response_schema=None,
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                    embedding=[0.1] * 384,
                    custom_description=None,
                    custom_notes=None,
                    created_at=now,
                    updated_at=now,
                )
            )
    try:
        yield
    finally:
        for op_id in _PARENTS.values():
            _DECLARED.pop(op_id, None)


@pytest.mark.usefixtures("_parent_composites")
@pytest.mark.parametrize("op_class", sorted(_PARENTS))
async def test_a_waiting_plain_step_keeps_the_parent_params_off_the_row(op_class: str) -> None:
    value = made_up_value()
    parent_params = {"path": "/notes", NOTE: value}
    requester = _operator("person-web", web_session=True)

    result = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR,
        op_id=_PARENTS[op_class],
        target=await _target(),
        params=dict(parent_params),
    )

    assert result.status == "awaiting_approval", result
    assert _STEPS_RAN == []
    approver = _operator("admin-web", role=TenantRole.TENANT_ADMIN, web_session=True)
    approved_ids: set[uuid.UUID] = set()
    if op_class == "credential_write":
        # A call that writes a credential waits for a person first, so the
        # parent's own request is approved before the plain step's request exists.
        (parent_request,) = await _requests()
        assert parent_request.op_id == _PARENTS[op_class]
        async with get_sessionmaker()() as session:
            approved_parent = await approve_request(session, parent_request.id, operator=approver)
            await session.commit()
        approved_ids.add(parent_request.id)
        resumed = await resume_dispatch_after_approval(operator=approver, request=approved_parent)
        assert resumed.status == "awaiting_approval", resumed
        assert _STEPS_RAN == []
    (request,) = [r for r in await _requests() if r.id not in approved_ids]
    assert request.op_id == _PLAIN_STEP
    assert request.params is None
    assert request.resume_parent == {"op_id": _PARENTS[op_class]}
    assert request.execution_handle is not None
    stored = {
        column.name: getattr(request, column.name) for column in ApprovalRequest.__table__.columns
    }
    assert value not in repr(stored)
    assert value not in _approval_view(request, operator=requester).model_dump_json()

    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request.id, operator=approver)
        await session.commit()
    ran = await resume_dispatch_after_approval(operator=approver, request=approved)

    assert ran.status == "ok", ran
    assert _STEPS_RAN == [_PLAIN_STEP]
    assert _PARENT_PARAMS_SEEN[-1] == parent_params


# ---------------------------------------------------------------------------
# The approval card in the web console names what the call does
# ---------------------------------------------------------------------------

_USE_CARD_TEXT = "This call uses a credential; its values are intentionally"
_WRITE_CARD_TEXT = "This is a credential-class write; its values are intentionally"


@pytest.fixture
def _console_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The settings the web console needs to render a page."""
    monkeypatch.setenv("BACKPLANE_URL", "https://backplane.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_templating_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_templating_for_testing()


async def _card_of(request_id: uuid.UUID) -> str:
    """The approval card of *request_id*, as the web console renders it."""
    reviewer = _operator("admin-web", role=TenantRole.TENANT_ADMIN, web_session=True)
    async with get_sessionmaker()() as session, session.begin():
        web_session = await create_session(
            session,
            operator_sub=reviewer.sub,
            tenant_id=_TENANT,
            access_token="access-token-plaintext",
            refresh_token="refresh-token-plaintext",
            lifetime=timedelta(hours=1),
        )
    with patch(_RESOLVE_UI_OPERATOR, new_callable=AsyncMock, return_value=reviewer):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_build_console_app()),
            base_url="https://testserver",
            cookies={SESSION_COOKIE_NAME: str(web_session.id)},
        ) as client:
            response = await client.get(f"/ui/approvals/{request_id}")
    assert response.status_code == 200, response.text
    return response.text


@pytest.mark.usefixtures("_console_env")
@pytest.mark.parametrize(
    ("op_class", "shown", "not_shown"),
    [
        ("credential_use", _USE_CARD_TEXT, "credential-class write"),
        ("credential_write", _WRITE_CARD_TEXT, "This call uses a credential"),
    ],
    ids=["credential_use", "credential_write"],
)
async def test_the_approval_card_names_what_a_waiting_call_of_the_class_does(
    op_class: str, shown: str, not_shown: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_DECLARED, _SAFE_OP, op_class)

    result = await _agent(_SAFE_OP)

    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    card = " ".join((await _card_of(request.id)).split())
    assert shown in card
    assert not_shown not in card
    assert _SECRET not in card
