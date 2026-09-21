# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the governed NFS tag-based SPBM storage-policy composites (#3494).

Exercises the ``storage_policy.create`` / ``.delete`` / ``.list`` handlers on
the **direct-session** path — a recording connector double stands in for the
REST tag sub-ops (``mount_op_path`` + ``_get_json`` / ``_post_json``) and the
typed PBM SOAP seam (``pbm_create_tag_profile`` / ``pbm_delete_profiles`` /
``pbm_retrieve_profiles``), and the shared ``enforce_subop_policy`` gate is
stubbed with a recorder. The tests prove the create-then-list-visible ->
delete -> absent lifecycle, the tag-substrate fan-out order, the fail-closed
datastore refuse, the delete-fault surface, and that every child write is
gated (its own audit / grant point).

The PBM wire codec itself is covered in ``test_connectors_vmware_rest_soap_pbm``;
here the SOAP seam is faked at the connector method boundary so the handler
orchestration is what is under test.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _storage_policy, _write
from meho_backplane.connectors.vmware_rest.composites._storage_policy import (
    storage_policy_create_composite,
    storage_policy_delete_composite,
    storage_policy_list_composite,
)
from meho_backplane.connectors.vmware_rest.soap_pbm import (
    pbm_tag_property_id as _storage_policy_tag_property_id,
)


def _make_operator() -> Operator:
    return Operator(
        sub="op-storage-policy",
        name="Storage Policy Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000003494"),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingConnector:
    """Connector double for the tag REST sub-ops + the typed PBM SOAP seam.

    REST reads/writes are recorded and served by mounted path; the PBM seam
    is faked at the method boundary and mutates ``policies`` so the create
    read-back sees the new policy and the delete read-back sees it gone.
    """

    def __init__(
        self,
        *,
        datastores: list[dict[str, str]],
        category_id: str = "cat-1",
        tag_id: str = "tag-1",
        policy_id: str = "policy-guid-1",
        policies: list[dict[str, str]] | None = None,
        pbm_create_returns: str = "policy-guid-1",
        pbm_delete_outcomes: list[dict[str, Any]] | None = None,
        existing_categories: list[dict[str, Any]] | None = None,
        existing_tags: list[dict[str, Any]] | None = None,
        pbm_tag_rules: dict[str, set[tuple[str, frozenset[str]]]] | None = None,
    ) -> None:
        self._datastores = datastores
        self._category_id = category_id
        self._tag_id = tag_id
        self._policy_id = policy_id
        self.policies = list(policies or [])
        self._pbm_create_returns = pbm_create_returns
        self._pbm_delete_outcomes = pbm_delete_outcomes if pbm_delete_outcomes is not None else []
        # #3826 adopt path: existing tag substrate the resolve-before-create reads
        # discover. Each category/tag carries its full detail model (id + name +
        # cardinality / associable_types / category_id); the list reads serve the
        # ids, the per-id gets serve the detail.
        self._existing_categories = list(existing_categories or [])
        self._existing_tags = list(existing_tags or [])
        self._pbm_tag_rules = dict(pbm_tag_rules or {})
        self.calls: list[dict[str, Any]] = []
        self.pbm_create_calls: list[dict[str, Any]] = []
        self.pbm_delete_calls: list[list[str]] = []
        self.pbm_retrieve_rule_calls: list[list[str]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: dict[str, Any] | None = None
    ) -> Any:
        self.calls.append({"method": "GET", "path": path, "query": params})
        if "/vcenter/datastore" in path:
            names = (params or {}).get("names") or []
            return [d for d in self._datastores if not names or d["name"] in names]
        if "/vcenter/storage/policies" in path:
            return list(self.policies)
        if path.endswith("/cis/tagging/category"):
            return [c["id"] for c in self._existing_categories]
        if "/cis/tagging/category/" in path:
            cid = path.rsplit("/", 1)[-1]
            return next((c for c in self._existing_categories if c["id"] == cid), None)
        if path.endswith("/cis/tagging/tag"):
            return [t["id"] for t in self._existing_tags]
        if "/cis/tagging/tag/" in path:
            tid = path.rsplit("/", 1)[-1]
            return next((t for t in self._existing_tags if t["id"] == tid), None)
        raise AssertionError(f"unexpected GET {path!r}")

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        self.calls.append({"method": verb, "path": path, "body": json})
        if path == "/api/cis/tagging/category":
            return self._category_id
        if path == "/api/cis/tagging/tag":
            return self._tag_id
        if "/cis/tagging/tag-association/" in path:
            return None
        raise AssertionError(f"unexpected POST {path!r}")

    async def pbm_create_tag_profile(
        self,
        target: Any,
        operator: Operator,
        *,
        name: str,
        description: str,
        category_name: str,
        tag_names: list[str],
    ) -> str:
        self.pbm_create_calls.append(
            {"name": name, "category_name": category_name, "tag_names": list(tag_names)}
        )
        if self._pbm_create_returns:
            self.policies.append({"policy": self._pbm_create_returns, "name": name})
        return self._pbm_create_returns

    async def pbm_delete_profiles(
        self, target: Any, operator: Operator, *, profile_ids: list[str]
    ) -> list[dict[str, Any]]:
        self.pbm_delete_calls.append(list(profile_ids))
        if not self._pbm_delete_outcomes:
            self.policies = [p for p in self.policies if p.get("policy") not in profile_ids]
        return self._pbm_delete_outcomes

    async def pbm_retrieve_profile_tag_rules(
        self, target: Any, operator: Operator, *, profile_ids: list[str]
    ) -> dict[str, set[tuple[str, frozenset[str]]]]:
        self.pbm_retrieve_rule_calls.append(list(profile_ids))
        return {pid: self._pbm_tag_rules[pid] for pid in profile_ids if pid in self._pbm_tag_rules}


