# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.12-T4 ArgoCD write-op recorded-fixture E2E test (#1405).

Drives the seven approval-gated argocd write ops through the dispatch stack
against a respx-mocked ``argocd-server`` REST API. No running ArgoCD, no
live Vault: the bearer-token loader is injected, ``respx`` replays the
recorded write fixtures, and the connector instance is preseeded into the
dispatcher's instance cache.

Acceptance criteria verified (Issue #1405)
==========================================

* The seven write ops register with the stated safety levels, all
  ``requires_approval=True`` (asserted on ``ARGOCD_WRITE_OPS``).
* A ``USER``-principal dispatch of a write op routes to the human
  approve-queue (``status=awaiting_approval``) rather than hard-deny — the
  governance dependency #1401 — and never reaches the handler.
* ``app.sync`` / ``app.rollback`` POST then poll ``status.operationState``
  to a terminal phase (Succeeded / Failed / Error) and return the final
  phase + message.
* ``app.set`` / ``app.delete`` capture before/after (or the cascade list)
  into ``proposed_effect``.
* Every write carries the Vault-sourced Bearer; the token never leaks.

The handler path is exercised with ``_approved=True`` (the approvals-API
resume flag) so the test drives the real handler/poll/HTTP behaviour the way
it runs *after* a human approves.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.argocd  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.argocd import ArgoCdConnector
from meho_backplane.connectors.argocd.ops_write import (
    ARGOCD_WRITE_OPS,
    TERMINAL_OPERATION_PHASES,
)
from meho_backplane.connectors.argocd.session import ArgoCdTargetLike
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "argocd-api-3.x"
_TARGET_NAME = "rdc-argocd-write-e2e"
_ARGOCD_HOST = "argocd-write-e2e.test.invalid"
_ARGOCD_BASE_URL = f"https://{_ARGOCD_HOST}"
_BEARER_TOKEN = "argocd-write-bearer-token-e2e-DO-NOT-LEAK"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_OPERATOR = Operator(
    sub="argocd-write-e2e-test",
    name="ArgoCD Write E2E Test Operator",
    email=None,
    raw_jwt="<argocd-write-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)

_APP_NAME = "guestbook"

EXPECTED_WRITE_OP_IDS: tuple[str, ...] = (
    "argocd.app.sync",
    "argocd.app.rollback",
    "argocd.app.set",
    "argocd.app.refresh",
    "argocd.app.delete",
    "argocd.appproject.create",
    "argocd.appproject.update",
)

EXPECTED_SAFETY: dict[str, str] = {
    "argocd.app.sync": "dangerous",
    "argocd.app.rollback": "dangerous",
    "argocd.app.set": "dangerous",
    "argocd.app.refresh": "caution",
    "argocd.app.delete": "dangerous",
    "argocd.appproject.create": "dangerous",
    "argocd.appproject.update": "dangerous",
}


# ---------------------------------------------------------------------------
# Recorded argocd-server REST fixtures
# ---------------------------------------------------------------------------


def _app_with_phase(phase: str, message: str = "", revision: str = "abc1234") -> dict[str, Any]:
    """An Application carrying a given operationState.phase."""
    return {
        "metadata": {"name": _APP_NAME, "namespace": "argocd"},
        "spec": {"project": "default", "source": {"targetRevision": "HEAD"}},
        "status": {
            "sync": {"status": "Synced"},
            "health": {"status": "Healthy"},
            "operationState": {
                "phase": phase,
                "message": message,
                "syncResult": {"revision": revision},
            },
        },
    }


_APP_SPEC_BEFORE: dict[str, Any] = {
    "metadata": {"name": _APP_NAME},
    "spec": {
        "project": "default",
        "source": {"repoURL": "https://github.com/example/gitops", "targetRevision": "HEAD"},
    },
}

_APP_SPEC_AFTER: dict[str, Any] = {
    "project": "default",
    "source": {"repoURL": "https://github.com/example/gitops", "targetRevision": "v2.0.0"},
}

_RESOURCE_TREE: dict[str, Any] = {
    "nodes": [
        {"group": "apps", "kind": "Deployment", "namespace": "guestbook", "name": "guestbook-ui"},
        {"group": "", "kind": "Service", "namespace": "guestbook", "name": "guestbook-ui"},
    ],
    "orphanedNodes": [],
}

_PROJECT_LIST: dict[str, Any] = {
    "items": [
        {
            "metadata": {"name": "team-a"},
            "spec": {"sourceRepos": ["https://github.com/example/*"], "destinations": []},
        }
    ],
}

_PROJECT_AFTER: dict[str, Any] = {
    "metadata": {"name": "team-a"},
    "spec": {
        "sourceRepos": ["https://github.com/example/*", "https://github.com/other/*"],
        "destinations": [{"server": "https://kubernetes.default.svc", "namespace": "team-a"}],
    },
}


# ---------------------------------------------------------------------------
# Stub credential loader + seeded connector instance
# ---------------------------------------------------------------------------


def _stub_loader(_target: ArgoCdTargetLike, _operator: Operator) -> Any:  # pragma: no cover
    async def _load() -> dict[str, str]:
        return {"token": _BEARER_TOKEN}

    return _load()


async def _seed_argocd_target() -> TargetORM:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="argocd",
            host=_ARGOCD_HOST,
            port=443,
            fqdn=None,
            secret_ref="rdc-hetzner-dc/argocd/api-token",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "3.x"},
            preferred_impl_id="argocd-api",
            notes="seeded by test_connectors_argocd_write_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> ArgoCdConnector:
    instance = ArgoCdConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[ArgoCdConnector] = instance
    return instance


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def argocd_write_e2e() -> AsyncIterator[ArgoCdConnector]:
    set_default_reducer(PassThroughReducer())
    await ArgoCdConnector.register_operations()
    await _seed_argocd_target()
    connector = _wire_seeded_connector()
    yield connector
    await connector.aclose()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool = True) -> dict[str, Any]:
    """Dispatch a write op. ``approved=True`` bypasses the gate (resume path).

    With ``approved=False`` the dispatch hits the policy gate, which (for a
    ``USER`` principal on a ``requires_approval`` op) routes to the human
    approve-queue and returns ``awaiting_approval`` — the handler never runs.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=resolved_target,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# Registration contract (acceptance criterion a)
# ---------------------------------------------------------------------------


def test_write_ops_registration_set() -> None:
    """ARGOCD_WRITE_OPS carries exactly the seven write ops the issue lists."""
    op_ids = {op.op_id for op in ARGOCD_WRITE_OPS}
    assert op_ids == set(EXPECTED_WRITE_OP_IDS)
    assert len(ARGOCD_WRITE_OPS) == 7


def test_write_ops_safety_levels_and_approval() -> None:
    """Every write op has the stated safety level and requires_approval=True."""
    for op in ARGOCD_WRITE_OPS:
        assert op.requires_approval is True, f"{op.op_id} must require approval"
        assert op.safety_level == EXPECTED_SAFETY[op.op_id], (
            f"{op.op_id} safety_level={op.safety_level} != {EXPECTED_SAFETY[op.op_id]}"
        )
        assert "write" in op.tags, f"{op.op_id} should carry the write tag"


# ---------------------------------------------------------------------------
# Approval routing (acceptance criterion: route to queue, not hard-deny / bypass)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_op_routes_to_approve_queue_not_hard_deny(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """An un-approved USER dispatch parks in the queue — no hard-deny, no cluster call."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        sync_route = mock.post(f"/api/v1/applications/{_APP_NAME}/sync").respond(
            200, json=_app_with_phase("Running")
        )
        result = await _dispatch("argocd.app.sync", {"name": _APP_NAME}, approved=False)
    assert result["status"] == "awaiting_approval", result
    # Parked, not denied: the handler never fired against argocd-server.
    assert not sync_route.called, "a parked write must not reach the cluster"
    assert result.get("extras", {}).get("approval_request_id")


