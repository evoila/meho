# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Proof plane for the add-on pairing contract (#3030, Initiative #2900).

The four contract planes each ship with their own unit tests; this file is the
*composition* proof — one reference add-on (:class:`tests.addon_reference_double.ReferenceAddon`)
that pairs, advertises a meta-tool family, produces and consumes step events for
its own work_refs, and drives a single audit-replay subtree, all in-process with
no external service. It pins the three acceptance criteria of #3030:

1. **Reference double exercises every contract plane end-to-end in CI** —
   :func:`test_reference_double_exercises_every_contract_plane`.
2. **Unpaired conformance** — a paired add-on advertising a meta-tool family
   grows *no* agent-surface tool, and unpairing returns the ``tools/list``
   wire output byte-identical to the never-paired baseline
   (:func:`test_advertising_meta_tool_family_grows_no_agent_surface`,
   :func:`test_full_pairing_lifecycle_leaves_toolslist_byte_identical`). The
   baseline is captured **live at test start**, not pinned to a literal tool
   list, so it stays correct as the working surface grows (e.g. when the
   sibling #3029 paired-surface-activation task lands).
3. **Cross-lineage isolation through the double** —
   :func:`test_step_events_are_scoped_to_each_doubles_own_lineage`.

The contract is documented in ``docs/codebase/addon-contract.md`` and its trust
model reviewed in ``docs/decisions/addon-contract-trust-model.md``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

import meho_backplane.main  # noqa: F401 — importing registers the full MCP tool surface
from meho_backplane.audit_query.replay import replay_session
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.handlers import handle_tools_list
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from meho_backplane.operations.addon_capability_schemas import CapabilityKind
from meho_backplane.operations.addon_orchestration import (
    ORCHESTRATION_METHOD,
    ORCHESTRATION_PATH,
)
from meho_backplane.settings import get_settings
from tests.addon_reference_double import ReferenceAddon

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _seed_tenant(tenant_id: uuid.UUID = _TENANT, slug: str = "tenant-a") -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=slug.title()))
            await session.commit()