class _GateRecorder:
    """Recording stub for ``enforce_subop_policy`` shared by both modules."""

    def __init__(self, gate_for: dict[str, OperationResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate_for = gate_for or {}

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {"op_id": op_id, "safety_level": safety_level, "requires_approval": requires_approval}
        )
        return self._gate_for.get(op_id)

    @property
    def gated_op_ids(self) -> list[str]:
        return [c["op_id"] for c in self.calls]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    """Install an auto-execute gate recorder on both handler modules."""
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_storage_policy, "enforce_subop_policy", recorder)
    return recorder


_TARGET = object()


# --- create ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_happy_path_full_fan_out_and_readback(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}])
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
            "description": "envision nfs",
        },
        connector=conn,
    )
    assert isinstance(out, dict)
    assert out["status"] == "created"
    assert out["policy_id"] == "policy-guid-1"
    assert out["category_id"] == "cat-1"
    assert out["tag_id"] == "tag-1"
    assert out["datastores"] == [{"name": "nfs-ds", "moid": "datastore-1"}]
    assert out["listed"] is True
    # Fresh path unchanged (#3826): nothing adopted, no warnings.
    assert out["adopted"] == {"category": False, "tag": False, "policy": False}
    assert out["issues"] == []
    # Category -> tag -> attach -> PBM create, all gated in order. The
    # resolve-before-create reads carry no gate, so the gated set is unchanged.
    assert gate.gated_op_ids == [
        "POST:/cis/tagging/category",
        "POST:/cis/tagging/tag",
        "POST:/cis/tagging/tag-association/{tagId}?action=attach",
        "POST:/pbm/ProfileManager/PbmCreate",
    ]
    # The PBM tag rule references the category by NAME (not the REST id), per
    # the govc / Ansible reference — com.vmware.storage.tag.<name>.property.
    assert conn.pbm_create_calls == [
        {"name": "NFS-Gold", "category_name": "meho-storage", "tag_names": ["nfs-gold"]}
    ]


@pytest.mark.asyncio
async def test_create_attaches_tag_to_every_datastore(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        datastores=[
            {"name": "nfs-a", "datastore": "datastore-1"},
            {"name": "nfs-b", "datastore": "datastore-2"},
        ]
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-a", "nfs-b"],
        },
        connector=conn,
    )
    assert out["status"] == "created"
    attach_calls = [c for c in conn.calls if "/cis/tagging/tag-association/" in c["path"]]
    assert len(attach_calls) == 2
    assert {c["body"]["object_id"]["id"] for c in attach_calls} == {"datastore-1", "datastore-2"}
    assert all(c["body"]["object_id"]["type"] == "Datastore" for c in attach_calls)


@pytest.mark.asyncio
async def test_create_fails_closed_when_datastore_unresolved(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(datastores=[])  # no datastore matches
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["missing-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "datastore_not_found"
    assert out["policy_id"] is None
    # No writes were issued and nothing was gated.
    assert gate.calls == []
    assert conn.pbm_create_calls == []
    assert not any(c["method"] == "POST" for c in conn.calls)


@pytest.mark.asyncio
async def test_create_gate_park_short_circuits_before_pbm(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="awaiting_approval",
        op_id="vmware.composite.storage_policy.create",
        duration_ms=0,
        result={"approval_request_id": "ar-1"},
    )
    recorder = _GateRecorder(gate_for={"POST:/pbm/ProfileManager/PbmCreate": parked})
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_storage_policy, "enforce_subop_policy", recorder)
    conn = _RecordingConnector(datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}])
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    # The tag substrate ran; the PBM create was gated and never fired.
    assert conn.pbm_create_calls == []


