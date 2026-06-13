# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G6 ``credential_read`` classifier integration test (G3.3-T5 / #549).

Load-bearing PII guarantee for the single most-used agent op. Decision
#3 (``docs/planning/v0.2-decisions.md``) names ``vault.kv.read`` and
``vault.kv.list`` as the canonical ``credential_read`` ops: the G6
broadcast feed must emit an **aggregate-only** view for them — the
fact that someone touched a credential, never *which* credential. No
mount, no path, no key names, no values may reach the stream.

This module proves that contract end-to-end across the
dispatch + broadcast boundary, exercising the **real**
``vault.kv.read`` / ``vault.kv.list`` handlers
(:func:`~meho_backplane.connectors.vault.ops.vault_kv_read` /
:func:`~meho_backplane.connectors.vault.ops.vault_kv_list`) registered
via :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
and driven through :func:`~meho_backplane.operations.dispatch`. The
Vault HTTP client is replaced with the shared in-process fake
(:func:`tests._vault_fakes.install_fake_vault`), seeded with
distinctive sentinel mount / path / key / value strings. The broadcast
publisher is swapped for a recording stub so the emitted
:class:`~meho_backplane.broadcast.events.BroadcastEvent` can be
inspected without standing up a Valkey container.

Why assert by exclusion on the *serialised event*: the redacted
``payload`` for ``credential_read`` collapses to
``{op_class, result_status}``, but the enclosing
:class:`BroadcastEvent` always carries ``op_id``, ``target_name`` and
``result_status`` as top-level fields (decision #3's
``{op_id, target, result_status}`` aggregate). The leak surface is
therefore the whole serialised event, not just ``payload`` — a
regression that smuggled the path into a new top-level field would
pass a ``payload``-only assertion. Each test serialises the event with
``model_dump_json()`` and asserts every secret-shaped sentinel
substring is absent from that text.

