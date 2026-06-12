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
            "harbor.robot.create",
            "vault.token.create",
            "vault.auth.approle.generate_secret_id",
        ],
    )
    def test_credential_mint_allowlist(self, op_id: str) -> None:
        """Response-secret ops classify ``credential_mint`` (G11.7-T1 #1401).

        ``vault.token.create`` / ``vault.auth.approle.generate_secret_id``
        end in ``.create`` / a write-shaped verb but the allowlist is
        consulted before the write-suffix branch, so the freshly-minted
        secret in the response collapses to aggregate-only rather than
        broadcasting under the plain ``write`` class.
        """
        assert classify_op(op_id) == "credential_mint"

    @pytest.mark.parametrize(
        "op_id",
        [
            "vault.auth.userpass.write",
            "vault.auth.userpass.update_password",
            "vault.kv.put",
            "vault.kv.patch",
            "k8s.secret.create",
        ],
    )
    def test_credential_write_allowlist(self, op_id: str) -> None:
        """Request-secret write ops classify ``credential_write`` (G11.7-T1 #1401).

        These ops carry the secret in their *request params*. The
        allowlist is consulted before the ``.write`` / ``.put`` /
        ``.create`` suffix branch so the broadcast (which ships params)
        collapses to aggregate-only instead of leaking the written
        credential under the plain ``write`` class. ``vault.kv.put``
        moved here from ``write`` — it shipped pre-G11.7 broadcasting its
        secret ``data`` in full.
        """
        assert classify_op(op_id) == "credential_write"

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
        "op_id",
        [
            "meho.audit.replay",
            "meho.audit.query",
        ],
    )
    def test_meho_audit_prefix_maps_to_audit_query(self, op_id: str) -> None:
        """``meho.audit.*`` admin meta-tools classify as audit_query (G8.2-T6 #1014).

        The MCP broadcast path derives op_class from ``classify_op(op_id)``
        with the tool name verbatim — it does not honor the
        ``ToolDefinition.op_class``. Without the ``meho.audit.`` prefix arm,
        ``meho.audit.replay`` would fall through to ``other`` and broadcast
        its full ``ReplayNode`` payload instead of the aggregate-only view.
        """
        assert classify_op(op_id) == "audit_query"

    def test_meho_broadcast_prefix_is_not_audit_query(self) -> None:
        """Only ``meho.audit.`` opts into audit_query — sibling meho.* tools don't.

        Guards against the prefix arm being widened to a bare ``meho.``
        match, which would mis-redact unrelated admin meta-tools like
        ``meho.broadcast.overrides.set``.
        """
        assert classify_op("meho.broadcast.overrides.set") == "other"

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
            # Vault ACL-policy list (G3.15-T2 #1410): ``.list`` suffix →
            # ``read`` (policy names, no secret content). ``policy.read``
            # is intentionally ``other`` (see test_unknown_falls_through_
            # to_other) — ``.read`` is not a read-suffix.
            ("vault.sys.policy.list", "read"),
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
            # bind9 record-write verbs (G3.4-T3 #589). The bind9
            # connector uses ``.add`` / ``.remove`` to match the
            # consumer wrapper's verb shape; without these suffixes
            # the rdata (FQDN + IP) would broadcast as ``other`` rather
            # than redact under the ``write`` branch.
            ("bind9.record.add", "write"),
            ("bind9.record.remove", "write"),
            # Vault ``.write`` mutating verb (G3.15 #1410/#1411).
            # ``vault.auth.approle.write`` carries no secret in its
            # params (a role definition), so it classifies as plain
            # ``write`` via the ``.write`` suffix; ``vault.sys.policy.write``
            # writes a policy document (full HCL body in its params).
            # Both would fall through to ``other`` without the ``.write``
            # suffix. ``vault.sys.policy.delete`` removes a policy by name
            # (G3.15-T2 #1410), classified ``write`` via the ``.delete``
            # suffix.
            ("vault.auth.approle.write", "write"),
            ("vault.sys.policy.write", "write"),
            ("vault.sys.policy.delete", "write"),
        ],
    )
    def test_write_suffixes(self, op_id: str, expected: str) -> None:
        assert classify_op(op_id) == expected

    @pytest.mark.parametrize(
        "op_id",
        [
            "vault.auth.userpass.write",
            "vault.auth.userpass.update_password",
        ],
    )
    def test_credential_write_allowlist_wins_over_write_suffix(self, op_id: str) -> None:
        """Userpass ``.write`` ops stay ``credential_write``, never plain ``write``.

        Critical safety precedence: ``vault.auth.userpass.write`` ends in
        ``.write`` (a mutation suffix). The credential-write allowlist is
        consulted in :func:`classify_op` *before* the ``.write`` suffix
        branch, so the password riding in ``params`` collapses to
        aggregate-only — it is never broadcast in full under the plain
        ``write`` class. This test pins that ordering so a future suffix
        reshuffle can't silently downgrade a credential write and leak the
        password to every operator on the feed.
        """
        assert classify_op(op_id) == "credential_write"

    @pytest.mark.parametrize(
        "op_id",
        [
            "some.unknown.op",
            "weird",
            "vsphere.vm.poweron",  # not a write suffix
            "",
            # G3.15-T2 #1410: ``.read`` is deliberately not a read-suffix
            # (it would over-match the credential_read-allowlisted
            # vault.kv.read and the auth-config .read ops). vault.sys.
            # policy.read's only param is the policy name, so ``other``
            # (full params) is the safe, consistent classification.
            "vault.sys.policy.read",
        ],
    )
    def test_unknown_falls_through_to_other(self, op_id: str) -> None:
        """Default branch keeps the broadcast informative without policy decisions."""
        assert classify_op(op_id) == "other"

    @pytest.mark.parametrize(
        ("op_id", "expected"),
        [
            ("GET:/api/v2.0/systeminfo", "read"),
            ("GET:/api/v2.0/projects", "read"),
            ("HEAD:/api/v2.0/projects/myproj", "read"),
            ("POST:/api/v2.0/projects", "write"),
            ("PUT:/api/v2.0/projects/myproj", "write"),
            ("PATCH:/api/v2.0/projects/myproj", "write"),
            ("DELETE:/api/v2.0/projects/myproj/repositories/repo", "write"),
        ],
    )
    def test_http_method_prefix_ingested_ops(self, op_id: str, expected: str) -> None:
        """HTTP-method-prefixed ingested op IDs map via HTTP semantics."""
        assert classify_op(op_id) == expected

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

    def test_credential_write_drops_secret_params(self) -> None:
        """G11.7-T1 #1401 — a request-secret write redacts to aggregate-only.

        The written credential lives in ``params``; the broadcast ships
        params, so ``credential_write`` must collapse to
        ``{op_class, result_status}`` with no trace of the secret.
        """
        result = redact_payload(
            "credential_write",
            {"path": "secret/db", "data": {"password": "s3kr3t-sentinel"}},
            "ok",
        )
        assert result == {"op_class": "credential_write", "result_status": "ok"}
        assert "s3kr3t-sentinel" not in str(result)
        assert "data" not in result
        assert "params" not in result

    def test_credential_mint_drops_response_secret(self) -> None:
        """G11.7-T1 #1401 — a response-secret mint redacts to aggregate-only."""
        result = redact_payload(
            "credential_mint",
            {"token": "hvs.minted-sentinel", "role": "ci"},
            "ok",
        )
        assert result == {"op_class": "credential_mint", "result_status": "ok"}
        assert "hvs.minted-sentinel" not in str(result)

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

    def test_meho_audit_replay_broadcast_is_aggregate_only(self) -> None:
        """``meho.audit.replay`` broadcasts aggregate-only — no ReplayNode tree (G8.2-T6 #1014).

        The MCP broadcast path classifies via ``classify_op(op_id)`` and
        redacts via ``redact_payload(op_class, raw_params, status)``. This
        chains both with the replay tool's name + a representative
        result-params dict to prove the SSE event carries only
        ``{op_class, result_status, row_count}`` — never the ``root``
        ReplayNode forest (full aggregate-only integration assertion is
        T7).
        """
        op_class = classify_op("meho.audit.replay")
        assert op_class == "audit_query"
        result = redact_payload(
            op_class,
            {
                "session_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "root": [{"id": "secret-node", "payload": {"vm": "prod-db"}}],
                "row_count": 12,
            },
            "ok",
        )
        assert result == {
            "op_class": "audit_query",
            "result_status": "ok",
            "row_count": 12,
        }
        assert "root" not in result
        assert "session_id" not in result

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