@pytest.mark.asyncio
async def test_create_reports_created_artifacts_when_pbm_returns_no_id(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}], pbm_create_returns=""
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "policy_create_failed"
    assert out["policy_id"] is None
    assert out["category_id"] == "cat-1"
    assert out["tag_id"] == "tag-1"
    # #3812: a load-bearing failure carries an error-severity issue so the
    # dispatcher surfaces it as a dispatch error, not a false ok/200.
    assert any(i["severity"] == "error" for i in out["issues"])


# --- adopt (idempotency, #3826) --------------------------------------------


@pytest.mark.asyncio
async def test_create_adopts_existing_category_and_tag(gate: _GateRecorder) -> None:
    """A retry after a partial run reuses the existing category + tag."""
    conn = _RecordingConnector(
        datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}],
        existing_categories=[
            {
                "id": "cat-existing",
                "name": "meho-storage",
                "cardinality": "MULTIPLE",
                "associable_types": ["Datastore"],
            }
        ],
        existing_tags=[{"id": "tag-existing", "name": "nfs-gold", "category_id": "cat-existing"}],
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "created"
    assert out["category_id"] == "cat-existing"
    assert out["tag_id"] == "tag-existing"
    assert out["policy_id"] == "policy-guid-1"
    assert out["adopted"] == {"category": True, "tag": True, "policy": False}
    # No category / tag CREATE POSTs — they were adopted, not re-created (the
    # matching GET list/get reads still fire; only the writes are suppressed).
    posts = [c for c in conn.calls if c["method"] == "POST"]
    assert not any(c["path"] == "/api/cis/tagging/category" for c in posts)
    assert not any(c["path"] == "/api/cis/tagging/tag" for c in posts)
    # Only the still-needed writes were gated (adoption reads carry no gate).
    assert gate.gated_op_ids == [
        "POST:/cis/tagging/tag-association/{tagId}?action=attach",
        "POST:/pbm/ProfileManager/PbmCreate",
    ]
    # Each adopted sub-step is reported as a warning issue.
    warnings = [i for i in out["issues"] if i["severity"] == "warning"]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_create_conflicting_category_errors(gate: _GateRecorder) -> None:
    """A same-named category with an incompatible cardinality is a terminal error."""
    conn = _RecordingConnector(
        datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}],
        existing_categories=[
            {
                "id": "cat-single",
                "name": "meho-storage",
                "cardinality": "SINGLE",
                "associable_types": ["Datastore"],
            }
        ],
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "category_conflict"
    assert out["policy_id"] is None
    assert out["category_id"] == "cat-single"
    errors = [i for i in out["issues"] if i["severity"] == "error"]
    assert errors and "SINGLE" in errors[0]["message"]
    # Fail-closed before any write: nothing gated, no PBM create, no POSTs.
    assert gate.calls == []
    assert conn.pbm_create_calls == []
    assert not any(c["method"] != "GET" for c in conn.calls)


@pytest.mark.asyncio
async def test_create_pbm_fault_after_adoption_is_clear_error_without_orphans(
    gate: _GateRecorder,
) -> None:
    """PbmCreate yielding no id after adoption is a clear error with no orphan narrative."""
    conn = _RecordingConnector(
        datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}],
        existing_categories=[
            {
                "id": "cat-existing",
                "name": "meho-storage",
                "cardinality": "MULTIPLE",
                "associable_types": ["Datastore"],
            }
        ],
        existing_tags=[{"id": "tag-existing", "name": "nfs-gold", "category_id": "cat-existing"}],
        pbm_create_returns="",
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "policy_create_failed"
    assert out["policy_id"] is None
    assert out["adopted"] == {"category": True, "tag": True, "policy": False}
    assert any(i["severity"] == "error" for i in out["issues"])
    # No orphan-cleanup narrative — a retry adopts the substrate.
    guidance = (out["guidance"] or "").lower()
    assert "clean up" not in guidance
    assert "retry" in guidance


@pytest.mark.asyncio
async def test_create_adopts_existing_policy_with_matching_rule_set(gate: _GateRecorder) -> None:
    """A same-named policy whose rule set matches is adopted, not re-created."""
    prop_id = _storage_policy_tag_property_id("meho-storage")
    conn = _RecordingConnector(
        datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}],
        existing_categories=[
            {
                "id": "cat-existing",
                "name": "meho-storage",
                "cardinality": "MULTIPLE",
                "associable_types": ["Datastore"],
            }
        ],
        existing_tags=[{"id": "tag-existing", "name": "nfs-gold", "category_id": "cat-existing"}],
        policies=[{"policy": "policy-existing", "name": "NFS-Gold"}],
        pbm_tag_rules={"policy-existing": {(prop_id, frozenset({"nfs-gold"}))}},
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "adopted"
    assert out["policy_id"] == "policy-existing"
    assert out["adopted"] == {"category": True, "tag": True, "policy": True}
    # No PbmCreate — the existing policy was adopted after the rule-set compare.
    assert conn.pbm_create_calls == []
    assert conn.pbm_retrieve_rule_calls == [["policy-existing"]]