def _widest_surface_operator() -> Operator:
    """An operator that sees the *entire* tool surface — the strictest baseline.

    ``tenant_admin`` role clears the role gate, ``mcp:admin`` scope lists the
    operator planes, and the ``meho-docs`` capability lights up the docs
    add-on tools. Any pairing-driven surface growth would have to appear in
    this listing, so pinning it byte-identical is the tightest conformance
    assertion.
    """
    return Operator(
        sub="op-conformance",
        raw_jwt="conformance-not-a-real-jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset({"meho-docs"}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


async def _canonical_toolslist(operator: Operator) -> str:
    """Serialize the ``tools/list`` wire output canonically for byte comparison."""
    result = await handle_tools_list(operator, None)
    return json.dumps(result, sort_keys=True)


# --------------------------------------------------------------------------- #
# Criterion 1: one double exercises every contract plane end to end
# --------------------------------------------------------------------------- #


async def test_reference_double_exercises_every_contract_plane() -> None:
    """Pair → advertise → produce/consume step events → single audit subtree.

    A single reference add-on drives all four planes in sequence, proving they
    compose in-process with no external service — the #3030 headline criterion.
    """
    await _seed_tenant()

    # Plane 1+2 — pair (real AddonPairingService, stubbed Keycloak) and
    # advertise a meta-tool family + an event kind (capability advertisement).
    addon = await ReferenceAddon.pair(
        tenant_id=_TENANT,
        name="reference-double",
        service_account_sub="svc-reference-double",
    )
    declaration = await addon.advertise_meta_tool_family(
        "provisioning", event_kinds=("run.step.completed",)
    )
    assert declaration.active is True
    assert {(c.kind, c.name) for c in declaration.capabilities} == {
        (CapabilityKind.META_TOOL_FAMILY, "provisioning"),
        (CapabilityKind.EVENT_KIND, "run.step.completed"),
    }

    # Plane 3 — produce three step events for the double's own work, then
    # consume them back over the durable, resumable subscription.
    work_ref = "gh:evoila/meho#3030"
    for i in range(3):
        await addon.produce_step_event(
            event_kind=f"run.step.{i}",
            work_ref=work_ref,
            payload={"step": i},
        )
    first_page = await addon.consume_step_events(after_seq=0, limit=2)
    assert [e.event_kind for e in first_page.items] == ["run.step.0", "run.step.1"]
    assert first_page.next_cursor is not None
    resumed = await addon.consume_step_events(after_seq=int(first_page.next_cursor))
    assert [e.event_kind for e in resumed.items] == ["run.step.2"]
    assert all(e.work_ref == work_ref for e in first_page.items + resumed.items)

    # Plane 4 — a multi-dispatch external run replays as ONE audit subtree.
    run = await addon.run_orchestration(
        work_ref=work_ref,
        dispatch_op_ids=["vmware-rest-9.0:vm.list", "vmware-rest-9.0:vm.power_on"],
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        roots = await replay_session(run.session_id, tenant_id=_TENANT, session=session)
    assert len(roots) == 1
    anchor = roots[0]
    assert anchor.method == ORCHESTRATION_METHOD
    assert anchor.path == ORCHESTRATION_PATH
    assert {child.path for child in anchor.children} == {
        "vmware-rest-9.0:vm.list",
        "vmware-rest-9.0:vm.power_on",
    }
    assert all(child.agent_session_id == run.session_id for child in anchor.children)

    # Teardown — unpair is reversible.
    assert await addon.unpair() is True


# --------------------------------------------------------------------------- #
# Criterion 2: unpaired conformance — the agent waist stays byte-identical
# --------------------------------------------------------------------------- #


async def test_advertising_meta_tool_family_grows_no_agent_surface() -> None:
    """A paired add-on advertising a meta-tool family registers no MCP tool.

    Capability advertisement is *data* (an ``addon_capability`` row), never a
    tool name on the agent waist (postulate 5). The ``tools/list`` wire output
    while paired-and-advertising is byte-identical to the baseline captured
    before pairing, and no registry tool is named for the advertised family.
    """
    await _seed_tenant()
    operator = _widest_surface_operator()
    baseline = await _canonical_toolslist(operator)

    addon = await ReferenceAddon.pair(tenant_id=_TENANT, name="reference-double")
    await addon.advertise_meta_tool_family("provisioning")

    # The advertised family name never becomes a tool name.
    offenders = sorted(name for name in mcp_registry._TOOLS if "provisioning" in name.lower())
    assert not offenders, f"advertised family leaked onto the agent surface: {offenders}"

    # And the whole listing is unchanged, byte for byte.
    assert await _canonical_toolslist(operator) == baseline


async def test_full_pairing_lifecycle_leaves_toolslist_byte_identical() -> None:
    """Unpaired backplane == never-paired backplane, across a full lifecycle.

    Capture the ``tools/list`` baseline (unpaired), run the double through
    every plane, unpair, and assert the wire output is byte-identical to the
    baseline. The baseline is captured live at test start rather than pinned to
    a literal tool list, so this stays correct as the working surface grows
    (e.g. once the sibling #3029 paired-surface-activation task lands).
    """
    await _seed_tenant()
    operator = _widest_surface_operator()
    baseline = await _canonical_toolslist(operator)

    addon = await ReferenceAddon.pair(tenant_id=_TENANT, name="reference-double")
    await addon.advertise_meta_tool_family("provisioning", event_kinds=("run.step.completed",))
    await addon.produce_step_event(
        event_kind="run.step.completed", work_ref="gh:evoila/meho#3030", payload={"ok": True}
    )
    await addon.consume_step_events()
    await addon.run_orchestration(work_ref="gh:evoila/meho#3030", dispatch_op_ids=["k8s:pod.list"])

    assert await addon.unpair() is True
    assert await _canonical_toolslist(operator) == baseline


# --------------------------------------------------------------------------- #
# Criterion 3: step events are scoped to each double's own lineage
# --------------------------------------------------------------------------- #


async def test_step_events_are_scoped_to_each_doubles_own_lineage() -> None:
    """Two doubles, one shared work_ref — each consumes only its own events.

    Driven end-to-end through the reference double (real pairing, real sub
    captured at pair time), so the isolation is proven on the same identity
    join production uses, not a hand-seeded ``service_account_sub``.
    """
    await _seed_tenant()
    addon_a = await ReferenceAddon.pair(
        tenant_id=_TENANT, name="reference-a", service_account_sub="svc-ref-a"
    )
    addon_b = await ReferenceAddon.pair(
        tenant_id=_TENANT, name="reference-b", service_account_sub="svc-ref-b"
    )

    shared_work_ref = "gh:evoila/meho#42"
    await addon_a.produce_step_event(
        event_kind="approval.approved", work_ref=shared_work_ref, payload={}
    )
    await addon_b.produce_step_event(
        event_kind="approval.rejected", work_ref=shared_work_ref, payload={}
    )

    a_events = await addon_a.consume_step_events()
    b_events = await addon_b.consume_step_events()

    assert [e.event_kind for e in a_events.items] == ["approval.approved"]
    assert [e.event_kind for e in b_events.items] == ["approval.rejected"]
    # The work_ref collision does not breach isolation — attribution is by
    # identity at write time, so neither log can hold the other's event.
    assert all(e.work_ref == shared_work_ref for e in a_events.items + b_events.items)


async def test_orchestration_isolated_per_double_on_shared_work_ref() -> None:
    """Two doubles under the same work_ref replay as two disjoint subtrees.

    The audit parent-linkage run is keyed by the caller's own
    ``keycloak_client_id``, so a shared work_ref string never merges two
    add-ons' orchestrations — the isolation boundary #3028 guarantees, proven
    through the double.
    """
    await _seed_tenant()
    addon_a = await ReferenceAddon.pair(
        tenant_id=_TENANT, name="reference-a", service_account_sub="svc-ref-a"
    )
    addon_b = await ReferenceAddon.pair(
        tenant_id=_TENANT, name="reference-b", service_account_sub="svc-ref-b"
    )

    shared_work_ref = "jira:OPS-99"
    run_a = await addon_a.run_orchestration(
        work_ref=shared_work_ref, dispatch_op_ids=["k8s:pod.list"]
    )
    run_b = await addon_b.run_orchestration(
        work_ref=shared_work_ref, dispatch_op_ids=["k8s:pod.list"]
    )

    assert run_a.session_id != run_b.session_id
    assert run_a.anchor_audit_id != run_b.anchor_audit_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        roots_a = await replay_session(run_a.session_id, tenant_id=_TENANT, session=session)
        roots_b = await replay_session(run_b.session_id, tenant_id=_TENANT, session=session)
    assert len(roots_a) == 1 and len(roots_b) == 1
    assert len(roots_a[0].children) == 1
    assert len(roots_b[0].children) == 1
