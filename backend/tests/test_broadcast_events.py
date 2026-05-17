# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G6.1-T2 broadcast event schema + classifier.

Covers (issue #308 acceptance criteria):

* :class:`BroadcastEvent` is frozen, all required fields enforced,
  optional fields accept ``None``, dict ↔ JSON round-trips cleanly.
* :func:`classify_op` maps each AC-listed op-id to the right sensitivity
  class, plus edge cases the AC doesn't enumerate (empty string,
  prefix-conflict op-ids, mixed-case suffix near-misses).
* :func:`redact_payload` strips per the per-class contract: credential
  reads drop ``path`` / ``key``, audit queries drop ``filter``, generic
  reads keep full params (including nested objects).

PII redaction is the load-bearing contract — every test that exercises
:func:`redact_payload` for a sensitive class asserts NOT just on the
returned shape but on absent-key invariants the AC names explicitly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from meho_backplane.broadcast import BroadcastEvent, classify_op, redact_payload

# ---------------------------------------------------------------------------
# Fixtures — one ready-to-use canonical event for the schema tests
# ---------------------------------------------------------------------------


_TENANT_A: UUID = UUID("11111111-1111-1111-1111-111111111111")
_AUDIT_ID: UUID = UUID("22222222-2222-2222-2222-222222222222")
_EVENT_ID: UUID = UUID("33333333-3333-3333-3333-333333333333")
_TS = datetime(2026, 5, 13, 9, 0, 0, tzinfo=UTC)