@pytest.mark.asyncio
async def test_create_policy_conflict_when_rule_set_differs(gate: _GateRecorder) -> None:
    """A same-named policy with a different rule set is a terminal policy_conflict."""
    conn = _RecordingConnector(
        datastores=[{"name": "nfs-ds", "datastore": "datastore-1"}],
        existing_categories=[
            {
                "id": "cat-existing",
                "name": "meho-storage",
                "cardinality": "MULTIPLE",
                "associable_types": ["Datastore"],
            }
        ],
        existing_tags=[{"id": "tag-existing", "name": "nfs-gold", "category_id": "cat-existing"}],
        policies=[{"policy": "policy-existing", "name": "NFS-Gold"}],
        pbm_tag_rules={
            "policy-existing": {
                (_storage_policy_tag_property_id("other-category"), frozenset({"gold"}))
            }
        },
    )
    out = await storage_policy_create_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={
            "policy_name": "NFS-Gold",
            "category_name": "meho-storage",
            "tag_name": "nfs-gold",
            "datastore_names": ["nfs-ds"],
        },
        connector=conn,
    )
    assert out["status"] == "policy_conflict"
    assert out["policy_id"] == "policy-existing"
    assert any(i["severity"] == "error" for i in out["issues"])
    assert conn.pbm_create_calls == []


# --- delete ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_happy_path_read_back_absent(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        datastores=[], policies=[{"policy": "policy-guid-1", "name": "NFS-Gold"}]
    )
    out = await storage_policy_delete_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={"policy_id": "policy-guid-1", "policy_name": "NFS-Gold"},
        connector=conn,
    )
    assert out["status"] == "deleted"
    assert conn.pbm_delete_calls == [["policy-guid-1"]]
    assert gate.gated_op_ids == ["POST:/pbm/ProfileManager/PbmDelete"]
    assert conn.policies == []


@pytest.mark.asyncio
async def test_delete_surfaces_per_id_fault(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        datastores=[],
        policies=[{"policy": "policy-guid-1", "name": "NFS-Gold"}],
        pbm_delete_outcomes=[
            {"profileId": {"uniqueId": "policy-guid-1"}, "fault": {"_typeName": "PbmFault"}}
        ],
    )
    out = await storage_policy_delete_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={"policy_id": "policy-guid-1"},
        connector=conn,
    )
    assert out["status"] == "delete_failed"
    assert out["fault"] == "PbmFault"
    # The policy is still listed (delete was refused).
    assert conn.policies == [{"policy": "policy-guid-1", "name": "NFS-Gold"}]


@pytest.mark.asyncio
async def test_delete_still_present_when_readback_shows_policy(gate: _GateRecorder) -> None:
    # PbmDelete reports success (no outcomes) but the policy lingers in the list.
    conn = _RecordingConnector(
        datastores=[], policies=[{"policy": "policy-guid-1", "name": "NFS-Gold"}]
    )
    # Freeze the policy list so the delete's list-scrub cannot remove it.
    conn.policies = [{"policy": "policy-guid-1", "name": "NFS-Gold"}]

    async def _no_scrub(target: Any, operator: Operator, *, profile_ids: list[str]) -> list[Any]:
        conn.pbm_delete_calls.append(list(profile_ids))
        return []

    conn.pbm_delete_profiles = _no_scrub  # type: ignore[assignment]
    out = await storage_policy_delete_composite(
        operator=_make_operator(),
        target=_TARGET,
        params={"policy_id": "policy-guid-1"},
        connector=conn,
    )
    assert out["status"] == "still_present"


# --- list ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_policies_and_filters_by_id(gate: _GateRecorder) -> None:
    conn = _RecordingConnector(
        datastores=[],
        policies=[
            {"policy": "p-1", "name": "one"},
            {"policy": "p-2", "name": "two"},
        ],
    )
    out = await storage_policy_list_composite(
        operator=_make_operator(), target=_TARGET, params={}, connector=conn
    )
    assert [p["policy"] for p in out["policies"]] == ["p-1", "p-2"]

    out2 = await storage_policy_list_composite(
        operator=_make_operator(), target=_TARGET, params={"policy_ids": ["p-2"]}, connector=conn
    )
    assert [p["policy"] for p in out2["policies"]] == ["p-2"]