# ---------------------------------------------------------------------------
# redact_payload(detail=...) — G6.3-T2 (#379) extension
# ---------------------------------------------------------------------------


class TestRedactPayloadDetailKwarg:
    """G6.3-T2: the resolver's chosen detail picks the redaction branch."""

    def test_detail_none_preserves_existing_credential_read_shape(self) -> None:
        """``detail=None`` (callers not going through the resolver) keeps pre-G6.3 behaviour."""
        result = redact_payload(
            "credential_read",
            {"path": "secret/foo"},
            "ok",
            detail=None,
        )
        assert result == {"op_class": "credential_read", "result_status": "ok"}

    def test_detail_none_preserves_existing_read_shape(self) -> None:
        result = redact_payload("read", {"folder": "prod"}, "ok", detail=None)
        assert result == {
            "op_class": "read",
            "params": {"folder": "prod"},
            "result_status": "ok",
        }

    def test_detail_full_on_credential_read_returns_full_params(self) -> None:
        """The request_override upgrade case: sensitive class → full detail.

        Pins the AC: configure ``request_override="full"`` on a
        ``credential_read`` → resolver returns ``detail="full"`` →
        :func:`redact_payload` returns full params + response summary.
        """
        result = redact_payload(
            "credential_read",
            {"path": "secret/foo", "key": "api-token"},
            "ok",
            detail="full",
        )
        assert result == {
            "op_class": "credential_read",
            "params": {"path": "secret/foo", "key": "api-token"},
            "result_status": "ok",
        }

    def test_detail_full_on_audit_query_returns_full_params(self) -> None:
        result = redact_payload(
            "audit_query",
            {"filter": "principal=damir"},
            "ok",
            detail="full",
        )
        assert result == {
            "op_class": "audit_query",
            "params": {"filter": "principal=damir"},
            "result_status": "ok",
        }

    def test_detail_aggregate_on_read_collapses_to_credential_shape(self) -> None:
        """The tenant-rule downgrade case: non-sensitive class → aggregate.

        Pins the AC: configure ``op_id_pattern="k8s.configmap.info"``,
        ``scope_field="namespace"``, ``scope_value="kube-system"``,
        ``detail="aggregate"`` → resolver returns ``detail="aggregate"``
        → :func:`redact_payload` collapses to the credential_read
        aggregate shape (no params).
        """
        result = redact_payload(
            "read",
            {"namespace": "kube-system", "name": "kube-root-ca.crt"},
            "ok",
            detail="aggregate",
        )
        assert result == {"op_class": "read", "result_status": "ok"}

    def test_detail_aggregate_on_audit_query_keeps_row_count(self) -> None:
        """audit_query aggregate is special-cased to preserve row_count."""
        result = redact_payload(
            "audit_query",
            {"filter": "x", "row_count": 7},
            "ok",
            detail="aggregate",
        )
        assert result == {
            "op_class": "audit_query",
            "result_status": "ok",
            "row_count": 7,
        }

    def test_detail_aggregate_on_credential_read_matches_default(self) -> None:
        """``detail="aggregate"`` on the already-aggregate class is a no-op shape-wise."""
        result = redact_payload(
            "credential_read",
            {"path": "secret/foo"},
            "ok",
            detail="aggregate",
        )
        assert result == {"op_class": "credential_read", "result_status": "ok"}