Reconciliation with the issue body (#549): the body describes #545
setting ``op_class='credential_read'`` as *register-time descriptor
metadata* read back by the classifier. The shipped G6 substrate has
**no per-row ``op_class`` column** on ``endpoint_descriptor`` —
classification is op-id-based via
:func:`~meho_backplane.broadcast.events.classify_op` (the
``_CREDENTIAL_READ_OPS`` allowlist plus the ``_READ_SUFFIXES`` /
``_WRITE_SUFFIXES`` tuples). #228 (G6.1 SSE feed) is shipped/CLOSED, so
this integrates against the real classifier path. The test satisfies
the *intent* of decision #3 — aggregate-only emission for the two
credential-read ops, proven through the live dispatch + broadcast
path — against the mechanism that actually shipped.

Skip-clean: per the issue's DoD, the test skips (does not fail) when
the broadcast feed is not configured in the environment
(``BROADCAST_REDIS_URL`` unset / blank), so a sandbox without the
broadcast substrate reports a clean skip rather than a spurious
failure. The publisher swap means no real Valkey connection is ever
opened; the guard mirrors the operator-facing "broadcast disabled"
posture.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.connectors.vault.ops_sys import (
    register_vault_sys_typed_operations,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_vault

# ---------------------------------------------------------------------------
# Skip-clean guard — broadcast disabled in the environment
# ---------------------------------------------------------------------------

#: The G6.1 substrate has no boolean "broadcast enabled" flag — the
#: feed is "on" iff a Valkey URL is configured. A sandbox that does not
#: provision the broadcast substrate leaves ``BROADCAST_REDIS_URL``
#: unset; treating that as the disabled posture lets the test skip
#: cleanly there (issue #549 DoD) rather than fail. The publisher is
#: swapped for a recording stub in every test below, so this guard is
#: about *environment intent*, not about whether a socket would open.
_BROADCAST_DISABLED = not (os.environ.get("BROADCAST_REDIS_URL") or "").strip()

pytestmark = pytest.mark.skipif(
    _BROADCAST_DISABLED,
    reason="broadcast feed disabled (BROADCAST_REDIS_URL unset) — G6 "
    "credential_read classifier integration test skipped cleanly per #549 DoD",
)


# ---------------------------------------------------------------------------
# Sentinel secret material — distinctive enough that any leak is obvious
# ---------------------------------------------------------------------------

#: Non-default mount so a hardcoded ``"secret"`` default can't mask a
#: leak. Deliberately unusual so a substring search is unambiguous and
#: cannot collide with the (allowed) target name.
_SENTINEL_MOUNT = "sntlmount0zzz"
#: Folder/secret path. Each segment is its own distinctive token so a
#: *partial* leak (just one segment reaching the stream) is still
#: caught — exactly the environment-name-shaped structure decision #3
#: forbids broadcasting. No segment is a generic English word, so the
#: by-exclusion search can't false-positive on the allowed
#: ``target_name`` (``rdc-vault-prod``).
_SENTINEL_PATH_SEGMENTS = ("sntlenv0zzz", "sntlsvc0zzz", "sntlleaf0zzz")
_SENTINEL_PATH = "/".join(_SENTINEL_PATH_SEGMENTS)
#: Secret key names returned by the list op.
_SENTINEL_KEYS = ["sntlkey0apikey0zzz", "sntlkey0token0zzz"]
#: Secret value returned by the read op.
_SENTINEL_SECRET = {"password": "sntlvalue0p4ssw0rd0zzz"}

#: Every substring that MUST NOT appear anywhere in a ``credential_read``
#: broadcast event. The mount, the full path, **each path segment** (a
#: partial-leak regression is still a leak), every key name, and the
#: secret value. The decision-#3 aggregate explicitly *permits*
#: ``op_id`` + ``target_name`` + ``result_status``; none of these
#: sentinels appear in those allowed fields, so a hit is an
#: unambiguous secret-material leak.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    _SENTINEL_MOUNT,
    _SENTINEL_PATH,
    *_SENTINEL_PATH_SEGMENTS,
    *_SENTINEL_KEYS,
    *_SENTINEL_SECRET.values(),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for this module.

    Mirrors :mod:`tests.test_operations_dispatcher` — the dispatcher
    constructs :class:`Settings` (Vault address, Keycloak issuer) while
    resolving the typed op, so the cache is cleared around the test.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # The default-on tenant-scope guard (#1725) pins KV calls under
    # ``secret/tenants/{tenant_id}/``. These tests dispatch against the
    # ``_SENTINEL_MOUNT`` sentinel mount under a real operator tenant to
    # exercise the credential-classifier path, not tenant isolation
    # (covered by ``test_connectors_vault_tenant_scope.py``). The sentinel
    # path is not under ``secret/tenants/<id>/``, so the guard would deny
    # it with VaultTenantScopeError once Redis is present. Disable the
    # guard explicitly — matching the empty-prefix pin the #1725 PR used
    # for its e2e fixtures.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test.

    Registers the v2 ``vault`` connector entry after the setup clear so
    the dispatcher's natural-key resolution finds an implementation for
    ``connector_id="vault-1.x"`` — the per-test
    ``register_vault_typed_operations`` call only upserts the typed-op
    *descriptor* rows, not the connector *class*, and connector
    resolution is a hard gate before the handler runs (``no_connector``
    otherwise). Mirrors the ``_registered_vault_substrate`` fixture in
    ``test_api_v1_health.py``.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with an in-memory recording stub.

    The dispatch audit helper invokes ``publish_event`` via the imported
    reference inside :mod:`meho_backplane.operations._audit`; patching
    that module attribute is sufficient (the same seam
    :mod:`tests.test_operations_dispatcher` uses). No real Valkey
    connection is opened.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


def _make_operator() -> Operator:
    """Construct an :class:`Operator` directly — no JWT round-trip.

    ``raw_jwt`` is read by
    :func:`~meho_backplane.auth.vault.vault_client_for_operator`, which
    the real Vault handlers call. The value is irrelevant — the fake
    Vault client (``install_fake_vault``) accepts any JWT.
    """
    return Operator(
        sub="op-credential-read-test",
        name="Credential Read Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c3"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint — the resolver reads only ``version``."""

    def __init__(self, version: str | None = "1.x") -> None:
        self.version = version


class _VaultDispatchTarget:
    """Target that satisfies the dispatcher's resolution + audit reads.

    The dispatcher reads ``product`` / ``fingerprint.version`` /
    ``preferred_impl_id`` (connector resolution — tolerated-miss for
    typed ops) and ``id`` / ``name`` (audit row + broadcast
    ``target_name``). The Vault handlers read the operator JWT from the
    request-scoped :class:`~meho_backplane.auth.operator.Operator` the
    dispatcher threads (G0.8-T3 #629), **not** from this target — so it
    carries no ``raw_jwt``.

    ``name`` is a benign, non-secret label; the broadcast event is
    *allowed* to carry it (decision #3's aggregate includes the
    target). It is deliberately distinct from every forbidden
    sentinel so the by-exclusion assertion can't false-positive on it.
    """

    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = _FakeFingerprint(version="1.x")
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "rdc-vault-prod"


# ---------------------------------------------------------------------------
# credential_read aggregate-only contract — vault.kv.read / vault.kv.list
# ---------------------------------------------------------------------------


def _assert_aggregate_only(event: BroadcastEvent, op_id: str) -> None:
    """Assert *event* is the decision-#3 aggregate for a credential read.

    The redacted ``payload`` is exactly ``{op_class, result_status}``;
    the enclosing event carries ``op_id`` / ``target_name`` /
    ``result_status`` (the allowed aggregate). The whole serialised
    event must contain none of the secret-shaped sentinels.
    """
    assert event.op_id == op_id
    assert event.op_class == "credential_read"
    assert event.result_status == "ok"
    # payload is aggregate-only — no params, no path/mount/key/value.
    assert event.payload == {
        "op_class": "credential_read",
        "result_status": "ok",
    }

    serialised = event.model_dump_json()
    # Defensive: model_dump_json must round-trip to the same shape the
    # SSE feed (#228) and MCP resource consumers deserialise.
    assert json.loads(serialised)["op_class"] == "credential_read"

    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in serialised, (
            f"credential_read leak: {forbidden!r} reached the broadcast "
            f"event for {op_id} — serialised event: {serialised}"
        )


@pytest.mark.asyncio
async def test_vault_kv_read_broadcast_is_aggregate_only(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.read`` broadcasts only ``{op_id, target, result_status}``.

    DoD #1 — the read op's broadcast event contains exactly the
    aggregate; mount / path / key / value never present (by-exclusion).
    """
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = dict(_SENTINEL_SECRET)

    await register_vault_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={"mount": _SENTINEL_MOUNT, "path": _SENTINEL_PATH},
    )

    assert result.status == "ok", result.error
    # The handler really ran against the fake — the secret value is in
    # the OperationResult (operator-facing, audited), proving the leak
    # surface below is a real exclusion, not a vacuous one.
    assert result.result == {"data": _SENTINEL_SECRET, "version": fake.secrets.kv.v2.version}
    assert fake.secrets.kv.v2.read_calls == [
        {"path": _SENTINEL_PATH, "mount_point": _SENTINEL_MOUNT}
    ]

    assert len(captured_events) == 1
    _assert_aggregate_only(captured_events[0], "vault.kv.read")


@pytest.mark.asyncio
async def test_vault_kv_list_broadcast_is_aggregate_only(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.list`` broadcasts only the aggregate.

    DoD #2 — same by-exclusion assertion as the read op. Key names are
    structure too (decision #3) — the list op is ``credential_read``
    even though it returns no secret values.
    """
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.keys = list(_SENTINEL_KEYS)

    await register_vault_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        target=target,
        params={"mount": _SENTINEL_MOUNT, "path": _SENTINEL_PATH},
    )

    assert result.status == "ok", result.error
    assert result.result == {"keys": _SENTINEL_KEYS}
    assert fake.secrets.kv.v2.list_calls == [
        {"path": _SENTINEL_PATH, "mount_point": _SENTINEL_MOUNT}
    ]

    assert len(captured_events) == 1
    _assert_aggregate_only(captured_events[0], "vault.kv.list")


# ---------------------------------------------------------------------------
# Contrast — a non-credential_read op broadcasts its normal payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_credential_read_op_broadcasts_full_payload(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.sys.health`` broadcasts its normal (non-redacted) payload.

    DoD #3 — proves the redaction is classifier-driven, not blanket.
    ``vault.sys.health`` classifies ``read`` (the ``.health``
    suffix), so its broadcast carries the full ``params`` block. Same
    dispatch + broadcast path, same recording publisher, same fake
    Vault — only the op-id differs, so a divergence here is purely the
    classifier doing its job.
    """
    install_fake_vault(monkeypatch)
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.health",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.op_id == "vault.sys.health"
    # Classifier-driven: NOT credential_read, NOT aggregate-only.
    assert event.op_class == "read"
    assert event.op_class != "credential_read"
    # Full-payload class — the redactor passes the request params
    # through verbatim under ``params``. The dispatch audit helper
    # (:func:`meho_backplane.operations._audit.publish_broadcast`)
    # hands ``{"params": params}`` to ``redact_payload``, so the
    # non-redacted branch nests the op params one level deep. The
    # load-bearing contrast is that the params block is *present at
    # all* — a ``credential_read`` op has no ``params`` key whatsoever.
    assert event.payload == {
        "op_class": "read",
        "params": {"params": {}},
        "result_status": "ok",
    }
    assert "params" in event.payload


# ---------------------------------------------------------------------------
# Classifier-contrast on a single fixed input — same op-id surface,
# opposite redaction. Pins the "credential_read vs read" boundary that
# the whole PII guarantee rests on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_read_and_contrast_diverge_on_same_dispatch_path(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """One run, two ops: read is redacted, sys.health is not.

    Dispatches a ``credential_read`` op and a ``read`` op back-to-back
    through the identical path with identical infrastructure. The only
    variable is the op-id; the divergent redaction is therefore
    attributable solely to :func:`classify_op`. This is the
    load-bearing assertion decision #3 exists for.
    """
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = dict(_SENTINEL_SECRET)

    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    read_result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={"mount": _SENTINEL_MOUNT, "path": _SENTINEL_PATH},
    )
    health_result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.health",
        target=target,
        params={},
    )

    assert read_result.status == "ok", read_result.error
    assert health_result.status == "ok", health_result.error
    assert len(captured_events) == 2

    cred_event = next(e for e in captured_events if e.op_id == "vault.kv.read")
    health_event = next(e for e in captured_events if e.op_id == "vault.sys.health")

    # Same path, opposite redaction — classifier-driven by construction.
    assert cred_event.op_class == "credential_read"
    assert health_event.op_class == "read"
    _assert_aggregate_only(cred_event, "vault.kv.read")
    assert "params" in health_event.payload
