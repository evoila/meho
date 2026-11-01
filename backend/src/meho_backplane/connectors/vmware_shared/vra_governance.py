# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approvals and policies of Aria Automation 8 and VCF Automation 9.

Aria Automation 8.18 and the tenant plane of a VCF Automation 9 "VM Apps"
org serve the same approval and policy APIs. So the four operations are
written once here and registered on both connectors:

* ``vcfa-vra8-8.0`` with ids ``vcfa.vra8.*`` (:mod:`..vra8.governance_ops`),
* ``vcfa-rest-9.0`` with ids ``vcfa.tenant.*``
  (:mod:`..vcf_automation.governance_ops`).

Each connector passes a :class:`VraGovCalls`: its own GET and its own send
(POST, PATCH, DELETE) with its own login, plus the op id prefix for the
texts that name the next op. The ABX and event subscription handlers in
:mod:`.vra_extensibility` use the same calls.

Where the paths come from:

* ``/policy/api/*`` is in the vendor's ``vra-catalog-deployment.json``
  (the Swagger 2.0 file of ``vmware/vra-sdk-go``); the reconcile lane
  checks it against that file on the spec shelf.
* ``/approval/api/*`` is in the vendor's published Approvals API
  reference; the lane checks it against the vendor's reference lists of
  VCF Automation 9.0, 9.1 and 9.1.1 on the spec shelf.

An upstream 4xx/5xx raises :exc:`httpx.HTTPStatusError`; the dispatcher
turns it into ``connector_error`` with the vendor's message.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.redaction.secret_fields import scrub_secret_fields

__all__ = [
    "APPROVALS_PATH",
    "APPROVAL_ACTION_PATH",
    "APPROVAL_PATH",
    "POLICIES_PATH",
    "POLICY_PATH",
    "VraChangeRefusedError",
    "VraGovCalls",
    "approval_decide",
    "approval_list",
    "content_rows",
    "path_segment",
    "policy_list",
    "policy_set",
]

# Vendor paths. The method of each is in the reconcile lane
# (``tests/_vra_governance_paths.py``).
APPROVALS_PATH: Final[str] = "/approval/api/approvals"
APPROVAL_PATH: Final[str] = "/approval/api/approvals/{id}"
APPROVAL_ACTION_PATH: Final[str] = "/approval/api/approvals/action"
POLICIES_PATH: Final[str] = "/policy/api/policies"
POLICY_PATH: Final[str] = "/policy/api/policies/{id}"

GetJson = Callable[[str, Mapping[str, Any] | None], Awaitable[dict[str, Any]]]
SendJson = Callable[[str, str, Mapping[str, Any] | None, Mapping[str, Any] | None], Awaitable[Any]]

#: Policy fields a caller may set. Everything else the vendor sends back
#: (ids of the org, who changed it and when, statistics) is read-only.
_POLICY_WRITABLE: Final[tuple[str, ...]] = (
    "name",
    "description",
    "typeId",
    "enforcementType",
    "projectId",
    "definition",
    "criteria",
    "scopeCriteria",
)


class VraChangeRefusedError(Exception):
    """MEHO refuses the call before it reaches the vendor; the text says why."""


@dataclass(frozen=True)
class VraGovCalls:
    """What a connector hands the shared handlers.

    ``get(path, query)`` sends one GET and returns the parsed JSON object.
    ``send(method, path, query, body)`` sends one POST, PATCH or DELETE and
    returns the parsed JSON (``{}`` for an empty answer). Both use the
    connector's own login. ``prefix`` is the op id prefix (``vcfa.vra8`` or
    ``vcfa.tenant``).
    """

    get: GetJson
    send: SendJson
    prefix: str


def path_segment(value: Any, what: str) -> str:
    """Check one caller value for a URL path and percent-encode it.

    Refuses an empty value, ``.`` and ``..``, so a caller value can never
    point at another API path. ``/`` and every other special character is
    encoded.
    """
    text = str(value)
    if text.strip() in ("", ".", ".."):
        raise VraChangeRefusedError(f"{what} must be an id, not {text!r}")
    return quote(text, safe="")


def content_rows(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``content`` rows of a vendor page (or ``[]``)."""
    rows = page.get("content")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _query(params: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any] | None:
    """Map the given params to vendor query names; ``None`` when none is set."""
    query = {wire: params[name] for name, wire in names.items() if params.get(name) is not None}
    return query or None


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def _approval_row(item: Mapping[str, Any]) -> dict[str, Any]:
    details = item.get("requestDetails")
    details = details if isinstance(details, Mapping) else {}
    catalog = details.get("catalogDetails")
    blueprint = details.get("blueprintDetails")
    policies = item.get("appliedPolicies")
    policies = [p for p in policies if isinstance(p, Mapping)] if isinstance(policies, list) else []
    approvers = sorted(
        {
            str(approver.get("approver"))
            for policy in policies
            for approver in (policy.get("approvers") or [])
            if isinstance(approver, Mapping)
            and approver.get("status") == "PENDING"
            and approver.get("approver")
        }
    )
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "action": item.get("action"),
        "requestId": item.get("requestId"),
        "requestedBy": item.get("requestedBy"),
        "deploymentId": item.get("deploymentId"),
        "deploymentName": item.get("deploymentName"),
        "projectId": item.get("projectId"),
        "projectName": item.get("projectName"),
        "catalogItemName": catalog.get("name") if isinstance(catalog, Mapping) else None,
        "templateName": blueprint.get("name") if isinstance(blueprint, Mapping) else None,
        "policyNames": [p.get("policyName") for p in policies if p.get("policyName")],
        "pendingApprovers": approvers,
        "currentPhase": item.get("currentPhase"),
        "createdAt": item.get("createdAt"),
        "expiryAt": item.get("expiryAt"),
    }


async def approval_list(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.approval.list`` — ``GET /approval/api/approvals`` as flat rows."""
    query = _query(
        params,
        {"state": "requestState", "search": "search", "page": "page", "size": "size"},
    )
    page = await calls.get(APPROVALS_PATH, query)
    return {
        "approvals": [_approval_row(item) for item in content_rows(page)],
        "totalElements": page.get("totalElements"),
    }


async def approval_decide(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.approval.decide`` — ``POST /approval/api/approvals/action``.

    Reads the approval first. When it no longer waits (``PENDING``), nothing
    is sent: the answer is ``not_pending`` with the state it has now.
    """
    approval_id = str(params["id"])
    path = APPROVAL_PATH.format(id=path_segment(approval_id, "id"))
    current = await calls.get(path, None)
    row = _approval_row(current)
    if current.get("status") != "PENDING":
        return {
            "status": "not_pending",
            "decision": None,
            "approval": row,
            "guidance": (
                f"the approval is {current.get('status')!r}, not PENDING; nothing was sent. "
                f"List the waiting approvals with {calls.prefix}.approval.list."
            ),
        }
    decision = str(params["decision"])
    body: dict[str, Any] = {"itemId": approval_id, "action": decision}
    if params.get("comment"):
        body["comment"] = params["comment"]
    await calls.send("POST", APPROVAL_ACTION_PATH, None, body)
    with contextlib.suppress(httpx.HTTPStatusError):
        row = _approval_row(await calls.get(path, None))
    return {
        "status": "decided",
        "decision": decision,
        "approval": row,
        "guidance": (
            "the decision was sent. Other approvers may still have to decide; read "
            f"{calls.prefix}.approval.list to see the state, and {calls.prefix}.request.get "
            "with approval.requestId to follow the request."
        ),
    }


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


def _policy_row(policy: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        "id": policy.get("id"),
        "name": policy.get("name"),
        "description": policy.get("description"),
        "typeId": policy.get("typeId"),
        "enforcementType": policy.get("enforcementType"),
        "projectId": policy.get("projectId"),
        "orgId": policy.get("orgId"),
        "createdAt": policy.get("createdAt"),
        "createdBy": policy.get("createdBy"),
        "lastUpdatedAt": policy.get("lastUpdatedAt"),
        "lastUpdatedBy": policy.get("lastUpdatedBy"),
        "definition": policy.get("definition"),
        "criteria": policy.get("criteria"),
        "scopeCriteria": policy.get("scopeCriteria"),
    }
    cleaned: dict[str, Any] = scrub_secret_fields(row)
    return cleaned


async def policy_list(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.policy.list`` — ``GET /policy/api/policies`` as flat rows."""
    query = _query(
        params,
        {
            "search": "search",
            "$top": "$top",
            "$skip": "$skip",
            "with_definition": "expandDefinition",
        },
    )
    page = await calls.get(POLICIES_PATH, query)
    return {
        "policies": [_policy_row(policy) for policy in content_rows(page)],
        "totalElements": page.get("totalElements"),
    }


def _invalid_policy(guidance: str) -> dict[str, Any]:
    return {"status": "invalid_request", "policy": None, "guidance": guidance}


async def _read_policy(calls: VraGovCalls, policy_id: str) -> dict[str, Any] | None:
    path = POLICY_PATH.format(id=path_segment(policy_id, "id"))
    try:
        return await calls.get(path, None)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            return None
        raise


async def _policy_by_name(
    calls: VraGovCalls, name: str, type_id: str, project_id: str | None
) -> dict[str, Any] | None:
    """The policy with this exact name, type and project, read in full."""
    page = await calls.get(POLICIES_PATH, {"search": name, "$top": 200})
    for row in content_rows(page):
        same = (row.get("name"), row.get("typeId")) == (name, type_id)
        if same and row.get("projectId") == project_id and row.get("id"):
            return await _read_policy(calls, str(row["id"]))
    return None


def _policy_body(params: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    """The policy to send: the existing writable fields, then the given ones."""
    body: dict[str, Any] = {}
    if existing is not None:
        body = {key: existing[key] for key in _POLICY_WRITABLE if existing.get(key) is not None}
        body["id"] = existing.get("id")
    given = {
        "name": params.get("name"),
        "description": params.get("description"),
        "typeId": params.get("type_id"),
        "enforcementType": params.get("enforcement_type"),
        "projectId": params.get("project_id"),
        "definition": params.get("definition"),
        "criteria": params.get("criteria"),
        "scopeCriteria": params.get("scope_criteria"),
    }
    body.update({key: value for key, value in given.items() if value is not None})
    return body


async def _target_policy(
    calls: VraGovCalls, params: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """The policy to change (or ``None`` for a new one), or why the call is refused."""
    type_id = str(params["type_id"])
    if params.get("id"):
        existing = await _read_policy(calls, str(params["id"]))
        if existing is None:
            return None, (
                f"no policy with id {params['id']!r} that this login may read; "
                f"list the policies with {calls.prefix}.policy.list"
            )
        if existing.get("typeId") != type_id:
            return None, (
                f"policy {params['id']!r} has type {existing.get('typeId')!r}, not "
                f"{type_id!r}; the type of a policy cannot change"
            )
        return existing, None
    project = params.get("project_id")
    return await _policy_by_name(calls, str(params["name"]), type_id, project), None


async def policy_set(calls: VraGovCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.policy.set`` — dry run, then ``POST /policy/api/policies``.

    Without ``id``, a policy with the same name, type and project is changed
    instead of making a second one, so the call is safe to run twice. When
    nothing differs, nothing is sent (``unchanged``).
    """
    if params.get("project_id") and params.get("scope_criteria"):
        return _invalid_policy(
            "give project_id (a policy of one project) or scope_criteria (a policy "
            "for many projects), not both"
        )
    existing, problem = await _target_policy(calls, params)
    if problem is not None:
        return _invalid_policy(problem)
    body = _policy_body(params, existing)
    if existing is not None and all(existing.get(key) == value for key, value in body.items()):
        return {
            "status": "unchanged",
            "policy": _policy_row(existing),
            "guidance": "the policy already has these values; nothing was sent",
        }
    await calls.send("POST", POLICIES_PATH, {"dryRun": "true"}, body)
    saved = await calls.send("POST", POLICIES_PATH, {"dryRun": "false"}, body)
    policy = saved if isinstance(saved, Mapping) and saved.get("id") else body
    return {
        "status": "changed" if existing is not None else "created",
        "policy": _policy_row(policy),
        "guidance": (
            f"the dry run passed and the policy was saved; read it with {calls.prefix}.policy.list"
        ),
    }
