# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the policy-replay sense (G11.4-T5, #1074).

The acceptance criteria for #1074 require a second audit-replay sense:
re-run the recorded :class:`~meho_backplane.redaction.policy.RedactionPolicy`
against an audit row's captured ``raw_payload`` and verify it still
reproduces the row's stored ``redaction_manifest``. The tests pin:

* ``test_match_when_policy_is_deterministic`` — the canonical positive
  path. A row written with policy P; replay against P returns
  :data:`PolicyReplayStatus.MATCH` with empty diff lists. This is the
  load-bearing acceptance criterion ("empty diff").
* ``test_diverged_when_policy_regressed`` — a regression is detected:
  swapping the registered policy at *replay time* to one that fires
  differently produces :data:`PolicyReplayStatus.DIVERGED` with the
  delta on ``missing`` / ``extra``. This is the C1-d round-trip
  (#1073) gate's signal.
* ``test_audit_row_not_found_is_tenant_scoped`` — a cross-tenant id
  is indistinguishable from "no such id"; the function never leaks
  another tenant's audit rows.
* ``test_replay_not_applicable_when_no_raw_payload`` — a pre-G11.4-T2
  (#1071) row with NULL ``raw_payload`` / ``redaction_policy_id``
  surfaces as :data:`PolicyReplayStatus.REPLAY_NOT_APPLICABLE`, not a
  silent match.
* ``test_policy_not_found_when_id_is_retired`` — a row whose recorded
  policy id no longer resolves surfaces as
  :data:`PolicyReplayStatus.POLICY_NOT_FOUND` -- distinct from
  divergence so the operator knows the diagnosis is "policy retired",
  not "policy regressed".

Tests run against ``sqlite+aiosqlite`` via the autouse fixture in
:mod:`tests.conftest`, which migrates a fresh per-test DB to head;
rows are seeded directly through the sessionmaker.
"""

from __future__ import annotations

import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import (
    PolicyReplayResult,
    PolicyReplayStatus,
    replay_policy,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.redaction import (
    RedactionPolicy,
    clear_overrides,
    manifest_to_audit_payload,
    normalize_for_audit,
    parse_policy,
    redact,
    register_policy,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Keycloak + Vault env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_policy_overrides() -> Iterator[None]:
    """Reset registered policy overrides around every test in this file.

    The resolver's override table is process-global; a leaked
    registration would silently change the next test's policy lookup.
    Mirrors the discipline ``test_redaction_resolver.py`` uses.
    """
    clear_overrides()
    yield
    clear_overrides()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test, scoped to a single ``async with``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _bearer_policy(policy_id: str) -> RedactionPolicy:
    """Build a one-rule policy that fires on ``bearer_token`` patterns."""
    return parse_policy(
        textwrap.dedent(
            f"""
            id: {policy_id}
            version: 1
            rules:
              - name: r-bearer
                pattern: bearer_token
                action: redact
                reason: "test policy {policy_id}"
            """
        ).strip()
    )


def _api_key_policy(policy_id: str) -> RedactionPolicy:
    """Build a one-rule policy targeting ``api_key`` labelled credentials.

    Pattern-disjoint from :func:`_bearer_policy` so the
    "policy regressed" test sees a clean delta -- the old policy's
    matches disappear, the new policy's matches appear.
    """
    return parse_policy(
        textwrap.dedent(
            f"""
            id: {policy_id}
            version: 1
            rules:
              - name: r-api-key
                pattern: api_key
                action: redact
                reason: "test policy {policy_id}"
            """
        ).strip()
    )


async def _seed_audit_row(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    raw_payload: object,
    policy: RedactionPolicy | None,
) -> uuid.UUID:
    """Insert an audit row mirroring the dispatcher's write contract.

    When *policy* is supplied, the engine runs against *raw_payload* to
    produce the canonical manifest the replay will diff against. ``None``
    seeds a pre-G11.4-T2 row (no ``raw_payload`` / ``redaction_policy_id``)
    so the ``REPLAY_NOT_APPLICABLE`` path can be exercised.
    """
    audit_id = uuid.uuid4()
    normalized = normalize_for_audit(raw_payload) if policy is not None else None
    if policy is None:
        recorded_manifest: list[dict[str, object]] | None = None
        payload: dict[str, object] = {"op_id": "vsphere.vm.list"}
        raw_store: object | None = None
    else:
        result = redact(normalized, policy)
        recorded_manifest = manifest_to_audit_payload(result.manifest)
        payload = {
            "op_id": "vsphere.vm.list",
            "redaction_policy_id": policy.id,
            "connector_impl_id": "vsphere-rest",
        }
        raw_store = normalized
    s.add(
        AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub="operator-1",
            tenant_id=tenant_id,
            method="DISPATCH",
            path="vsphere.vm.list",
            status_code=200,
            duration_ms=Decimal("1.0"),
            payload=payload,
            raw_payload=raw_store,
            redaction_manifest=recorded_manifest,
        ),
    )
    return audit_id


# ---------------------------------------------------------------------------
# Match / diverge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_when_policy_is_deterministic(session: AsyncSession) -> None:
    """The canonical positive path -- empty diff on identical policy.

    A row written with policy P; replay against the same registered P
    re-produces the same manifest. This is the load-bearing AC: "policy-
    replay reproduces the agent's redacted view (empty diff)".
    """
    tenant_id = uuid.uuid4()
    policy = _bearer_policy("test-bearer-v1")
    register_policy(policy)
    raw_payload = {"headers": {"Authorization": "Bearer secret-token-aaaabbbbccccdddd"}}
    audit_id = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        raw_payload=raw_payload,
        policy=policy,
    )
    await session.commit()

    verdict = await replay_policy(audit_id, tenant_id=tenant_id, session=session)

    assert isinstance(verdict, PolicyReplayResult)
    assert verdict.status is PolicyReplayStatus.MATCH
    assert verdict.audit_id == audit_id
    assert verdict.policy_id == "test-bearer-v1"
    assert verdict.missing == ()
    assert verdict.extra == ()
    # The replayed redacted view is the engine's deterministic output;
    # the bearer token has been removed.
    assert verdict.replayed_redacted is not None
    assert "secret-token" not in str(verdict.replayed_redacted)


@pytest.mark.asyncio
async def test_diverged_when_policy_regressed(session: AsyncSession) -> None:
    """A policy regression at replay time is caught by the diff.

    Seed a row written under policy A (fires on bearer tokens). At
    replay time the registered policy under the same id has been
    swapped for policy B (fires on api_key shapes, ignores bearer
    tokens). The replay must diverge: the bearer-token manifest entry
    is in ``missing``; if the raw payload had an api_key shape, the
    api_key entry would be in ``extra``. Here we use a raw payload
    that only has a bearer token, so the regression mode is "we used
    to redact this; we now leak it" -- the operator-visible
    correctness signal.
    """
    tenant_id = uuid.uuid4()
    original_policy = _bearer_policy("regressed-id")
    register_policy(original_policy)
    raw_payload = {"headers": {"Authorization": "Bearer secret-token-aaaabbbbccccdddd"}}
    audit_id = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        raw_payload=raw_payload,
        policy=original_policy,
    )
    await session.commit()

    # Regress: same policy id, different rule. The replay looks up by
    # id and re-runs against the recorded raw; the manifest diverges.
    clear_overrides()
    regressed_policy = _api_key_policy("regressed-id")
    register_policy(regressed_policy)

    verdict = await replay_policy(audit_id, tenant_id=tenant_id, session=session)

    assert verdict.status is PolicyReplayStatus.DIVERGED
    assert verdict.policy_id == "regressed-id"
    # The original bearer-token manifest entry is missing from the
    # replay; the operator-visible regression "we no longer redact
    # bearer tokens" surfaces here.
    assert len(verdict.missing) == 1
    assert verdict.missing[0].pattern == "bearer_token"
    # No api_key matches in the raw payload, so ``extra`` is empty;
    # the divergence is purely "rule no longer fires".
    assert verdict.extra == ()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_not_found_is_tenant_scoped(session: AsyncSession) -> None:
    """A cross-tenant audit id resolves the same as a missing id.

    Defense-in-depth: policy-replay must not leak the existence of
    another tenant's audit rows. Tenant B's id surfaces as
    ``AUDIT_ROW_NOT_FOUND`` from tenant A's perspective.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    policy = _bearer_policy("test-tenant-iso")
    register_policy(policy)
    raw_payload = {"key": "Bearer secret-token-aaaabbbbccccdddd"}
    audit_id = await _seed_audit_row(
        session,
        tenant_id=tenant_b,
        raw_payload=raw_payload,
        policy=policy,
    )
    await session.commit()

    # Replay with the row's id but the wrong tenant: 404-shaped.
    verdict = await replay_policy(audit_id, tenant_id=tenant_a, session=session)
    assert verdict.status is PolicyReplayStatus.AUDIT_ROW_NOT_FOUND
    assert verdict.audit_id == audit_id

    # Replay with the *unknown* id under tenant_a: still 404-shaped,
    # structurally indistinguishable.
    verdict_unknown = await replay_policy(uuid.uuid4(), tenant_id=tenant_a, session=session)
    assert verdict_unknown.status is PolicyReplayStatus.AUDIT_ROW_NOT_FOUND


# ---------------------------------------------------------------------------
# Edge cases: not-applicable / not-found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_not_applicable_when_no_raw_payload(session: AsyncSession) -> None:
    """Pre-G11.4-T2 rows (NULL ``raw_payload``) surface as not-applicable.

    A row written before the connector-boundary redaction middleware
    shipped, or an error-path row whose handler raised before
    producing a response, has no captured raw payload. The replay has
    nothing to run against; the verdict must distinguish that from a
    successful match (silent success on an empty replay would be
    actively misleading -- "this row was checked" when nothing was).
    """
    tenant_id = uuid.uuid4()
    audit_id = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        raw_payload=None,
        policy=None,
    )
    await session.commit()

    verdict = await replay_policy(audit_id, tenant_id=tenant_id, session=session)
    assert verdict.status is PolicyReplayStatus.REPLAY_NOT_APPLICABLE
    # No policy id was recorded; the verdict carries None to match.
    assert verdict.policy_id is None


@pytest.mark.asyncio
async def test_policy_not_found_when_id_is_retired(session: AsyncSession) -> None:
    """A retired policy id surfaces as ``POLICY_NOT_FOUND``, not divergence.

    Operator clarity: "the policy whose id is on this row is no longer
    registered" is a distinct diagnosis from "the policy still
    registered under that id now produces a different manifest". The
    former is "policy was removed"; the latter is "policy regressed".
    Conflating them would mask the actual diagnosis.
    """
    tenant_id = uuid.uuid4()
    policy = _bearer_policy("retired-policy-id")
    register_policy(policy)
    raw_payload = {"key": "Bearer secret-token-aaaabbbbccccdddd"}
    audit_id = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        raw_payload=raw_payload,
        policy=policy,
    )
    await session.commit()

    # Retire the policy: clear the registration, do not replace it.
    clear_overrides()

    verdict = await replay_policy(audit_id, tenant_id=tenant_id, session=session)
    # ``replay_policy`` falls back to the packaged default for policy
    # lookups; the recorded id ``retired-policy-id`` does not match
    # the default's id (``default-connector-redaction``), so the verdict
    # is ``POLICY_NOT_FOUND``.
    assert verdict.status is PolicyReplayStatus.POLICY_NOT_FOUND
    assert verdict.policy_id == "retired-policy-id"
    # No replay ran, so the redacted view is None.
    assert verdict.replayed_redacted is None
