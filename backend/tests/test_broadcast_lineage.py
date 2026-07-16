# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Lineage projection onto ``BroadcastEvent`` (T3 #2545).

Proves the real-time broadcast carries the same actor / agent-session /
work_ref lineage the durable ``audit_log`` row records for the same
operation, so a feed reader can tell a delegated agent's work
(``actor_sub`` = agent, ``principal_sub`` = human) from a human's own,
group a run's operations by ``agent_session_id``, and join work to an
external ticket by ``work_ref``. The audit-side twin of this projection is
covered by ``test_audit_actor_sub.py`` / ``test_audit_work_ref.py`` (the
``#2086`` lineage-gap precedent).

Four contracts:

* the model gains three optional lineage fields that default to ``None``
  (pre-v2 stream entries that predate them still validate);
* :func:`resolve_broadcast_lineage` reads them off the publish-site
  contextvars, mirroring :func:`~meho_backplane.operations._audit.write_audit_row`;
* :func:`publish_broadcast` projects them onto the emitted event under a
  delegated agent run;
* :func:`~meho_backplane.broadcast.history.event_matches` filters on
  ``actor_sub`` / ``work_ref``;

plus a grep-pinned invariant: every ``BroadcastEvent`` construction site
in the package supplies the lineage kwargs, so a delegated agent never
broadcasts as the human on any surface.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import UTC, datetime

import pytest
import structlog

import meho_backplane
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.history import event_matches
from meho_backplane.operations import _audit as audit_module
from meho_backplane.operations._audit import (
    agent_session_id_var,
    publish_broadcast,
    resolve_broadcast_lineage,
    work_ref_var,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000025a5")
_TS = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
_SESSION = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _make_event(**overrides: object) -> BroadcastEvent:
    base: dict[str, object] = {
        "event_id": uuid.uuid4(),
        "ts": _TS,
        "tenant_id": _TENANT,
        "principal_sub": "user-alice",
        "op_id": "vsphere.vm.list",
        "op_class": "read",
        "result_status": "ok",
        "audit_id": uuid.uuid4(),
    }
    base.update(overrides)
    return BroadcastEvent(**base)


# ---------------------------------------------------------------------------
# Model — optional lineage fields, defaults, back-compat
# ---------------------------------------------------------------------------


class TestBroadcastEventLineageFields:
    def test_lineage_defaults_to_none_when_omitted(self) -> None:
        event = _make_event()
        assert event.actor_sub is None
        assert event.agent_session_id is None
        assert event.work_ref is None

    def test_lineage_fields_accept_values(self) -> None:
        event = _make_event(
            actor_sub="agent:incident-triage",
            agent_session_id=_SESSION,
            work_ref="gh:evoila/meho#2545",
        )
        assert event.actor_sub == "agent:incident-triage"
        assert event.agent_session_id == _SESSION
        assert event.work_ref == "gh:evoila/meho#2545"

    def test_pre_v2_entry_without_lineage_still_validates(self) -> None:
        """A stream entry written before T3 lacks the fields; it must parse.

        Mixed-stream back-compat: the publisher's ``MAXLEN ~`` trim keeps
        pre-T3 entries live for a while, and the frozen model deserialises
        them on read. Absent fields resolve to ``None`` via the defaults —
        no ``ValidationError``.
        """
        pre_v2_json = (
            f'{{"kind": "operation", "event_id": "{uuid.uuid4()}", '
            f'"ts": "2026-07-01T09:00:00Z", "tenant_id": "{_TENANT}", '
            f'"principal_sub": "user-bob", "op_id": "vsphere.vm.list", '
            f'"op_class": "read", "result_status": "ok", '
            f'"audit_id": "{uuid.uuid4()}", "payload": {{}}}}'
        )
        event = BroadcastEvent.model_validate_json(pre_v2_json)
        assert event.actor_sub is None
        assert event.agent_session_id is None
        assert event.work_ref is None

    def test_lineage_survives_json_round_trip_on_the_wire(self) -> None:
        original = _make_event(
            actor_sub="agent:triage",
            agent_session_id=_SESSION,
            work_ref="JIRA-42",
        )
        rebuilt = BroadcastEvent.model_validate_json(original.model_dump_json())
        assert rebuilt.actor_sub == "agent:triage"
        assert rebuilt.agent_session_id == _SESSION
        assert rebuilt.work_ref == "JIRA-42"
        # UUID serialises as a string on the wire (consumers read it back).
        dumped = original.model_dump(mode="json")
        assert dumped["agent_session_id"] == str(_SESSION)


# ---------------------------------------------------------------------------
# resolve_broadcast_lineage — reads the publish-site contextvars
# ---------------------------------------------------------------------------


class TestResolveBroadcastLineage:
    def test_unbound_context_returns_all_none(self) -> None:
        structlog.contextvars.clear_contextvars()
        lineage = resolve_broadcast_lineage()
        assert lineage.actor_sub is None
        assert lineage.agent_session_id is None
        assert lineage.work_ref is None

    def test_bound_context_returns_the_bound_values(self) -> None:
        structlog.contextvars.clear_contextvars()
        session_token = agent_session_id_var.set(_SESSION)
        work_token = work_ref_var.set("gh:evoila/meho#2545")
        try:
            with actor_delegation("agent:incident-triage"):
                lineage = resolve_broadcast_lineage()
        finally:
            agent_session_id_var.reset(session_token)
            work_ref_var.reset(work_token)
        assert lineage.actor_sub == "agent:incident-triage"
        assert lineage.agent_session_id == _SESSION
        assert lineage.work_ref == "gh:evoila/meho#2545"


# ---------------------------------------------------------------------------
# publish_broadcast — projects lineage onto the emitted event
# ---------------------------------------------------------------------------


class _Descriptor:
    """Duck-typed stand-in — ``publish_broadcast`` reads ``op_id`` only."""

    def __init__(self, op_id: str) -> None:
        self.op_id = op_id


def _make_operator() -> Operator:
    return Operator(
        sub="user-alice",
        name="Alice Human",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


async def _publish(captured_events: list[BroadcastEvent]) -> BroadcastEvent:
    await publish_broadcast(
        audit_id=uuid.uuid4(),
        operator=_make_operator(),
        descriptor=_Descriptor("vsphere.vm.list"),
        target=None,
        params={"folder": "prod"},
        result_status="ok",
    )
    assert len(captured_events) == 1
    return captured_events[0]


class TestPublishBroadcastProjectsLineage:
    async def test_delegated_agent_broadcasts_as_agent_not_human(
        self,
        captured_events: list[BroadcastEvent],
    ) -> None:
        """The keystone: a delegated agent's op is attributable to the agent.

        ``principal_sub`` stays the delegating human; ``actor_sub`` is the
        agent; the run groups by ``agent_session_id``. This is the exact
        distinction the feed could not make before T3.
        """
        structlog.contextvars.clear_contextvars()
        session_token = agent_session_id_var.set(_SESSION)
        work_token = work_ref_var.set("gh:evoila/meho#2545")
        try:
            with actor_delegation("agent:incident-triage"):
                event = await _publish(captured_events)
        finally:
            agent_session_id_var.reset(session_token)
            work_ref_var.reset(work_token)
        assert event.principal_sub == "user-alice"
        assert event.actor_sub == "agent:incident-triage"
        assert event.agent_session_id == _SESSION
        assert event.work_ref == "gh:evoila/meho#2545"

    async def test_direct_human_request_carries_no_lineage(
        self,
        captured_events: list[BroadcastEvent],
    ) -> None:
        """No delegation / run / work_ref bound: lineage stays ``None``."""
        structlog.contextvars.clear_contextvars()
        event = await _publish(captured_events)
        assert event.principal_sub == "user-alice"
        assert event.actor_sub is None
        assert event.agent_session_id is None
        assert event.work_ref is None


# ---------------------------------------------------------------------------
# event_matches — actor_sub / work_ref filters
# ---------------------------------------------------------------------------


class TestEventMatchesLineageFilters:
    def test_actor_sub_filter_matches_and_excludes(self) -> None:
        event = _make_event(actor_sub="agent:triage")
        assert event_matches(
            event, op_class=None, principal=None, target=None, actor_sub="agent:triage"
        )
        assert not event_matches(
            event, op_class=None, principal=None, target=None, actor_sub="agent:other"
        )

    def test_work_ref_filter_matches_and_excludes(self) -> None:
        event = _make_event(work_ref="gh:evoila/meho#2545")
        assert event_matches(
            event, op_class=None, principal=None, target=None, work_ref="gh:evoila/meho#2545"
        )
        assert not event_matches(
            event, op_class=None, principal=None, target=None, work_ref="JIRA-1"
        )

    def test_actor_sub_filter_distinct_from_principal(self) -> None:
        """A delegated event matches on actor_sub but the human on principal."""
        event = _make_event(principal_sub="user-alice", actor_sub="agent:triage")
        assert event_matches(
            event, op_class=None, principal="user-alice", target=None, actor_sub="agent:triage"
        )
        assert not event_matches(
            event, op_class=None, principal="agent:triage", target=None, actor_sub=None
        )

    def test_announcement_never_qualifies_for_lineage_filters(self) -> None:
        """Lineage is audit-driven; an announcement carries no actor/work_ref."""
        announcement = AgentAnnouncementEvent(
            tenant_id=_TENANT,
            principal_sub="op-test",
            activity="probing rdc-vcenter",
            ts=_TS,
        )
        assert not event_matches(
            announcement, op_class=None, principal=None, target=None, actor_sub="agent:triage"
        )
        assert not event_matches(
            announcement, op_class=None, principal=None, target=None, work_ref="JIRA-1"
        )

    def test_unset_lineage_filters_leave_matching_unchanged(self) -> None:
        """Omitting the new filters keeps the pre-T3 op_class/principal/target semantics."""
        event = _make_event(op_class="write", principal_sub="user-alice")
        assert event_matches(event, op_class="write", principal="user-alice", target=None)
        assert not event_matches(event, op_class="read", principal=None, target=None)


# ---------------------------------------------------------------------------
# Grep-pinned invariant — every construction site projects lineage
# ---------------------------------------------------------------------------

_LINEAGE_KWARGS = frozenset({"actor_sub", "agent_session_id", "work_ref"})


def _broadcast_event_constructions() -> list[tuple[pathlib.Path, int, set[str]]]:
    """Every ``BroadcastEvent(...)`` call in the package, with its kwargs.

    AST-based so it is robust to formatting: finds ``Call`` nodes whose
    callee is the ``BroadcastEvent`` name (direct or attribute access) and
    records the keyword-argument names supplied at that site.
    """
    package_root = pathlib.Path(meho_backplane.__file__).parent
    found: list[tuple[pathlib.Path, int, set[str]]] = []
    for py_file in package_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name != "BroadcastEvent":
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
            found.append((py_file, node.lineno, kwargs))
    return found


def test_all_broadcast_event_constructions_project_lineage() -> None:
    """No call site constructs a ``BroadcastEvent`` without the lineage kwargs.

    Guards the goal end-to-end: adding a new broadcast surface that forgets
    to project ``actor_sub`` / ``agent_session_id`` / ``work_ref`` would
    reintroduce "agents broadcast as humans" on that surface. Fails with the
    offending file:line so the fix is obvious.
    """
    constructions = _broadcast_event_constructions()
    assert constructions, "expected to find BroadcastEvent construction sites in the package"
    offenders = [
        f"{path}:{lineno} missing {sorted(_LINEAGE_KWARGS - kwargs)}"
        for path, lineno, kwargs in constructions
        if not kwargs >= _LINEAGE_KWARGS
    ]
    assert not offenders, "BroadcastEvent constructed without lineage kwargs:\n" + "\n".join(
        offenders
    )
