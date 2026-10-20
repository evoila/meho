# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Lineage fields on ``BroadcastEvent`` (T3 #2545).

Since #3758 nothing publishes a ``BroadcastEvent`` (Broadcast carries agent
announcements only). The model stays so older stream entries still parse,
so two contracts remain:

* the model's three optional lineage fields default to ``None`` (entries
  that predate them still validate);
* :func:`~meho_backplane.broadcast.history.event_matches` filters on
  ``actor_sub`` / ``work_ref``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.history import event_matches

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
