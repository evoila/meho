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
    ) -> None:
        self._datastores = datastores
        self._category_id = category_id
        self._tag_id = tag_id
        self._policy_id = policy_id
        self.policies = list(policies or [])
        self._pbm_create_returns = pbm_create_returns
        self._pbm_delete_outcomes = pbm_delete_outcomes if pbm_delete_outcomes is not None else []
        self.calls: list[dict[str, Any]] = []
        self.pbm_create_calls: list[dict[str, Any]] = []
        self.pbm_delete_calls: list[list[str]] = []

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
    # Category -> tag -> attach -> PBM create, all gated in order.
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
