# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every place that reads the class of an operation reads the class stored on the row.

A connector's safety floor can store a class on an ingested operation. The op
id of such an operation says only ``read`` or ``write``. Each test here uses
an operation whose class is only stored, and checks one place that used to
read the op id alone:

* the subject and the preview subject of the approval card, the stored
  parameters of the approval request, the vendor error text, the guidance
  stamp of a run, the policy file check, the result reducer, the console
  drawer class, the standing-grant checks;
* the write notes of a stored ``credential_read`` on an operation that also
  changes something;
* the same class on the request row and on the run row of a typed operation;
* a connector floor that sets ``credential_write`` or ``credential_mint``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatcher, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    register_preview_builder,
)
from meho_backplane.operations._vendor_text import withhold_vendor_text
from meho_backplane.operations.approval_context import _preview_subject, _resolve_subject
from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload
from meho_backplane.operations.approval_outcome import with_approval_guidance
from meho_backplane.operations.approved_reveal import is_write_reveal
from meho_backplane.operations.ingest.safety_floors import _FLOORS
from meho_backplane.operations.jsonflux_reducer import _is_credential_class
from meho_backplane.operations.op_class_of import (
    descriptor_op_class,
    request_op_class,
    write_note_op_class,
)
from meho_backplane.operations.reducer import SECRET_REVEAL_CONTEXT_KEY
from meho_backplane.operations.service_grants import (
    consult_and_record_grant,
    credential_write_grant_reason,
)
from meho_backplane.policy.validate import load_known_ops
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.broadcast.aggregate_gate import resolve_row_op_class
from tests._stored_op_class_harness import (
    GET_CLIENTS,
    MADE_UP_VALUE,
    POST_CLIENTS,
    Clients,
    call,
    ingest,
    operator,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    Clients.calls = []
    Clients.answer = [{"name": "a"}]
    Clients.failure = None
    yield
    _FLOORS.clear()
    reset_dispatcher_caches()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


async def _audit_payloads(op_id: str) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [dict(row.payload) for row in rows if row.payload.get("op_id") == op_id]


async def _parked_request(
    service: AsyncMock, stored: str | None, params: dict[str, Any]
) -> ApprovalRequest:
    """Ingest with *stored* on ``GET /clients``, call it, and return the parked request."""
    await ingest(service, {GET_CLIENTS: stored} if stored else {}, approval=True)
    result = await call(operator(), GET_CLIENTS, params)
    assert result.status == "awaiting_approval", (result.status, result.error)
    (request,) = await _requests()
    return request


# -- the approval card: subject, preview subject, guidance, reveal --------------------


@pytest.mark.asyncio
async def test_approval_card_subject_is_not_echoed_for_a_stored_credential_read(
    stub_embedding_service: AsyncMock,
) -> None:
    request = await _parked_request(stub_embedding_service, "credential_read", {"name": "web-01"})

    assert request.proposed_effect["op_class"] == "credential_read"
    assert _resolve_subject(request) is None
    request.proposed_effect = {**request.proposed_effect, "preview": {"subject": "web-01"}}
    assert _preview_subject(request) is None


@pytest.mark.asyncio
async def test_approval_card_subject_is_shown_when_no_class_is_stored(
    stub_embedding_service: AsyncMock,
) -> None:
    """The control: the same call with no stored class shows its subject."""
    request = await _parked_request(stub_embedding_service, None, {"name": "web-01"})

    assert _resolve_subject(request) == "web-01"
    request.proposed_effect = {**request.proposed_effect, "preview": {"subject": "web-01"}}
    assert _preview_subject(request) == "web-01"


@pytest.mark.asyncio
async def test_run_guidance_is_not_stamped_for_a_stored_credential_read(
    stub_embedding_service: AsyncMock,
) -> None:
    request = await _parked_request(stub_embedding_service, "credential_read", {"name": "x"})
    envelope = {
        "status": "error",
        "op_id": GET_CLIENTS,
        "extras": {"error_code": "step_failed"},
        "result": {"status": "step_failed", "guidance": "Run the next step by hand."},
    }

    stamped = with_approval_guidance(
        envelope,
        parked_op_id=request.op_id,
        parked_op_class=request_op_class(request.op_id, request.proposed_effect),
    )
    control = with_approval_guidance(envelope, parked_op_id=request.op_id)

    assert "approval_guidance" not in stamped["extras"]
    assert control["extras"]["approval_guidance"] == "Run the next step by hand."


@pytest.mark.asyncio
async def test_stored_write_class_is_a_change_for_the_reveal_check(
    stub_embedding_service: AsyncMock,
) -> None:
    """A reveal of an operation with a stored ``write`` is a change; ``read`` is not."""
    request = await _parked_request(stub_embedding_service, "write", {})
    request.reveal_secret = True

    assert request_op_class(request.op_id, request.proposed_effect) == "write"
    assert is_write_reveal(request) is True
    request.proposed_effect = {**request.proposed_effect, "op_class": "read"}
    assert is_write_reveal(request) is False


# -- the stored parameters of an approval request ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "plain"), [("credential_write", False), (None, True)])
async def test_request_params_are_not_stored_in_plain_for_a_stored_credential_write(
    stub_embedding_service: AsyncMock, stored: str | None, plain: bool
) -> None:
    request = await _parked_request(stub_embedding_service, stored, {"q": MADE_UP_VALUE})

    assert (request.params == {"q": MADE_UP_VALUE}) is plain
    async with get_sessionmaker()() as session:
        payloads = (await session.execute(select(ApprovalExecutionPayload))).scalars().all()
    assert (len(payloads) == 0) is plain
    if not plain:
        assert MADE_UP_VALUE not in json.dumps(request.proposed_effect, default=str)
        assert request.proposed_effect["op_class"] == "credential_write"


# -- the vendor error text -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "kept"), [("credential_write", False), (None, True)])
async def test_vendor_error_text_is_not_kept_for_a_stored_credential_write(
    stub_embedding_service: AsyncMock, stored: str | None, kept: bool
) -> None:
    await ingest(stub_embedding_service, {GET_CLIENTS: stored} if stored else {})
    Clients.failure = RuntimeError(f"vendor said {MADE_UP_VALUE}")

    result = await call(operator(), GET_CLIENTS, {}, approved=True)

    assert result.status == "error", (result.status, result.error)
    assert (MADE_UP_VALUE in repr(result.model_dump())) is kept
    stored_text = json.dumps(await _audit_payloads(GET_CLIENTS), default=str)
    assert (MADE_UP_VALUE in stored_text) is kept


def test_withhold_vendor_text_reads_the_class_it_is_given() -> None:
    built = OperationResult(
        status="error",
        op_id=GET_CLIENTS,
        error="the vendor refused the call",
        duration_ms=1.0,
        extras={"upstream_message": MADE_UP_VALUE},
    )

    assert withhold_vendor_text(built, op_id=GET_CLIENTS, tags=None) is built
    out = withhold_vendor_text(built, op_id=GET_CLIENTS, tags=None, op_class="credential_write")
    assert MADE_UP_VALUE not in repr(out.model_dump())


# -- the policy file check ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "shows"), [("credential_read", True), (None, False)])
async def test_policy_file_check_knows_a_stored_credential_read(
    stub_embedding_service: AsyncMock, stored: str | None, shows: bool
) -> None:
    await ingest(stub_embedding_service, {GET_CLIENTS: stored} if stored else {})

    async with get_sessionmaker()() as session:
        known = await load_known_ops(session, scope="install", tenant_id=None)

    by_op = {
        op.coords.op_id: op for op in known if op.coords.connector_id.startswith("storedclass")
    }
    assert by_op[GET_CLIENTS].shows_secret is shows
    assert by_op[POST_CLIENTS].shows_secret is False


# -- the result reducer ---------------------------------------------------------------


def test_reducer_reads_the_class_in_its_context() -> None:
    assert _is_credential_class({"op_id": GET_CLIENTS}) is False
    assert _is_credential_class({"op_id": GET_CLIENTS, "op_class": "read"}) is False
    for stored in ("credential_read", "credential_write", "credential_mint"):
        assert _is_credential_class({"op_id": GET_CLIENTS, "op_class": stored}) is True


# -- the console drawers ---------------------------------------------------------------


def test_drawer_class_reads_the_secret_class_the_audit_row_stored() -> None:
    def row(stored: str | None) -> Any:
        return SimpleNamespace(payload={} if stored is None else {"op_class": stored})

    assert resolve_row_op_class(row(None), GET_CLIENTS) == "read"
    assert resolve_row_op_class(row("credential_read"), GET_CLIENTS) == "credential_read"
    assert resolve_row_op_class(row("credential_write"), GET_CLIENTS) == "credential_write"
    # A route-bound class that is not a secret class does not change what the op id shows.
    assert resolve_row_op_class(row("write"), GET_CLIENTS) == "read"
    assert resolve_row_op_class(row("read"), "approval.pending") == "approval"


# -- the standing-grant checks ---------------------------------------------------------


def test_credential_write_needs_a_preview_builder_before_a_grant_can_cover_it() -> None:
    op_id = "GET:/clients-with-builder"
    assert credential_write_grant_reason(GET_CLIENTS) is None
    assert credential_write_grant_reason(GET_CLIENTS, "write") is None
    assert credential_write_grant_reason(GET_CLIENTS, "credential_write") is not None

    async def builder(ctx: PreviewContext) -> dict[str, Any] | None:
        return {}

    register_preview_builder(op_id, builder)

    assert credential_write_grant_reason(op_id, "credential_write") is None


@pytest.mark.asyncio
async def test_grant_row_never_clears_a_stored_credential_write_with_no_builder(
    stub_embedding_service: AsyncMock,
) -> None:
    rows = await ingest(stub_embedding_service, {GET_CLIENTS: "credential_write"}, approval=True)
    who = operator(PrincipalKind.SERVICE)

    granted = await consult_and_record_grant(
        operator=who,
        descriptor=rows[GET_CLIENTS],
        target=None,
        connector_id="storedclass-rest-1.0",
        record=False,
    )

    assert granted is None


# -- the write notes of a stored credential_read on an operation that changes something --


def _descriptor(op_id: str, stored: str | None, source_kind: str = "ingested") -> Any:
    return SimpleNamespace(
        op_id=op_id, source_kind=source_kind, safety_level="caution", op_class=stored
    )


def test_stored_credential_read_on_a_post_keeps_the_write_notes() -> None:
    post = _descriptor(POST_CLIENTS, "credential_read")
    get = _descriptor(GET_CLIENTS, "credential_read")

    assert descriptor_op_class(post) == "credential_read"
    assert write_note_op_class(post) == "write"
    assert descriptor_op_class(get) == "credential_read"
    assert write_note_op_class(get) == "credential_read"
    assert write_note_op_class(_descriptor(POST_CLIENTS, None)) == "write"
    assert write_note_op_class(_descriptor(POST_CLIENTS, "credential_write")) == "credential_write"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "note_class"), [(POST_CLIENTS, "write"), (GET_CLIENTS, "credential_read")]
)
async def test_approved_call_of_a_stored_credential_read_gets_the_notes_of_its_method(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    note_class: str,
) -> None:
    """The run row says ``credential_read``; a ``POST`` still gets the write notes."""
    await ingest(
        stub_embedding_service, {GET_CLIENTS: "credential_read", POST_CLIENTS: "credential_read"}
    )
    seen: list[str] = []

    async def activity(operator: Any, *, op_class: str, target_name: Any) -> dict[str, Any]:
        seen.append(op_class)
        return {}

    monkeypatch.setattr(dispatcher, "build_target_activity_advisory", activity)

    result = await call(operator(), op_id, {}, approved=True)

    assert result.status == "ok", (result.status, result.error)
    assert seen == [note_class]
    # The written run row of the approved call says what the row stores.
    assert {payload["op_class"] for payload in await _audit_payloads(op_id)} == {"credential_read"}


