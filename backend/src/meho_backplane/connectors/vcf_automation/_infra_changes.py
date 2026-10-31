# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud zone, flavor profile, project member and project changes (IaaS API).

Shared by Aria Automation 8 (``vcfa.vra8.*``) and VCF Automation 9
(``vcfa.tenant.*``), like :mod:`._infra`. Each change reads the object first
and sends only what differs; when nothing differs it answers ``unchanged``
and writes nothing, so a second run is safe. The answer is::

    {"status": "created" | "updated" | "unchanged" | "invalid_request", <object>, "guidance"}

Project members are changed with ``PATCH /iaas/api/projects/{id}`` (the role
lists of the ``ProjectSpecification``). That call is in the pinned
``vra-iaas.json``; the project service call ``/project-service/api/...`` is
not in a pinned file, so the reconcile lanes could not check it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.connectors.vcf_automation._infra import (
    API_VERSION,
    flavor_rows,
    linked_id,
    list_rows,
    segment,
    tags_of,
    zone_row,
)
from meho_backplane.connectors.vcf_automation._infra_paths import (
    CLOUD_ACCOUNTS_PATH,
    FLAVOR_PROFILE_PATH,
    FLAVOR_PROFILES_PATH,
    PROJECT_PATH,
    PROJECTS_PATH,
    ZONE_PATH,
    ZONES_PATH,
)
from meho_backplane.connectors.vcf_automation._lookups import find_by_odata_name, password_ref
from meho_backplane.connectors.vcf_automation._provisioning import project_create_body
from meho_backplane.connectors.vcf_automation._provisioning_preview import _existence
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "MEMBER_ROLES",
    "cloud_account_create_preview",
    "flavor_profile_set",
    "project_create",
    "project_members_set",
    "zone_set",
]

#: Op param -> ``ZoneSpecification`` field, for the plain fields.
_ZONE_FIELDS: Final[dict[str, str]] = {
    "name": "name",
    "description": "description",
    "placement_policy": "placementPolicy",
    "folder": "folder",
}
#: Role -> the ``ProjectSpecification`` list that holds it.
MEMBER_ROLES: Final[dict[str, str]] = {
    "administrator": "administrators",
    "member": "members",
    "viewer": "viewers",
    "supervisor": "supervisors",
}


def _invalid(key: str, guidance: str) -> dict[str, Any]:
    return {"status": "invalid_request", key: None, "changed": [], "guidance": guidance}


def _tag_list(items: Any) -> list[dict[str, str]]:
    return [{"key": str(t["key"]), "value": str(t.get("value") or "")} for t in items or []]


def _same_tags(current: Any, wanted: list[dict[str, str]]) -> bool:
    have = sorted((t["key"], t["value"] or "") for t in tags_of(current))
    return have == sorted((t["key"], t["value"]) for t in wanted)


# ---------------------------------------------------------------------------
# Cloud zone
# ---------------------------------------------------------------------------


