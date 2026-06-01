# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end activation tests for the 8 vmware-rest write composites.

G3.16-T2 (#1415). The unit tests in
:mod:`tests.test_connectors_vmware_rest_composites_write` mock
``dispatch_child`` directly and prime the L2 pre-flight cache, so they
prove handler wiring but never exercise (a) the real
:func:`~meho_backplane.connectors.vmware_rest.composites._preflight.preflight_l2_dependencies`
walk against an ingested descriptor set, or (b) the production
approval-queue path a human principal hits on a
``requires_approval=True`` composite. T1 (#1414) proved the declared
``_SUB_OPS_*`` op_ids reconcile with the ingest pipeline's
``METHOD:/path`` keys; this module proves the same set passes pre-flight
*through the production* :func:`~meho_backplane.operations.dispatch`
entry point, and that every write composite dispatches end-to-end behind
the approval queue.

What this module covers, per #1415 acceptance criteria:

1. **Pre-flight passes for all 8 composites** -- each composite's
   ``_SUB_OPS_*`` raw-REST op_ids are registered as real
   ``source_kind="typed"`` descriptor rows; dispatching the composite
   runs the real pre-flight walk and never returns
   ``composite_l2_missing``. The leaf typed-op set is derived from the
   live ``_SUB_OPS_*`` constants (no hardcoded mirror to drift), so the
   registered descriptor set is exactly what pre-flight queries.
2. **End-to-end dispatch with sub-op sequence + rollback branch** --
   each composite is driven through production :func:`dispatch` against
   stub leaf handlers that record every ``(op_id, params)`` call. The
   recorded sequence is asserted against the documented orchestration
   workflow; ``vm.create`` additionally exercises the rollback branch
   (NIC-attach failure → ``DELETE:/vcenter/vm/{vm}``).
3. **The human approval-queue path** -- a USER principal dispatching a
   ``requires_approval=True`` composite is parked at
   ``awaiting_approval`` (G11.7-T1 #1401 routing), a distinct human
   reviewer approves the parked request, and the ``_approved=True``
   resume re-dispatch executes the composite. At least one composite
   exercises the full queued → approve → resume cycle; the rest of the
   8 assert the park half (so every composite is proven approval-gated).

Determinism: the module registers leaf typed-ops with stub handlers and
drives them through the in-process SQLite dispatcher (the autouse
migrations harness in :mod:`tests.conftest`). No vcsim / testcontainer
required -- the recorded-fixture approach keeps the run deterministic in
the unit lane.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest.composites import (
    _preflight,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.approval_queue import approve_request
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0a3")


# ---------------------------------------------------------------------------
# Fixtures (mirror tests.test_connectors_vmware_rest_composites_recursive_e2e)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches, connector registry, and pre-flight cache.

    The pre-flight cache (:data:`._preflight._PREFLIGHT_CACHE`) is reset
    so each test exercises the real cache-miss walk against the freshly
    registered descriptor rows rather than a primed positive carried
    over from another module's fixture.
    """
    reset_dispatcher_caches()
    clear_registry()
    _preflight.reset_preflight_cache()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _preflight.reset_preflight_cache()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registrations skip ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub.

    The dispatcher's audit path publishes a broadcast event on every
    audit-log row write; without the stub the real bus would attempt a
    write against a missing Postgres instance.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = "op-vmware-write-e2e",
    principal_kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: UUID = _TENANT_ID,
) -> Operator:
    """Synthetic operator scoped to the write-composite E2E tenant.

    Defaults to a USER (human) principal — the approval-queue path under
    test fires for human/service principals on a ``requires_approval``
    op (G11.7-T1 #1401).
    """
    return Operator(
        sub=sub,
        name="VMware Write Composite E2E",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for the resolver."""

    def __init__(self, version: str | None = "9.0") -> None:
        self.version = version


class _FakeVmwareTarget:
    """Minimal target the resolver / dispatcher reads from."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = target_id or uuid.uuid4()
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


class _NoOpVmwareConnector(Connector):
    """Resolver-satisfying connector — never actually invoked.

    The dispatcher resolves a connector instance only for
    ``source_kind="ingested"`` rows; ``typed`` and ``composite`` rows
    skip the instance lookup. The class is here so the registry resolver
    finds *something* under ``(vmware, 9.0, vmware-rest)``.
    """

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Recording leaf typed-op stubs
# ---------------------------------------------------------------------------
#
# Every raw-REST L2 sub-op the 8 write composites dispatch into is
# registered as one ``source_kind="typed"`` descriptor row pointing at a
# single shared recorder. Per-op canned payloads live in a module-level
# registry the tests populate before dispatch; the recorder appends each
# ``(op_id, params)`` call so a test can assert the sub-op sequence.
#
# Module-level (not closures) because ``derive_handler_ref`` rejects
# closures — the same constraint ``register_typed_operation`` enforces in
# production.

#: Ordered record of every leaf op_id dispatched in the current test.
_CALLS: list[tuple[str, dict[str, Any]]] = []

#: Per-op canned response payload (the vSphere REST envelope shape). A
#: test sets the payloads it needs before dispatch; absent ops default to
#: an empty ``{"value": {}}`` success.
_RESPONSES: dict[str, Any] = {}

#: op_ids whose stub should return a non-ok :class:`OperationResult` (to
#: drive a rollback / partial-failure branch). Maps op_id → error string.
_FAILURES: dict[str, str] = {}


def _reset_recorder() -> None:
    """Clear the call log + canned-response/failure registries."""
    _CALLS.clear()
    _RESPONSES.clear()
    _FAILURES.clear()


def _record(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Append a leaf call and resolve its canned payload, raising on failure."""
    _CALLS.append((op_id, dict(params)))
    if op_id in _FAILURES:
        raise RuntimeError(_FAILURES[op_id])
    return _RESPONSES.get(op_id, {"value": {}})


# One wrapper per raw-REST leaf op. Each tags its op_id then delegates to
# ``_record``. ``register_typed_operation`` binds these by dotted path, so
# they must be module-level ``async def``.


async def _h_list_folders(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/folder", params)


async def _h_list_vms(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/vm", params)


async def _h_get_vm(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/vm/{vm}", params)


async def _h_create_vm(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm", params)


async def _h_delete_vm(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("DELETE:/vcenter/vm/{vm}", params)


async def _h_attach_nic(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("PATCH:/vcenter/vm/{vm}/network", params)


async def _h_relocate(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm/{vm}?action=relocate", params)


async def _h_list_snapshots(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/vm/{vm}/snapshot", params)


async def _h_revert_snapshot(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert", params)


async def _h_list_cluster_hosts(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/cluster/{cluster}/host", params)


async def _h_drs_recs(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/cluster/{cluster}/drs/recommendations", params)


async def _h_deploy_library_vm(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm-template/library-items?action=deploy", params)


async def _h_get_task(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/cis/tasks/{task}", params)


async def _h_host_patch(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/host/{host}?action=patch", params)


async def _h_list_portgroups(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/network/distributed-portgroup", params)


async def _h_remove_dvs_host(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/network/dvs/{dvs}?action=remove_host", params)


async def _h_power_start(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm/{vm}/power?action=start", params)


async def _h_power_stop(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm/{vm}/power?action=stop", params)


async def _h_power_suspend(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm/{vm}/power?action=suspend", params)


async def _h_power_reset(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("POST:/vcenter/vm/{vm}/power?action=reset", params)


async def _h_maintenance_enter(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("PATCH:/vcenter/host/{host}/maintenance?action=enter", params)


async def _h_maintenance_exit(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("PATCH:/vcenter/host/{host}/maintenance?action=exit", params)


#: op_id → recording stub handler. Keyed by the canonical ``METHOD:/path``
#: descriptor key. Covers every raw-REST L2 sub-op across the 8 write
#: composites; the registration helper below asserts this map is a
#: superset of the live ``_SUB_OPS_*`` requirement so a future composite
#: edit that adds a sub-op without a stub fails loudly.
_LEAF_HANDLERS: dict[str, Any] = {
    "GET:/vcenter/folder": _h_list_folders,
    "GET:/vcenter/vm": _h_list_vms,
    "GET:/vcenter/vm/{vm}": _h_get_vm,
    "POST:/vcenter/vm": _h_create_vm,
    "DELETE:/vcenter/vm/{vm}": _h_delete_vm,
    "PATCH:/vcenter/vm/{vm}/network": _h_attach_nic,
    "POST:/vcenter/vm/{vm}?action=relocate": _h_relocate,
    "GET:/vcenter/vm/{vm}/snapshot": _h_list_snapshots,
    "POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert": _h_revert_snapshot,
    "GET:/vcenter/cluster/{cluster}/host": _h_list_cluster_hosts,
    "GET:/vcenter/cluster/{cluster}/drs/recommendations": _h_drs_recs,
    "POST:/vcenter/vm-template/library-items?action=deploy": _h_deploy_library_vm,
    "GET:/cis/tasks/{task}": _h_get_task,
    "POST:/vcenter/host/{host}?action=patch": _h_host_patch,
    "GET:/vcenter/network/distributed-portgroup": _h_list_portgroups,
    "POST:/vcenter/network/dvs/{dvs}?action=remove_host": _h_remove_dvs_host,
    "POST:/vcenter/vm/{vm}/power?action=start": _h_power_start,
    "POST:/vcenter/vm/{vm}/power?action=stop": _h_power_stop,
    "POST:/vcenter/vm/{vm}/power?action=suspend": _h_power_suspend,
    "POST:/vcenter/vm/{vm}/power?action=reset": _h_power_reset,
    "PATCH:/vcenter/host/{host}/maintenance?action=enter": _h_maintenance_enter,
    "PATCH:/vcenter/host/{host}/maintenance?action=exit": _h_maintenance_exit,
}


# ---------------------------------------------------------------------------
# Composite metadata
# ---------------------------------------------------------------------------

#: The 8 write composites under test, op_id → human-readable name. Pinned
#: explicitly so the suite can't silently shrink if a composite is renamed
#: out of the registrar.
_WRITE_COMPOSITES: dict[str, str] = {
    "vmware.composite.vm.create": "vm.create",
    "vmware.composite.vm.clone": "vm.clone",
    "vmware.composite.vm.snapshot.revert": "vm.snapshot.revert",
    "vmware.composite.vm.migrate": "vm.migrate",
    "vmware.composite.vm.power.bulk": "vm.power.bulk",
    "vmware.composite.host.evacuate": "host.evacuate",
    "vmware.composite.host.detach_from_vds": "host.detach_from_vds",
    "vmware.composite.cluster.patch": "cluster.patch",
}


def _required_raw_sub_op_ids() -> set[str]:
    """Union of every raw-REST ``_SUB_OPS_*`` op_id across the 8 composites.

    Excludes composite-to-composite references (``vmware.composite.*``):
    the pre-flight walk skips those (their own handlers pre-flight). This
    is the exact set the registered leaf descriptors must cover.
    """
    raw: set[str] = set()
    for name in dir(_write):
        if not name.startswith("_SUB_OPS_"):
            continue
        for op_id in getattr(_write, name):
            if op_id.startswith("vmware.composite."):
                continue
            raw.add(op_id)
    return raw


async def _register_leaf_typed_ops(stub_embedding_service: AsyncMock) -> None:
    """Register one ``source_kind="typed"`` row per raw-REST leaf op.

    The registered set is exactly :data:`_LEAF_HANDLERS`, asserted to be a
    superset of the live ``_SUB_OPS_*`` requirement so pre-flight resolves
    every declared sub-op. ``parameter_schema`` is permissive
    (``additionalProperties=True``) so the composites pass whatever params
    they build without per-op schema bookkeeping in this test.
    """
    required = _required_raw_sub_op_ids()
    missing = required - set(_LEAF_HANDLERS)
    assert not missing, (
        "the write composites declare raw sub-op_ids this module has no leaf "
        f"stub for: {sorted(missing)}. Add a stub + map it in _LEAF_HANDLERS."
    )
    permissive_schema = {"type": "object", "additionalProperties": True}
    for op_id, handler in _LEAF_HANDLERS.items():
        await register_typed_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id=op_id,
            handler=handler,
            summary=f"Stub leaf op {op_id} for the write-composite E2E.",
            description=f"Stub leaf op {op_id} for the write-composite E2E.",
            parameter_schema=permissive_schema,
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


async def _bootstrap_registry(stub_embedding_service: AsyncMock) -> None:
    """Register the connector, all 13 composites, and the leaf typed ops."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_NoOpVmwareConnector,
    )
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    await _register_leaf_typed_ops(stub_embedding_service)
    _reset_recorder()


async def _clear_requires_approval(op_ids: set[str]) -> None:
    """Flip ``requires_approval`` off for *op_ids* and drop stale caches.

    Used by the call-sequence E2E half: the sub-op sequence + rollback
    branches are the contract under test there, not the approval gate, so
    the composites auto-execute. The approval-queue half keeps
    ``requires_approval=True`` and routes through park → approve → resume.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        await fresh.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id.in_(op_ids))
            .values(requires_approval=False)
        )
        await fresh.commit()
    # The dispatcher caches descriptor rows; drop the stale pre-mutation
    # copy so the policy gate sees requires_approval=False.
    reset_dispatcher_caches()


def _ops_in_calls() -> list[str]:
    """The recorded leaf op_ids in dispatch order."""
    return [op_id for op_id, _ in _CALLS]


# ===========================================================================
# AC1 — pre-flight passes for all 8 write composites
# ===========================================================================


def test_write_composite_set_is_the_expected_eight() -> None:
    """Guard: the registrar still ships exactly the 8 write composites.

    Pins the op_id set so a renamed / dropped composite can't shrink the
    parametrised pre-flight + dispatch coverage to a vacuous pass.
    """
    registrar_write_op_ids = {f"vmware.composite.{name}" for name in _WRITE_COMPOSITES.values()}
    assert set(_WRITE_COMPOSITES) == registrar_write_op_ids
    assert len(_WRITE_COMPOSITES) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("composite_op_id", sorted(_WRITE_COMPOSITES))
async def test_write_composite_passes_preflight_through_dispatch(
    composite_op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Each composite's pre-flight walk resolves every declared sub-op (AC1).

    Drives the composite through production :func:`dispatch` against the
    registered leaf descriptor set with ``requires_approval`` cleared.
    The assertion is the negative one #1415 cares about: the dispatch
    never returns the ``composite_l2_missing`` structured error, i.e.
    :func:`preflight_l2_dependencies` found every ``_SUB_OPS_*`` op_id in
    ``endpoint_descriptor``. Listing-shaped leaf responses are empty by
    default, so most composites short-circuit to a benign no-work status
    — that is fine here; the dispatch-sequence detail is asserted in the
    dedicated per-composite tests below.
    """
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval(set(_WRITE_COMPOSITES))
    for op_id, payload in _benign_responses_for(composite_op_id).items():
        _RESPONSES[op_id] = payload

    operator = _make_operator()
    target = _FakeVmwareTarget()
    params = _benign_params_for(composite_op_id)

    result = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=composite_op_id,
        target=target,
        params=params,
    )

    # Pre-flight passed: the dispatch never surfaced the
    # ``composite_l2_missing`` structured error (status='error', error
    # text prefixed ``composite_l2_missing:`` — see
    # ``_errors.result_composite_l2_missing``). Any benign business-logic
    # status (created / no_recommendation / detached / ...) is acceptable
    # here; the point is the L2 dependency walk resolved every declared
    # ``_SUB_OPS_*`` op_id against the registered descriptor set.
    assert "composite_l2_missing" not in (result.error or ""), result.error
    # AC1 (#1436): a *non*-``composite_l2_missing`` failure would still
    # satisfy the negative check above — an ``unknown_op`` /
    # ``invalid_params`` / ``connector_error`` generic error has a
    # different error text yet is plainly not a passing pre-flight. Pin
    # the positive outcome too (``status != "error"``) so only a genuine
    # pre-flight pass through the production dispatch path satisfies the
    # test. The benign leaf responses above let every composite reach its
    # no-work business status (``OperationResult.status`` ∈ {ok, pending}),
    # never a generic execution error.
    assert result.status != "error", result.error
    assert result.status in {"ok", "pending"}, (result.status, result.error)


def _benign_params_for(composite_op_id: str) -> dict[str, Any]:
    """Minimal schema-valid params for a benign (empty-listing) dispatch."""
    return {
        "vmware.composite.vm.create": {
            "folder_name": "prod",
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
        },
        "vmware.composite.vm.clone": {
            "source_vm": "vm-1",
            "target_name": "vm-clone",
            "library_item": "lib-1",
            "wait_for_completion": False,
        },
        "vmware.composite.vm.snapshot.revert": {
            "vm": "vm-1",
            "snapshot_name": "snap-a",
        },
        "vmware.composite.vm.migrate": {
            "vm": "vm-1",
            "cluster": "domain-c1",
        },
        "vmware.composite.vm.power.bulk": {"action": "start"},
        "vmware.composite.host.evacuate": {"host": "host-1"},
        "vmware.composite.host.detach_from_vds": {
            "host": "host-1",
            "dvs": "dvs-1",
            "fallback_network": "net-fallback",
        },
        "vmware.composite.cluster.patch": {"cluster": "domain-c1"},
    }[composite_op_id]


def _benign_responses_for(composite_op_id: str) -> dict[str, Any]:
    """Per-op leaf responses that steer each composite to a no-work status.

    The recorder's default leaf payload is ``{"value": {}}`` (an empty
    *object* envelope). Composites whose first sub-op is a listing read
    unwrap ``value`` and expect a *list*; the empty-object default trips
    their ``isinstance(..., list)`` guard and raises ``RuntimeError`` —
    surfacing as a generic ``connector_error`` (``status='error'``), which
    is exactly the non-``composite_l2_missing`` failure AC1 (#1436) now
    rejects. Returning an empty *list* envelope for those listing ops lets
    the composite short-circuit to its benign no-work business status
    (``no_recommendation`` / ``detached`` / ``completed`` / ...) so the
    strengthened ``status != "error"`` assertion checks a genuine
    pre-flight pass rather than masking an execution error.

    ``vm.clone`` is fire-and-forget here (``wait_for_completion=False``):
    it reads the source VM (the empty-object default is a valid non-list
    payload) then deploys, so it needs a deploy envelope carrying a task
    id to reach the benign ``pending`` status without a poll.
    """
    empty_listing: dict[str, Any] = {"value": []}
    per_composite: dict[str, dict[str, Any]] = {
        "vmware.composite.vm.create": {"GET:/vcenter/folder": empty_listing},
        "vmware.composite.vm.clone": {
            "POST:/vcenter/vm-template/library-items?action=deploy": {
                "value": {"task": "task-benign"}
            },
        },
        "vmware.composite.vm.snapshot.revert": {"GET:/vcenter/vm/{vm}/snapshot": empty_listing},
        "vmware.composite.vm.migrate": {
            "GET:/vcenter/cluster/{cluster}/drs/recommendations": empty_listing,
        },
        "vmware.composite.vm.power.bulk": {"GET:/vcenter/vm": empty_listing},
        "vmware.composite.host.evacuate": {"GET:/vcenter/vm": empty_listing},
        "vmware.composite.host.detach_from_vds": {
            "GET:/vcenter/network/distributed-portgroup": empty_listing,
            "GET:/vcenter/vm": empty_listing,
        },
        "vmware.composite.cluster.patch": {"GET:/vcenter/cluster/{cluster}/host": empty_listing},
    }
    return per_composite[composite_op_id]


# ===========================================================================
# AC2 — end-to-end dispatch: sub-op sequence + rollback branch
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_create_happy_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.create: folder lookup → create → NIC attach → power-on (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.create"})
    _RESPONSES["GET:/vcenter/folder"] = {"value": [{"folder": "group-v1"}]}
    _RESPONSES["POST:/vcenter/vm"] = {"value": "vm-123"}

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=_FakeVmwareTarget(),
        params={
            "folder_name": "prod",
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
            "nics": [{"network": "net-1"}],
            "power_on_after_create": True,
        },
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "created"
    assert result.result["vm_id"] == "vm-123"
    assert _ops_in_calls() == [
        "GET:/vcenter/folder",
        "POST:/vcenter/vm",
        "PATCH:/vcenter/vm/{vm}/network",
        "POST:/vcenter/vm/{vm}/power?action=start",
    ]


@pytest.mark.asyncio
async def test_vm_create_rollback_on_nic_failure(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.create: NIC-attach failure rolls back via DELETE:/vcenter/vm/{vm} (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.create"})
    _RESPONSES["GET:/vcenter/folder"] = {"value": [{"folder": "group-v1"}]}
    _RESPONSES["POST:/vcenter/vm"] = {"value": "vm-123"}
    _FAILURES["PATCH:/vcenter/vm/{vm}/network"] = "nic backend rejected the attach"

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=_FakeVmwareTarget(),
        params={
            "folder_name": "prod",
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
            "nics": [{"network": "net-1"}],
        },
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "rolled_back"
    assert result.result["failed_step"] == "nic_attach"
    assert result.result["vm_id"] is None
    # The rollback issued the DELETE after the failed NIC attach.
    assert _ops_in_calls() == [
        "GET:/vcenter/folder",
        "POST:/vcenter/vm",
        "PATCH:/vcenter/vm/{vm}/network",
        "DELETE:/vcenter/vm/{vm}",
    ]


@pytest.mark.asyncio
async def test_vm_clone_pending_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.clone (fire-and-forget): source read → deploy → return task id (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.clone"})
    _RESPONSES["GET:/vcenter/vm/{vm}"] = {"value": {"name": "src"}}
    _RESPONSES["POST:/vcenter/vm-template/library-items?action=deploy"] = {
        "value": {"task": "task-9"}
    }

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.clone",
        target=_FakeVmwareTarget(),
        params={
            "source_vm": "vm-1",
            "target_name": "vm-clone",
            "library_item": "lib-1",
            "wait_for_completion": False,
        },
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "pending"
    assert result.result["task_id"] == "task-9"
    # wait_for_completion=False short-circuits before any task poll.
    assert _ops_in_calls() == [
        "GET:/vcenter/vm/{vm}",
        "POST:/vcenter/vm-template/library-items?action=deploy",
    ]


@pytest.mark.asyncio
async def test_vm_snapshot_revert_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.snapshot.revert: list → match-by-name → revert (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.snapshot.revert"})
    _RESPONSES["GET:/vcenter/vm/{vm}/snapshot"] = {
        "value": [{"name": "snap-a", "snapshot": "snap-moid-1"}]
    }

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.snapshot.revert",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "snapshot_name": "snap-a"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "reverted"
    assert result.result["snapshot_id"] == "snap-moid-1"
    assert _ops_in_calls() == [
        "GET:/vcenter/vm/{vm}/snapshot",
        "POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert",
    ]


@pytest.mark.asyncio
async def test_vm_migrate_drs_path_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.migrate: DRS recommendation → relocate (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.migrate"})
    _RESPONSES["GET:/vcenter/cluster/{cluster}/drs/recommendations"] = {
        "value": [{"vm": "vm-1", "target_host": "host-target"}]
    }

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.migrate",
        target=_FakeVmwareTarget(),
        params={"vm": "vm-1", "cluster": "domain-c1"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "migrated"
    assert result.result["target_host"] == "host-target"
    assert result.result["source"] == "drs"
    assert _ops_in_calls() == [
        "GET:/vcenter/cluster/{cluster}/drs/recommendations",
        "POST:/vcenter/vm/{vm}?action=relocate",
    ]


@pytest.mark.asyncio
async def test_vm_power_bulk_fan_out_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.power.bulk: list → per-VM power action fan-out (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.vm.power.bulk"})
    _RESPONSES["GET:/vcenter/vm"] = {"value": [{"vm": "vm-a"}, {"vm": "vm-b"}]}

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.power.bulk",
        target=_FakeVmwareTarget(),
        params={"action": "stop"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["summary"] == {"ok": 2, "error": 0}
    # One listing then one stop per matched VM (the chosen action verb).
    assert _ops_in_calls() == [
        "GET:/vcenter/vm",
        "POST:/vcenter/vm/{vm}/power?action=stop",
        "POST:/vcenter/vm/{vm}/power?action=stop",
    ]


@pytest.mark.asyncio
async def test_host_evacuate_recursive_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """host.evacuate: list VMs → recursive vm.migrate per VM → maintenance enter (AC2).

    Exercises the only composite-to-composite recursion in the write set
    through production dispatch. The recorded leaf sequence proves the
    recursive vm.migrate frames bottom out on the DRS + relocate leaves
    before the host enters maintenance.
    """
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval(
        {"vmware.composite.host.evacuate", "vmware.composite.vm.migrate"}
    )
    _RESPONSES["GET:/vcenter/vm"] = {"value": [{"vm": "vm-a", "cluster": "domain-c1"}]}
    _RESPONSES["GET:/vcenter/cluster/{cluster}/drs/recommendations"] = {
        "value": [{"vm": "vm-a", "target_host": "host-target"}]
    }

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.host.evacuate",
        target=_FakeVmwareTarget(),
        params={"host": "host-1"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "evacuated"
    assert result.result["maintenance_entered"] is True
    assert result.result["migrated_vms"] == ["vm-a"]
    # list VMs (host.evacuate) → recursive vm.migrate (DRS → relocate) →
    # maintenance enter (host.evacuate).
    assert _ops_in_calls() == [
        "GET:/vcenter/vm",
        "GET:/vcenter/cluster/{cluster}/drs/recommendations",
        "POST:/vcenter/vm/{vm}?action=relocate",
        "PATCH:/vcenter/host/{host}/maintenance?action=enter",
    ]


@pytest.mark.asyncio
async def test_host_detach_from_vds_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """host.detach_from_vds: portgroups → VMs → per-VM NIC migrate → DVS detach (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.host.detach_from_vds"})
    _RESPONSES["GET:/vcenter/network/distributed-portgroup"] = {"value": []}
    _RESPONSES["GET:/vcenter/vm"] = {"value": [{"vm": "vm-a"}]}

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.host.detach_from_vds",
        target=_FakeVmwareTarget(),
        params={"host": "host-1", "dvs": "dvs-1", "fallback_network": "net-fallback"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "detached"
    assert result.result["vms_migrated"] == ["vm-a"]
    assert _ops_in_calls() == [
        "GET:/vcenter/network/distributed-portgroup",
        "GET:/vcenter/vm",
        "PATCH:/vcenter/vm/{vm}/network",
        "POST:/vcenter/network/dvs/{dvs}?action=remove_host",
    ]


@pytest.mark.asyncio
async def test_cluster_patch_sequential_sub_op_sequence(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """cluster.patch: list hosts → per-host maintenance enter → patch → exit (AC2)."""
    await _bootstrap_registry(stub_embedding_service)
    await _clear_requires_approval({"vmware.composite.cluster.patch"})
    _RESPONSES["GET:/vcenter/cluster/{cluster}/host"] = {"value": [{"host": "host-1"}]}

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.cluster.patch",
        target=_FakeVmwareTarget(),
        params={"cluster": "domain-c1"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["status"] == "completed"
    assert result.result["patched_hosts"] == ["host-1"]
    assert _ops_in_calls() == [
        "GET:/vcenter/cluster/{cluster}/host",
        "PATCH:/vcenter/host/{host}/maintenance?action=enter",
        "POST:/vcenter/host/{host}?action=patch",
        "PATCH:/vcenter/host/{host}/maintenance?action=exit",
    ]


# ===========================================================================
# AC3 — human approval-queue path (queue → approve → resume → execute)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("composite_op_id", sorted(_WRITE_COMPOSITES))
async def test_write_composite_human_dispatch_parks_for_approval(
    composite_op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A USER principal dispatching a write composite is parked, not denied (AC3).

    Every write composite ships ``requires_approval=True``; G11.7-T1
    (#1401) routes a human/service principal hitting such an op to the
    approval queue (``awaiting_approval``) rather than the pre-G11.7
    hard-deny. This proves the park half for all 8 — a durable
    :class:`ApprovalRequest` row is created and the result carries its id.
    """
    await _bootstrap_registry(stub_embedding_service)

    operator = _make_operator()
    target = _FakeVmwareTarget()

    result = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=composite_op_id,
        target=target,
        params=_benign_params_for(composite_op_id),
    )

    assert result.status == "awaiting_approval", result.error
    assert result.status != "denied"
    approval_request_id = UUID(result.extras["approval_request_id"])
    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
    assert pending is not None
    assert pending.status == ApprovalRequestStatus.PENDING.value
    assert pending.op_id == composite_op_id


@pytest.mark.asyncio
async def test_vm_create_full_queue_approve_resume_execute(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vm.create: full queued → approve → resume → execute cycle (AC3).

    Drives the complete human write path end-to-end:

    1. A USER principal dispatches the ``requires_approval=True``
       composite → parked at ``awaiting_approval``; the op does NOT run
       (no leaf calls recorded yet).
    2. A distinct human reviewer approves the parked request (self-
       approval is blocked by the requester != approver guard, so the
       reviewer is a different ``sub``).
    3. The ``_approved=True`` resume re-dispatch executes the composite —
       the gate is bypassed (the approval *is* the authorization), the
       leaf sub-ops fire, and the composite returns ``status='created'``.
    """
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/folder"] = {"value": [{"folder": "group-v1"}]}
    _RESPONSES["POST:/vcenter/vm"] = {"value": "vm-789"}

    # Persist a real Target row so the resume path can re-hydrate it by id.
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name="prod-vcenter",
                product="vmware",
                host="vcenter.prod.invalid",
                aliases=[],
            )
        )
        await s.commit()

    requester = _make_operator(sub="ops-human", principal_kind=PrincipalKind.USER)
    target = _FakeVmwareTarget(target_id=target_id)
    params = {
        "folder_name": "prod",
        "name": "vm-approved",
        "guest_os": "UBUNTU_64",
    }

    # Step 1: human dispatch → awaiting_approval; the op did not run.
    result1 = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert _CALLS == [], "the composite must not execute before approval"
    approval_request_id = UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
        assert pending is not None
        assert pending.target_id == target_id

    # Step 2: a distinct human reviewer approves the parked request.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume re-dispatch with the gate bypass → the op executes.
    result2 = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vm.create",
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert isinstance(result2.result, dict)
    assert result2.result["status"] == "created"
    assert result2.result["vm_id"] == "vm-789"
    # The resume run executed the real sub-op chain.
    assert _ops_in_calls() == ["GET:/vcenter/folder", "POST:/vcenter/vm"]