# -- the same class on the request row and on the run row ------------------------------


@pytest.mark.asyncio
async def test_request_row_and_run_row_of_a_typed_operation_have_the_same_class() -> None:
    descriptor = SimpleNamespace(
        op_id="widget.thing.restart",
        source_kind="typed",
        safety_level="caution",
        op_class=None,
        requires_approval=True,
        parameter_schema={},
        response_schema=None,
        tags=[],
    )
    ctx = PreviewContext(
        descriptor=descriptor,  # type: ignore[arg-type]
        connector_instance=None,
        operator=operator(),
        target=None,
        params={"name": "w-1"},
    )

    effect = await build_proposed_effect(ctx)

    assert effect is not None
    assert effect["op_class"] == descriptor_op_class(descriptor) == "write"


# -- a connector floor that sets credential_write or credential_mint ----------------------


@pytest.mark.asyncio
async def test_floor_may_set_credential_write_and_credential_mint(
    stub_embedding_service: AsyncMock,
) -> None:
    rows = await ingest(
        stub_embedding_service, {GET_CLIENTS: "credential_write", POST_CLIENTS: "credential_mint"}
    )

    assert rows[GET_CLIENTS].op_class == "credential_write"
    assert rows[POST_CLIENTS].op_class == "credential_mint"


@pytest.mark.asyncio
async def test_stored_credential_mint_is_at_least_caution_and_waits_for_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    rows = await ingest(stub_embedding_service, {GET_CLIENTS: "credential_mint"})
    assert rows[GET_CLIENTS].safety_level == "caution"
    assert rows[GET_CLIENTS].requires_approval is True

    result = await call(operator(), GET_CLIENTS, {})

    assert result.status == "awaiting_approval", (result.status, result.error)
    assert Clients.calls == []
    (request,) = await _requests()
    assert request.proposed_effect["op_class"] == "credential_mint"