def _zone_changes(current: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``ZoneSpecification`` fields whose wanted value differs from *current*."""
    changes = {
        wire: params[param]
        for param, wire in _ZONE_FIELDS.items()
        if param in params and params[param] != current.get(wire)
    }
    for param, wire in (("tags_to_match", "tagsToMatch"), ("tags", "tags")):
        if param in params and not _same_tags(current.get(wire), _tag_list(params[param])):
            changes[wire] = _tag_list(params[param])
    if "compute_ids" in params:
        changes["computeIds"] = list(params["compute_ids"])
    return changes


async def _find_zone(
    connector: Any, operator: Any, target: Any, name: str, region_id: str
) -> dict[str, Any] | None:
    """The zone named *name* (case does not matter) in *region_id*, or ``None``.

    The zone list has no filter, so this reads every zone.
    """
    rows, _ = await list_rows(connector, operator, target, ZONES_PATH, {}, paged=True)
    for row in rows:
        same_name = str(row.get("name") or "").casefold() == name.casefold()
        if same_name and linked_id(row, "/iaas/api/regions/") in (None, region_id):
            return row
    return None


async def zone_set(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """Create a cloud zone (``POST /iaas/api/zones``) or change one (``PATCH .../{id}``)."""
    if not params.get("id"):
        return await _zone_create(connector, operator, target, params, prefix)
    path = ZONE_PATH.format(id=segment(params["id"]))
    current = await connector._request_json(
        target, "GET", path, operator=operator, params=API_VERSION
    )
    changes = _zone_changes(current, params)
    region_id = params.get("region_id") or linked_id(current, "/iaas/api/regions/")
    if not changes:
        return {
            "status": "unchanged",
            "zone": zone_row(current),
            "changed": [],
            "guidance": "the cloud zone already has these values; nothing was written",
        }
    if not region_id:
        return _invalid("zone", "the zone's region could not be read; give region_id")
    base = {"name": current.get("name"), "placementPolicy": current.get("placementPolicy")}
    body = {
        **{key: value for key, value in base.items() if value is not None},
        "regionId": region_id,
        **changes,
    }
    updated = await connector._post_json(
        target, path, operator=operator, verb="PATCH", params=API_VERSION, json=body
    )
    return {
        "status": "updated",
        "zone": zone_row(updated or {**current, **changes}),
        "changed": sorted(changes),
        "guidance": f"read {prefix}.zone.list to see the zone",
    }


async def _zone_create(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], prefix: str
) -> dict[str, Any]:
    if not params.get("name") or not params.get("region_id"):
        return _invalid("zone", "a new cloud zone needs name and region_id; a change needs id")
    existing = await _find_zone(connector, operator, target, params["name"], params["region_id"])
    if existing is not None:
        return {
            "status": "unchanged",
            "zone": zone_row(existing),
            "changed": [],
            "guidance": (
                f"a cloud zone named {existing.get('name')!r} exists in this region; nothing "
                "was written. To change it, call again with its id"
            ),
        }
    body = {"regionId": params["region_id"], **_zone_changes({}, params)}
    created = await connector._post_json(
        target, ZONES_PATH, operator=operator, params=API_VERSION, json=body
    )
    return {
        "status": "created",
        "zone": zone_row(created),
        "changed": sorted(body),
        "guidance": f"give the zone to a project; read {prefix}.zone.list to see it",
    }


# ---------------------------------------------------------------------------
# Flavor profile
# ---------------------------------------------------------------------------


def _flavor_description(flavor: Mapping[str, Any]) -> dict[str, Any]:
    """One ``FabricFlavorDescription`` from a flavor row or an op param."""
    description: dict[str, Any] = {}
    for source, wire in (("cpu_count", "cpuCount"), ("memory_mb", "memoryInMB")):
        value = flavor.get(source, flavor.get(wire))
        if value is not None:
            description[wire] = value
    if flavor.get("name"):
        description["name"] = flavor["name"]
    return description


def _wanted_mapping(
    current: Mapping[str, Any], params: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    mapping = {row["key"]: _flavor_description(row) for row in flavor_rows(current)}
    for key in params.get("remove_flavors") or []:
        mapping.pop(key, None)
    for key, flavor in (params.get("flavors") or {}).items():
        mapping[key] = _flavor_description(flavor)
    return mapping


def _profile_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "externalRegionId": row.get("externalRegionId"),
        "flavors": flavor_rows(row),
    }


async def flavor_profile_set(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """Create a flavor profile (``POST``) or change its flavors (``PATCH .../{id}``)."""
    if not params.get("id"):
        return await _flavor_profile_create(connector, operator, target, params, prefix)
    path = FLAVOR_PROFILE_PATH.format(id=segment(params["id"]))
    current = await connector._request_json(
        target, "GET", path, operator=operator, params=API_VERSION
    )
    mapping = _wanted_mapping(current, params)
    old = {row["key"]: _flavor_description(row) for row in flavor_rows(current)}
    body: dict[str, Any] = {}
    for param in ("name", "description"):
        if param in params and params[param] != current.get(param):
            body[param] = params[param]
    if mapping != old:
        body["flavorMapping"] = mapping
    if not body:
        return {
            "status": "unchanged",
            "flavor_profile": _profile_view(current),
            "changed": [],
            "guidance": "the flavor profile already has these values; nothing was written",
        }
    changed = sorted(body)
    body["flavorMapping"] = mapping
    updated = await connector._post_json(
        target, path, operator=operator, verb="PATCH", params=API_VERSION, json=body
    )
    return {
        "status": "updated",
        "flavor_profile": _profile_view(updated or current),
        "changed": changed,
        "guidance": f"read {prefix}.flavor_profile.list to see the profile",
    }


async def _flavor_profile_create(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], prefix: str
) -> dict[str, Any]:
    if not params.get("name") or not params.get("region_id") or not params.get("flavors"):
        return _invalid(
            "flavor_profile",
            "a new flavor profile needs name, region_id and flavors; a change needs id",
        )
    existing = await find_by_odata_name(
        connector, target, operator, FLAVOR_PROFILES_PATH, params["name"], params=API_VERSION
    )
    if existing is not None:
        return {
            "status": "unchanged",
            "flavor_profile": _profile_view(existing),
            "changed": [],
            "guidance": (
                f"a flavor profile named {existing.get('name')!r} exists; nothing was "
                "written. To change it, call again with its id"
            ),
        }
    body: dict[str, Any] = {
        "name": params["name"],
        "regionId": params["region_id"],
        "flavorMapping": _wanted_mapping({}, params),
    }
    if params.get("description"):
        body["description"] = params["description"]
    created = await connector._post_json(
        target, FLAVOR_PROFILES_PATH, operator=operator, params=API_VERSION, json=body
    )
    return {
        "status": "created",
        "flavor_profile": _profile_view(created),
        "changed": sorted(body),
        "guidance": f"read {prefix}.flavor_profile.list to see the profile",
    }


# ---------------------------------------------------------------------------
# Project members
# ---------------------------------------------------------------------------


def _principal(item: Mapping[str, Any]) -> dict[str, str]:
    return {"email": str(item["email"]), "type": str(item.get("type") or "user")}


def _key(item: Mapping[str, Any]) -> tuple[str, str]:
    return str(item.get("email") or "").casefold(), str(item.get("type") or "user")


def _role_lists(project: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    lists: dict[str, list[dict[str, str]]] = {}
    for wire in MEMBER_ROLES.values():
        raw = project.get(wire)
        lists[wire] = (
            [_principal(p) for p in raw if isinstance(p, Mapping) and p.get("email")]
            if isinstance(raw, list)
            else []
        )
    return lists


def _apply_members(
    lists: dict[str, list[dict[str, str]]], params: Mapping[str, Any]
) -> dict[str, list[dict[str, str]]]:
    """The role lists after *params*: ``remove`` first, then ``add``.

    A principal holds one role: adding it with a role takes it out of the
    other role lists.
    """
    gone = {_key(p) for p in params.get("remove") or []}
    gone |= {_key(p) for p in params.get("add") or []}
    wanted = {wire: [p for p in rows if _key(p) not in gone] for wire, rows in lists.items()}
    for item in params.get("add") or []:
        wanted[MEMBER_ROLES[item["role"]]].append(_principal(item))
    return wanted


def _members_view(project: Mapping[str, Any], lists: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": project.get("id"), "name": project.get("name"), **lists}


async def project_members_set(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """Add or remove project members: ``GET``, then ``PATCH /iaas/api/projects/{id}``."""
    if not params.get("add") and not params.get("remove"):
        return _invalid("project", "give add, remove or both")
    path = PROJECT_PATH.format(id=segment(params["project_id"]))
    current = await connector._request_json(
        target, "GET", path, operator=operator, params=API_VERSION
    )
    before = _role_lists(current)
    after = _apply_members(before, params)
    changed = sorted(
        wire for wire in after if sorted(map(_key, after[wire])) != sorted(map(_key, before[wire]))
    )
    if not changed:
        return {
            "status": "unchanged",
            "project": _members_view(current, before),
            "changed": [],
            "guidance": "the project already has these members; nothing was written",
        }
    body = {"name": current.get("name"), **{wire: after[wire] for wire in changed}}
    await connector._post_json(
        target, path, operator=operator, verb="PATCH", params=API_VERSION, json=body
    )
    return {
        "status": "updated",
        "project": _members_view(current, after),
        "changed": changed,
        "guidance": f"read {prefix}.project.list to check the members",
    }


# ---------------------------------------------------------------------------
# Project create (Aria Automation 8; VCF Automation 9 has vcfa.tenant.project.create)
# ---------------------------------------------------------------------------


async def project_create(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``POST /iaas/api/projects``. A project with the same name answers ``unchanged``."""
    existing = await find_by_odata_name(
        connector, target, operator, PROJECTS_PATH, params["name"], params=API_VERSION
    )
    if existing is not None:
        return {
            "status": "unchanged",
            "project": {key: existing.get(key) for key in ("id", "name", "orgId")},
            "guidance": (
                f"a project named {existing.get('name')!r} already exists; nothing was "
                f"written. Change its members with {prefix}.project.members.set"
            ),
        }
    created = await connector._post_json(
        target,
        PROJECTS_PATH,
        operator=operator,
        params=API_VERSION,
        json=project_create_body(params),
    )
    return {
        "status": "created",
        "project": {key: created.get(key) for key in ("id", "name", "orgId")},
        "guidance": None,
    }


# ---------------------------------------------------------------------------
# Preview of the cloud account add (it sends a secret, so it has no generic preview)
# ---------------------------------------------------------------------------


async def cloud_account_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the add would do: the account, the system, the user and where the password is.

    Shows the Vault place of the password, never the password. Reads the
    account list once (by name) to say ``create`` or ``unchanged``.
    """
    p = ctx.params
    name = p.get("name")
    if not isinstance(name, str):
        return None
    secret_ref, mount, key = password_ref(p)
    return {
        "action": "add_cloud_account",
        "type": p.get("type"),
        "name": name,
        "host_name": p.get("host_name"),
        "username": p.get("username"),
        "regions": p.get("regions") or [],
        "password_source": {"secret_ref": secret_ref, "mount": mount, "key": key},
        **await _existence(
            ctx,
            lambda connector: find_by_odata_name(
                connector, ctx.target, ctx.operator, CLOUD_ACCOUNTS_PATH, name, params=API_VERSION
            ),
        ),
    }


register_preview_builder("vcfa.tenant.cloud_account.create", cloud_account_create_preview)
register_preview_builder("vcfa.vra8.cloud_account.create", cloud_account_create_preview)