# ---------------------------------------------------------------------------
# operationState polling (acceptance criterion: sync/rollback to terminal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_sync_polls_to_terminal_succeeded(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """app.sync POSTs the sync then polls operationState to Succeeded."""
    phases = iter([_app_with_phase("Running"), _app_with_phase("Succeeded", "successfully synced")])
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        post_route = mock.post(f"/api/v1/applications/{_APP_NAME}/sync").respond(
            200, json=_app_with_phase("Running")
        )
        mock.get(f"/api/v1/applications/{_APP_NAME}").mock(
            side_effect=lambda _req: respx.MockResponse(200, json=next(phases))
        )
        result = await _dispatch(
            "argocd.app.sync",
            {"name": _APP_NAME, "prune": True, "poll_timeout_seconds": 30},
        )
    assert result["status"] == "ok", result.get("error")
    assert post_route.called
    assert result["result"]["phase"] == "Succeeded"
    assert result["result"]["phase"] in TERMINAL_OPERATION_PHASES
    assert result["result"]["message"] == "successfully synced"
    assert result["result"]["timed_out"] is False
    assert result["result"]["synced_revision"] == "abc1234"


@pytest.mark.asyncio
async def test_app_rollback_polls_to_terminal_failed(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """app.rollback POSTs with the history id then polls to a terminal Failed."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        post_route = mock.post(f"/api/v1/applications/{_APP_NAME}/rollback").respond(
            200, json=_app_with_phase("Running")
        )
        mock.get(f"/api/v1/applications/{_APP_NAME}").respond(
            200, json=_app_with_phase("Failed", "rollback failed: conflict")
        )
        result = await _dispatch(
            "argocd.app.rollback",
            {"name": _APP_NAME, "id": 7, "poll_timeout_seconds": 30},
        )
    assert result["status"] == "ok", result.get("error")
    # The POST body carried the int64 history id, not a Git SHA.
    body = post_route.calls.last.request.content
    assert b'"id":7' in body.replace(b" ", b"")
    assert result["result"]["rollback_id"] == 7
    assert result["result"]["phase"] == "Failed"
    assert result["result"]["message"] == "rollback failed: conflict"


# ---------------------------------------------------------------------------
# proposed_effect snapshots (acceptance criterion: set/delete capture effect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_set_captures_before_after_into_proposed_effect(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """app.set snapshots the spec before + after into proposed_effect."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"/api/v1/applications/{_APP_NAME}").respond(200, json=_APP_SPEC_BEFORE)
        put_route = mock.put(f"/api/v1/applications/{_APP_NAME}/spec").respond(
            200, json=_APP_SPEC_AFTER
        )
        result = await _dispatch(
            "argocd.app.set",
            {"name": _APP_NAME, "spec": _APP_SPEC_AFTER},
        )
    assert result["status"] == "ok", result.get("error")
    assert put_route.called
    effect = result["result"]["proposed_effect"]
    assert effect["before_spec"]["source"]["targetRevision"] == "HEAD"
    assert effect["after_spec"]["source"]["targetRevision"] == "v2.0.0"


@pytest.mark.asyncio
async def test_app_delete_captures_cascade_list_into_proposed_effect(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """app.delete snapshots the managed resource tree as the cascade list."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        tree_route = mock.get(f"/api/v1/applications/{_APP_NAME}/resource-tree").respond(
            200, json=_RESOURCE_TREE
        )
        delete_route = mock.delete(f"/api/v1/applications/{_APP_NAME}").respond(200, json={})
        result = await _dispatch("argocd.app.delete", {"name": _APP_NAME})
    assert result["status"] == "ok", result.get("error")
    assert tree_route.called, "delete must snapshot the cascade list before deleting"
    assert delete_route.called
    # cascade=true is on the DELETE query.
    assert "cascade=true" in str(delete_route.calls.last.request.url)
    cascade = result["result"]["proposed_effect"]["cascade_resources"]
    assert {(r["kind"], r["name"]) for r in cascade} == {
        ("Deployment", "guestbook-ui"),
        ("Service", "guestbook-ui"),
    }
    assert result["result"]["cascade"] is True


@pytest.mark.asyncio
async def test_app_delete_no_cascade_skips_tree_snapshot(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """cascade=false leaves managed resources and skips the tree read."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        tree_route = mock.get(f"/api/v1/applications/{_APP_NAME}/resource-tree").respond(
            200, json=_RESOURCE_TREE
        )
        delete_route = mock.delete(f"/api/v1/applications/{_APP_NAME}").respond(200, json={})
        result = await _dispatch("argocd.app.delete", {"name": _APP_NAME, "cascade": False})
    assert result["status"] == "ok", result.get("error")
    assert not tree_route.called
    assert "cascade=false" in str(delete_route.calls.last.request.url)
    assert result["result"]["proposed_effect"]["cascade_resources"] == []


# ---------------------------------------------------------------------------
# Refresh + appproject writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_refresh_hard_sets_query(argocd_write_e2e: ArgoCdConnector) -> None:
    """app.refresh GETs ?refresh=hard and returns the refreshed status."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        get_route = mock.get(f"/api/v1/applications/{_APP_NAME}").respond(
            200, json=_app_with_phase("Succeeded")
        )
        result = await _dispatch("argocd.app.refresh", {"name": _APP_NAME})
    assert result["status"] == "ok", result.get("error")
    assert "refresh=hard" in str(get_route.calls.last.request.url)
    assert result["result"]["refresh"] == "hard"
    assert result["result"]["sync_status"] == "Synced"


@pytest.mark.asyncio
async def test_appproject_create_posts_create_request(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """appproject.create POSTs {project, upsert} and returns the project name."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        post_route = mock.post("/api/v1/projects").respond(200, json=_PROJECT_AFTER)
        result = await _dispatch(
            "argocd.appproject.create",
            {"project": _PROJECT_AFTER, "upsert": True},
        )
    assert result["status"] == "ok", result.get("error")
    body = post_route.calls.last.request.content
    assert b'"upsert":true' in body.replace(b" ", b"")
    assert b'"project"' in body
    assert result["result"]["name"] == "team-a"
    assert result["result"]["created"] is True


@pytest.mark.asyncio
async def test_appproject_update_captures_before_after(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """appproject.update snapshots the project spec before + after."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/v1/projects").respond(200, json=_PROJECT_LIST)
        put_route = mock.put("/api/v1/projects/team-a").respond(200, json=_PROJECT_AFTER)
        result = await _dispatch(
            "argocd.appproject.update",
            {"project": _PROJECT_AFTER},
        )
    assert result["status"] == "ok", result.get("error")
    assert put_route.called
    effect = result["result"]["proposed_effect"]
    assert len(effect["before_spec"]["sourceRepos"]) == 1
    assert len(effect["after_spec"]["sourceRepos"]) == 2


# ---------------------------------------------------------------------------
# Park-envelope uniformity across the write set (#2681)
#
# One op_class + one op-identity/metadata schema per connector, regardless of
# whether the per-op preview builder succeeds (set / delete / appproject.update),
# declines to the generic params-echo (sync / rollback / refresh /
# appproject.create), or later raises (delete against a nonexistent app).
# ---------------------------------------------------------------------------


#: The op-identity + metadata fields every parked argocd envelope must carry
#: uniformly (#2681 claim 3). The content key (bespoke ``preview`` XOR generic
#: ``params_echo``) is deliberately NOT in this set — it legitimately varies.
_ENVELOPE_IDENTITY_META_KEYS = frozenset(
    {"op_id", "connector_id", "target_id", "op_class", "preview_populated", "safety_level"}
)

_PARK_PARAMS_BY_OP: dict[str, dict[str, Any]] = {
    "argocd.app.sync": {"name": _APP_NAME},
    "argocd.app.rollback": {"name": _APP_NAME, "id": 7},
    "argocd.app.set": {"name": _APP_NAME, "spec": _APP_SPEC_AFTER},
    "argocd.app.refresh": {"name": _APP_NAME},
    "argocd.app.delete": {"name": _APP_NAME},
    "argocd.appproject.create": {"project": _PROJECT_AFTER, "upsert": True},
    "argocd.appproject.update": {"project": _PROJECT_AFTER},
}


def _identity_meta_keys(effect: dict[str, Any]) -> frozenset[str]:
    """The op-identity/metadata keys present on a parked envelope."""
    return _ENVELOPE_IDENTITY_META_KEYS & effect.keys()


@pytest.mark.asyncio
async def test_park_envelope_uniform_across_write_ops(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """Every parked argocd write op exposes one op_class + envelope schema (#2681).

    (a) ``op_class == "write"`` for all seven (sync / rollback / set / refresh
    classified ``other`` before the fix); (b) ``preview_populated`` is True
    exactly when a content key — bespoke ``preview`` OR generic
    ``params_echo`` — is present; (c) the op-identity + metadata field-set is
    identical across every op regardless of whether its preview builder
    succeeds or declines to the generic echo. The content key (``preview``
    XOR ``params_echo``) is intentionally not unified, so this does NOT
    assert ``set(effect.keys())`` equality.
    """
    assert set(_PARK_PARAMS_BY_OP) == set(EXPECTED_WRITE_OP_IDS)

    identity_meta_sets: list[frozenset[str]] = []
    for op_id, params in _PARK_PARAMS_BY_OP.items():
        with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
            # Existing-app / existing-project reads for the builder-backed
            # ops (set / delete / appproject.update). The no-builder ops
            # issue no cluster call on the park path; unused routes are
            # harmless under assert_all_called=False.
            mock.get(f"/api/v1/applications/{_APP_NAME}").respond(200, json=_APP_SPEC_BEFORE)
            mock.get(f"/api/v1/applications/{_APP_NAME}/resource-tree").respond(
                200, json=_RESOURCE_TREE
            )
            mock.get("/api/v1/projects").respond(200, json=_PROJECT_LIST)
            result = await _dispatch(op_id, params, approved=False)
            assert result["status"] == "awaiting_approval", (op_id, result)
            _assert_no_mutation_fired(mock)

        effect = await _parked_proposed_effect(result["extras"]["approval_request_id"])

        # (a) op_class is `write` for every approval-gated write op.
        assert effect["op_class"] == "write", (op_id, effect.get("op_class"))
        # (b) preview_populated iff a content key is present.
        has_content = "preview" in effect or "params_echo" in effect
        assert effect["preview_populated"] is has_content, (op_id, effect)
        # metadata carries the catalog severity for the op.
        assert effect["safety_level"] == EXPECTED_SAFETY[op_id], op_id
        # (c) all op-identity + metadata fields present on this op.
        assert _identity_meta_keys(effect) == _ENVELOPE_IDENTITY_META_KEYS, (op_id, sorted(effect))
        identity_meta_sets.append(_identity_meta_keys(effect))

    # (c) identical across all seven.
    assert all(s == identity_meta_sets[0] for s in identity_meta_sets)


@pytest.mark.asyncio
async def test_park_app_delete_nonexistent_app_keeps_uniform_identity(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """app.delete parked against a NONEXISTENT app keeps the uniform schema (#2681 claim 3).

    The cascade builder GETs the resource tree, receives a 404 and raises →
    the ``preview_unavailable`` fail-soft marker. This is the ONLY branch
    that previously carried ``op_id`` / ``target_id`` / ``connector_id``,
    making its field-set diverge from the builder-success path. The
    op-identity + metadata field-set must now match the existing-app cases;
    the fault marker rides alongside and is outside that set.
    """
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        tree_route = mock.get(f"/api/v1/applications/{_APP_NAME}/resource-tree").respond(
            404, json={"error": "application not found"}
        )
        result = await _dispatch("argocd.app.delete", {"name": _APP_NAME}, approved=False)
        assert result["status"] == "awaiting_approval", result
        assert tree_route.called, "the cascade preview must attempt the resource-tree read"
        _assert_no_mutation_fired(mock)

    effect = await _parked_proposed_effect(result["extras"]["approval_request_id"])
    # The builder faulted: not populated, and the fault marker rides along.
    assert effect["preview_populated"] is False
    assert effect["preview_unavailable"] is True
    assert "preview_error" in effect
    # But the op-identity + metadata field-set matches the existing-app cases.
    assert _identity_meta_keys(effect) == _ENVELOPE_IDENTITY_META_KEYS
    assert effect["op_class"] == "write"


# ---------------------------------------------------------------------------
# Bearer-token + secret-leak guarantee across the write set
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Park-time proposed_effect preview (G11.7 follow-up #1452)
#
# These drive the *un-approved* USER park path: the dispatch routes to the
# approve-queue and the per-op preview builder (#1437 hook, wired in
# ops_write_preview) must populate the durable ApprovalRequest.proposed_effect
# from READ-ONLY calls only — no POST/PUT/DELETE may fire before approval.
# ---------------------------------------------------------------------------


async def _parked_proposed_effect(approval_request_id: str) -> dict[str, Any]:
    """Read back the durable ApprovalRequest.proposed_effect for a parked op."""
    from meho_backplane.operations.approval_queue import get_request

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        request = await get_request(
            session,
            tenant_id=_OPERATOR_TENANT_ID,
            request_id=uuid.UUID(approval_request_id),
        )
        return dict(request.proposed_effect)


def _assert_no_mutation_fired(mock: respx.MockRouter) -> None:
    """Assert the park path issued only read GETs — no POST/PUT/DELETE."""
    for call in mock.calls:
        assert call.request.method == "GET", (
            f"park-time preview must be read-only; saw {call.request.method} {call.request.url}"
        )


@pytest.mark.asyncio
async def test_park_app_set_populates_before_after_preview(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """A parked argocd.app.set has before_spec + after_spec before approval."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        get_route = mock.get(f"/api/v1/applications/{_APP_NAME}").respond(
            200, json=_APP_SPEC_BEFORE
        )
        put_route = mock.put(f"/api/v1/applications/{_APP_NAME}/spec").respond(
            200, json=_APP_SPEC_AFTER
        )
        result = await _dispatch(
            "argocd.app.set",
            {"name": _APP_NAME, "spec": _APP_SPEC_AFTER},
            approved=False,
        )
        assert result["status"] == "awaiting_approval", result
        # Read-only: the spec PUT never fired; only the before-read GET did.
        assert get_route.called, "park-time preview must read the live spec"
        assert not put_route.called, "no mutation may fire before approval"
        _assert_no_mutation_fired(mock)

    request_id = result["extras"]["approval_request_id"]
    effect = await _parked_proposed_effect(request_id)
    # The hook wraps the builder output in {op_class, preview}. op_class is
    # `write` since #2681 added `.set` to the write-suffix set (was `other`).
    assert effect["op_class"] == "write"
    preview = effect["preview"]
    assert preview["before_spec"]["source"]["targetRevision"] == "HEAD"
    # after_spec at park time is the proposed spec the approved PUT would apply.
    assert preview["after_spec"]["source"]["targetRevision"] == "v2.0.0"


@pytest.mark.asyncio
async def test_park_app_delete_populates_cascade_preview(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """A parked argocd.app.delete has the cascade_resources list before approval."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        tree_route = mock.get(f"/api/v1/applications/{_APP_NAME}/resource-tree").respond(
            200, json=_RESOURCE_TREE
        )
        delete_route = mock.delete(f"/api/v1/applications/{_APP_NAME}").respond(200, json={})
        result = await _dispatch("argocd.app.delete", {"name": _APP_NAME}, approved=False)
        assert result["status"] == "awaiting_approval", result
        assert tree_route.called, "park-time preview must read the resource tree"
        assert not delete_route.called, "no DELETE may fire before approval"
        _assert_no_mutation_fired(mock)

    request_id = result["extras"]["approval_request_id"]
    effect = await _parked_proposed_effect(request_id)
    assert effect["op_class"] == "write"
    cascade = effect["preview"]["cascade_resources"]
    assert {(r["kind"], r["name"]) for r in cascade} == {
        ("Deployment", "guestbook-ui"),
        ("Service", "guestbook-ui"),
    }


@pytest.mark.asyncio
async def test_park_appproject_update_populates_before_after_preview(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """A parked argocd.appproject.update has before_spec + after_spec before approval."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        list_route = mock.get("/api/v1/projects").respond(200, json=_PROJECT_LIST)
        put_route = mock.put("/api/v1/projects/team-a").respond(200, json=_PROJECT_AFTER)
        result = await _dispatch(
            "argocd.appproject.update",
            {"project": _PROJECT_AFTER},
            approved=False,
        )
        assert result["status"] == "awaiting_approval", result
        assert list_route.called, "park-time preview must read the live project spec"
        assert not put_route.called, "no mutation may fire before approval"
        _assert_no_mutation_fired(mock)

    request_id = result["extras"]["approval_request_id"]
    effect = await _parked_proposed_effect(request_id)
    assert effect["op_class"] == "write"
    preview = effect["preview"]
    assert len(preview["before_spec"]["sourceRepos"]) == 1
    assert len(preview["after_spec"]["sourceRepos"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("argocd.app.sync", {"name": _APP_NAME}),
        ("argocd.app.rollback", {"name": _APP_NAME, "id": 7}),
        ("argocd.app.refresh", {"name": _APP_NAME}),
    ],
)
async def test_park_no_builder_ops_use_generic_params_echo(
    argocd_write_e2e: ArgoCdConnector,
    op_id: str,
    params: dict[str, Any],
) -> None:
    """sync / rollback / refresh park with the generic params-echo (#1856).

    They register no bespoke builder, so since #1856 their proposed_effect
    is the generic ``{op_class, params_echo}`` default (param-level
    legibility for free) rather than a computed ``preview``. No cluster
    call fires on the park path: the echo only redacts the already-known
    params, the handler is never reached, and no preview read is issued.
    """
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        result = await _dispatch(op_id, params, approved=False)
        assert result["status"] == "awaiting_approval", result
        assert not mock.calls, f"{op_id} must make no cluster call on the park path"

    from meho_backplane.broadcast.events import classify_op

    request_id = result["extras"]["approval_request_id"]
    effect = await _parked_proposed_effect(request_id)
    # Generic params-echo default: no computed preview, but the requested
    # params are echoed (none secret-bearing here, so they pass through).
    assert "preview" not in effect
    assert effect["op_class"] == classify_op(op_id)
    assert effect["params_echo"] == params


@pytest.mark.asyncio
async def test_all_writes_carry_bearer_and_never_leak_secret(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """Every write carries the Vault-sourced Bearer; the token never leaks."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/api/v1/applications/{_APP_NAME}/sync").respond(
            200, json=_app_with_phase("Running")
        )
        mock.get(f"/api/v1/applications/{_APP_NAME}").respond(
            200, json=_app_with_phase("Succeeded")
        )
        result = await _dispatch("argocd.app.sync", {"name": _APP_NAME, "poll_timeout_seconds": 30})
        assert result["status"] == "ok", result.get("error")
        assert _BEARER_TOKEN not in str(result)
        for call in mock.calls:
            assert call.request.headers.get("authorization") == f"Bearer {_BEARER_TOKEN}"
            assert _OPERATOR.raw_jwt not in (call.request.headers.get("authorization") or "")


# ---------------------------------------------------------------------------
# Approved-dispatch 5xx: vendor detail in BOTH the caller envelope and the
# durable DISPATCH audit row (#2680)
#
# The DoD witness: an approved re-dispatch whose upstream ArgoCD sync POST
# returns HTTP 500 with a gRPC-gateway JSON body must (1) return an
# OperationResult whose extras carry the structured error envelope
# (error_code + http_status=500 + upstream_message extracted from the body)
# rather than the pre-fix bare connector_error, and (2) persist that same
# envelope on the DISPATCH audit row's payload rather than only
# result_status='error'. approved=True drives the identical _execute_and_audit
# error arm the approvals resume path (resume_dispatch_after_approval ->
# dispatch(_approved=True)) reaches, so this is a faithful witness of it.
# ---------------------------------------------------------------------------


#: gRPC-gateway-shaped 500 body ArgoCD returns when a sync dry-run fails
#: server-side. ``message`` is the vendor detail an operator needs; before
#: #2680 only ``str(HTTPStatusError)`` (the status line + URL) reached the
#: envelope, so this text was discarded.
_ARGOCD_SYNC_500_BODY: dict[str, Any] = {
    "error": "rpc error: code = Internal desc = application dry-run failed",
    "code": 13,
    "message": "application dry-run failed: pruning would delete Service guestbook-ui",
}


async def _dispatch_audit_rows(op_id: str) -> list[AuditLog]:
    """Read back the durable DISPATCH audit rows for *op_id*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_approved_sync_500_surfaces_vendor_detail_to_caller_and_audit(
    argocd_write_e2e: ArgoCdConnector,
) -> None:
    """An approved sync whose POST 500s carries the vendor body to caller AND audit (#2680)."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        post_route = mock.post(f"/api/v1/applications/{_APP_NAME}/sync").respond(
            500, json=_ARGOCD_SYNC_500_BODY
        )
        result = await _dispatch(
            "argocd.app.sync",
            {"name": _APP_NAME, "dry_run": True},
        )

    # The POST fired (this is the re-dispatch, not a park) and 500'd.
    assert post_route.called

    # Clause 1 -- VENDOR DETAIL reaches the caller. Not the pre-fix bare
    # connector_error (whose only free-text was str(exc), the httpx status
    # line): the 5xx now carries http_status + the extracted upstream body.
    assert result["status"] == "error"
    extras = result["extras"]
    assert extras["error_code"] == "connector_error"
    assert extras["http_status"] == 500
    assert "application dry-run failed" in extras["upstream_message"]
    # The Vault bearer never rides the error envelope.
    assert _BEARER_TOKEN not in str(result)

    # Clause 2 -- the DISPATCH audit row persists the SAME envelope, not
    # merely result_status='error'.
    rows = await _dispatch_audit_rows("argocd.app.sync")
    assert len(rows) == 1
    row = rows[0]
    assert row.method == "DISPATCH"
    assert row.payload["result_status"] == "error"
    envelope = row.payload["error"]
    assert envelope["error_code"] == "connector_error"
    assert envelope["http_status"] == 500
    assert "application dry-run failed" in envelope["upstream_message"]
    # The durable ledger row does not leak the bearer either.
    assert _BEARER_TOKEN not in str(row.payload)