class _RecordingReducer:
    """A reducer that keeps the context it is given and returns the answer whole."""

    contexts: ClassVar[list[dict[str, Any]]] = []

    async def reduce(self, payload: Any, schema: Any, context: Any = None) -> tuple[Any, None]:
        type(self).contexts.append(dict(context or {}))
        return payload, None


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "kept_nowhere"), [("credential_mint", True), (None, False)])
async def test_reducer_keeps_no_copy_of_the_answer_of_a_stored_credential_mint(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    stored: str | None,
    kept_nowhere: bool,
) -> None:
    """The run tells the reducer to store nothing: the result store holds no clear value."""
    await ingest(stub_embedding_service, {GET_CLIENTS: stored} if stored else {})
    Clients.answer = [{"name": "client-1", "token": MADE_UP_VALUE}]
    _RecordingReducer.contexts = []
    monkeypatch.setattr(dispatcher, "_DEFAULT_REDUCER", _RecordingReducer())

    result = await call(operator(), GET_CLIENTS, {}, approved=True)

    assert result.status == "ok", (result.status, result.error)
    (context,) = _RecordingReducer.contexts
    assert (context.get(SECRET_REVEAL_CONTEXT_KEY) is True) is kept_nowhere
    assert context["op_class"] == (stored or "read")
    stored_text = json.dumps(await _audit_payloads(GET_CLIENTS), default=str)
    assert MADE_UP_VALUE not in stored_text


@pytest.mark.asyncio
async def test_reingest_never_replaces_a_stored_secret_class(
    stub_embedding_service: AsyncMock,
) -> None:
    await ingest(stub_embedding_service, {GET_CLIENTS: "credential_mint"})

    rows = await ingest(stub_embedding_service, {GET_CLIENTS: "write"})

    assert rows[GET_CLIENTS].op_class == "credential_mint"