def _make_event(**overrides: object) -> BroadcastEvent:
    """Return a fully-populated :class:`BroadcastEvent` with overridable fields."""
    base: dict[str, object] = {
        "event_id": _EVENT_ID,
        "ts": _TS,
        "tenant_id": _TENANT_A,
        "principal_sub": "op-test",
        "principal_name": "Operator Test",
        "target_name": "rdc-vcenter",
        "op_id": "vsphere.vm.list",
        "op_class": "read",
        "result_status": "ok",
        "audit_id": _AUDIT_ID,
        "payload": {
            "op_class": "read",
            "params": {"folder": "prod"},
            "result_status": "ok",
        },
    }
    base.update(overrides)
    return BroadcastEvent(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BroadcastEvent — schema, frozen, optionality, round-trip
# ---------------------------------------------------------------------------


class TestBroadcastEventSchema:
    """The wire-shape contract every downstream consumer reads back."""

    def test_required_fields_populated(self) -> None:
        event = _make_event()
        assert event.event_id == _EVENT_ID
        assert event.tenant_id == _TENANT_A
        assert event.audit_id == _AUDIT_ID
        assert event.op_class == "read"
        assert event.result_status == "ok"

    def test_model_is_frozen(self) -> None:
        """A consumer in the pipeline must not be able to rewrite the event."""
        event = _make_event()
        with pytest.raises(ValidationError):
            event.op_class = "write"  # type: ignore[misc]

    def test_optional_fields_accept_none(self) -> None:
        """``principal_name`` and ``target_name`` may be unknown at publish time."""
        event = _make_event(principal_name=None, target_name=None)
        assert event.principal_name is None
        assert event.target_name is None

    def test_optional_fields_omittable_at_construction(self) -> None:
        """Pydantic must accept events without ``principal_name`` / ``target_name``.

        T3's publisher (#309) builds events from audit-row data where
        ``principal_name`` is best-effort from the JWT's ``name`` claim
        (frequently absent) and ``target_name`` is unknown until the
        connector resolves the target alias. Forcing callers to pass
        ``principal_name=None`` / ``target_name=None`` explicitly turns
        every omission into boilerplate; the ``= None`` defaults on the
        model make omission the natural path.
        """
        event = BroadcastEvent(
            event_id=_EVENT_ID,
            ts=_TS,
            tenant_id=_TENANT_A,
            principal_sub="op-test",
            op_id="vsphere.vm.list",
            op_class="read",
            result_status="ok",
            audit_id=_AUDIT_ID,
        )
        assert event.principal_name is None
        assert event.target_name is None

    def test_payload_default_is_empty_dict(self) -> None:
        """Default payload lands at ``{}``, not a shared mutable instance.

        Constructs events that **omit** ``payload`` entirely so the
        model's ``Field(default_factory=dict)`` actually runs. The prior
        shape of this test built both events via ``_make_event()`` which
        always supplied a payload kwarg, so the default-factory path
        was never traversed — a future regression that swapped
        ``default_factory=dict`` for ``default={}`` (the literal — the
        classic mutable-default-argument footgun) would have silently
        bled payload data across events without failing this test.
        """
        base = _make_event().model_dump()
        base.pop("payload", None)
        a = BroadcastEvent(**base)
        b = BroadcastEvent(**base)
        assert a.payload == {}
        assert b.payload == {}
        # Mutating the dict on ``a`` after construction would mutate ``b`` too
        # if the field's default_factory were a shared instance; the frozen
        # model itself blocks that path, but verifying object identity
        # documents the contract.
        assert a.payload is not b.payload

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            BroadcastEvent(  # type: ignore[call-arg]
                event_id=_EVENT_ID,
                ts=_TS,
                tenant_id=_TENANT_A,
                principal_sub="op-test",
                principal_name=None,
                target_name=None,
                op_id="vsphere.vm.list",
                op_class="read",
                result_status="ok",
                # audit_id intentionally omitted
            )

    def test_json_round_trip(self) -> None:
        """T4 / T6 deserialise events from the Valkey stream; the round-trip must be lossless."""
        original = _make_event()
        serialised = original.model_dump_json()
        decoded = json.loads(serialised)
        rebuilt = BroadcastEvent.model_validate(decoded)
        assert rebuilt == original


# ---------------------------------------------------------------------------
# classify_op — AC #2 through #5 + extension cases
# ---------------------------------------------------------------------------


class TestClassifyOp:
    """Per-op-id sensitivity class derivation."""

    @pytest.mark.parametrize(
        ("op_id", "expected"),
        [
            ("vault.kv.read", "credential_read"),
            ("vault.kv.list", "credential_read"),
        ],
    )
    def test_credential_read_allowlist(self, op_id: str, expected: str) -> None:
        """Exact-match allowlist — decision #3 names these two."""
        assert classify_op(op_id) == expected

    @pytest.mark.parametrize(
        "op_id",
        [
            "audit.query",
            "audit.show",
            "audit.recent",
            "audit.who-touched",
        ],
    )
    def test_audit_prefix_maps_to_audit_query(self, op_id: str) -> None:
        """Every ``audit.*`` op classifies the same — response rows carry mixed sensitivity."""
        assert classify_op(op_id) == "audit_query"

    @pytest.mark.parametrize(
        ("op_id", "expected"),
        [
            ("vsphere.vm.list", "read"),
            ("vsphere.host.info", "read"),
            ("vsphere.about", "read"),
            ("k8s.pod.get", "read"),
            ("vsphere.vm.ls", "read"),
            # Vault sys diagnostics verbs (G3.3-T2 #546): non-mutating
            # cluster-state reads with no secret content. They must
            # classify as ``read`` (DoD: op_class=read), not fall
            # through to the full-detail ``other`` class.
            ("vault.sys.health", "read"),
            ("vault.sys.seal_status", "read"),
            ("vault.sys.mounts.list", "read"),
            ("vault.sys.auth.list", "read"),
            # KV-v2 version-metadata browse (G3.3-T1 #545): a read of
            # metadata only (no secret values) → ``read``, not
            # ``credential_read``.
            ("vault.kv.versions", "read"),
        ],
    )
    def test_read_suffixes(self, op_id: str, expected: str) -> None:
        assert classify_op(op_id) == expected

    @pytest.mark.parametrize(
        ("op_id", "expected"),
        [
            ("vsphere.vm.create", "write"),
            ("vsphere.vm.update", "write"),
            ("vsphere.vm.delete", "write"),
            ("k8s.deployment.patch", "write"),
            # KV-v2 write verb (G3.3-T1 #545). Without ``.put`` in the
            # write-suffix tuple this would fall through to ``other``
            # and broadcast the full secret payload.
            ("vault.kv.put", "write"),
        ],
    )
    def test_write_suffixes(self, op_id: str, expected: str) -> None:
        assert classify_op(op_id) == expected

    @pytest.mark.parametrize(
        "op_id",
        [
            "some.unknown.op",
            "weird",
            "vsphere.vm.poweron",  # not a write suffix
            "",
        ],
    )
    def test_unknown_falls_through_to_other(self, op_id: str) -> None:
        """Default branch keeps the broadcast informative without policy decisions."""
        assert classify_op(op_id) == "other"

    def test_credential_read_takes_priority_over_suffix(self) -> None:
        """``vault.kv.list`` ends in ``.list`` but the allowlist match wins.

        The order in :func:`classify_op` is intentional — a future op like
        ``vault.kv.audit-list`` could opt out of the credential class by
        being absent from the allowlist; this test pins the precedence
        rather than letting it drift to "first match by suffix" later.
        """
        assert classify_op("vault.kv.list") == "credential_read"

    def test_audit_prefix_takes_priority_over_suffix(self) -> None:
        """``audit.query`` has no read/write suffix; ``audit.list`` does.

        Both must classify as ``audit_query`` — the prefix gate runs before
        the suffix check, and ``audit.list`` returning ``"read"`` would leak
        audit-row contents the prefix-class is specifically meant to redact.
        """
        assert classify_op("audit.list") == "audit_query"


# ---------------------------------------------------------------------------
# redact_payload — AC #6, #7, #8 + edge cases
# ---------------------------------------------------------------------------


class TestRedactPayload:
    """Per-class redaction — load-bearing for the PII discipline."""

    def test_credential_read_drops_path_and_key(self) -> None:
        """AC #6 — only ``op_class`` and ``result_status`` survive."""
        result = redact_payload(
            "credential_read",
            {"path": "secret/foo", "key": "api-token"},
            "ok",
        )
        assert result == {"op_class": "credential_read", "result_status": "ok"}
        assert "path" not in result
        assert "key" not in result

    def test_audit_query_drops_filter(self) -> None:
        """AC #7 — filter never reaches the stream; row_count is allowed."""
        result = redact_payload(
            "audit_query",
            {"filter": "principal=damir", "since": "24h"},
            "ok",
        )
        assert result == {
            "op_class": "audit_query",
            "result_status": "ok",
            "row_count": None,
        }
        assert "filter" not in result
        assert "since" not in result

    def test_audit_query_surfaces_row_count_when_present(self) -> None:
        """The publisher pre-merges request + response; row_count plumbs through."""
        result = redact_payload(
            "audit_query",
            {"filter": "principal=damir", "row_count": 42},
            "ok",
        )
        assert result["row_count"] == 42

    def test_audit_query_handles_stringified_row_count(self) -> None:
        """JSON round-trips can stringify counts; the redactor coerces back to int."""
        result = redact_payload("audit_query", {"row_count": "42"}, "ok")
        assert result["row_count"] == 42

    def test_audit_query_non_numeric_row_count_becomes_none(self) -> None:
        """A malformed count never poisons the broadcast — fail-closed to None."""
        result = redact_payload("audit_query", {"row_count": "not-a-number"}, "error")
        assert result["row_count"] is None

    def test_read_keeps_full_params(self) -> None:
        """AC #8 — generic read ops broadcast in full."""
        result = redact_payload("read", {"folder": "prod"}, "ok")
        assert result == {
            "op_class": "read",
            "params": {"folder": "prod"},
            "result_status": "ok",
        }
        assert result["params"]["folder"] == "prod"

    def test_write_keeps_full_params(self) -> None:
        """Write ops also broadcast in full — the audit row carries the same."""
        result = redact_payload(
            "write",
            {"vm_name": "web-01", "memory_mb": 4096},
            "ok",
        )
        assert result["params"]["vm_name"] == "web-01"
        assert result["params"]["memory_mb"] == 4096

    def test_other_keeps_full_params(self) -> None:
        """Default class — same shape as read/write."""
        result = redact_payload("other", {"flag": True}, "ok")
        assert result["params"]["flag"] is True

    def test_read_preserves_nested_params(self) -> None:
        """Nested dicts pass through verbatim — no flattening, no transformation."""
        params: dict[str, object] = {
            "target": "rdc-vcenter",
            "filter": {"folder": "prod", "tags": ["pinned", "long-lived"]},
        }
        result = redact_payload("read", params, "ok")
        assert result["params"] == params
        # Verify the inner list is the same object — no defensive copy.
        # A future change that wraps params in a deep-copy would force
        # this assertion to flip, which is the right way to discover it.
        assert result["params"]["filter"] is params["filter"]

    def test_empty_params_for_each_class(self) -> None:
        """Boundary case — empty params dict produces well-shaped output for every class."""
        assert redact_payload("credential_read", {}, "ok") == {
            "op_class": "credential_read",
            "result_status": "ok",
        }
        assert redact_payload("audit_query", {}, "ok") == {
            "op_class": "audit_query",
            "result_status": "ok",
            "row_count": None,
        }
        assert redact_payload("read", {}, "ok") == {
            "op_class": "read",
            "params": {},
            "result_status": "ok",
        }

    @pytest.mark.parametrize("status", ["ok", "error", "denied"])
    def test_status_is_passed_through_verbatim(self, status: str) -> None:
        """``result_status`` is the handler's verdict — the redactor doesn't re-classify."""
        result = redact_payload("read", {}, status)
        assert result["result_status"] == status
