# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers and the approval preview for the image ops (see :mod:`.image_ops`).

VCF Automation 9 (``vcfa.tenant.*``) and Aria Automation 8 (``vcfa.vra8.*``,
:mod:`meho_backplane.connectors.vra8.image_ops`) both run them: the two speak
the same ``/iaas/api`` calls. Each handler takes the op id prefix for its guidance.

The image name change carries the two traps of the vendor API:

* ``PATCH /iaas/api/image-profiles/{id}`` replaces the **whole**
  ``imageMapping``. A body with one name deletes every other name. So the
  handler reads the profile first and sends every other name back, each
  as ``{id, constraints?, cloudConfig?}`` -- the ``FabricImageDescription``
  shape the vendor's own Terraform provider sends.
* The region sent is the profile's **own** region (``_links.region.href``),
  never the first region of the org: an org with two vCenter accounts has
  two regions.

Two changes to the same profile at the same time would each read the
mapping, add their own name and send the whole mapping back, so the
second write would drop the first name. The change op therefore holds a
lock for the profile from the read to the write: an ``asyncio.Lock`` for
calls in this process and a PG advisory lock
(:func:`meho_backplane.db.advisory.advisory_lock`) for calls in other
workers. A second call waits; after :data:`PROFILE_LOCK_WAIT_SECONDS` it
fails instead of writing a stale mapping.

Every ``/iaas/api`` call pins ``apiVersion`` (:data:`._paths.TENANT_IAAS_API_VERSION`).
Path ids are percent-encoded with an empty safe set, like
``vcfa.tenant.deployment.get``; an empty id, ``.`` or ``..`` is refused,
because it would not stay one path segment.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx

from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_FABRIC_IMAGE_PATH,
    TENANT_FABRIC_IMAGES_PATH,
    TENANT_IAAS_API_VERSION,
    TENANT_IMAGE_ENUMERATION_PATH,
    TENANT_IMAGE_PROFILE_PATH,
    TENANT_IMAGE_PROFILES_PATH,
    TENANT_REQUEST_TRACKER_PATH,
)
from meho_backplane.db.advisory import advisory_lock
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
    from meho_backplane.connectors.adapters.http import HttpConnector

__all__ = [
    "TENANT_PREFIX",
    "MappingPlan",
    "plan_mapping_change",
    "register_mapping_update_preview",
    "tenant_cloud_account_enumerate_images",
    "tenant_fabric_image_list",
    "tenant_image_profile_list",
    "tenant_image_profile_mapping_update",
]

#: The op id prefix of the VCF Automation 9 tenant plane.
TENANT_PREFIX = "vcfa.tenant"

_API_VERSION: dict[str, str] = {"apiVersion": TENANT_IAAS_API_VERSION}

#: How long the enumeration op waits for its request, and how often it looks.
#: A lab run took about 5 seconds.
ENUMERATION_WAIT_SECONDS: float = 60.0
ENUMERATION_POLL_SECONDS: float = 2.0

_TRACKER_DONE: dict[str, str] = {"FINISHED": "finished", "FAILED": "failed"}

#: How long a name change waits for another change to the same profile.
PROFILE_LOCK_WAIT_SECONDS: float = 30.0


_BAD_SEGMENTS = frozenset({"", ".", ".."})


def _segment(value: Any) -> str:
    """One percent-encoded path segment; refuses an empty id, ``.`` and ``..``."""
    text = str(value)
    if text in _BAD_SEGMENTS:
        raise ValueError(f"the id {text!r} is not a valid path segment")
    return quote(text, safe="")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def tenant_image_profile_list(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``<prefix>.image_profile.list`` -- ``GET /iaas/api/image-profiles``."""
    del params  # schema declares the param object empty
    return await connector._request_json(
        target, "GET", TENANT_IMAGE_PROFILES_PATH, operator=operator, params=_API_VERSION
    )


async def tenant_fabric_image_list(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``<prefix>.fabric_image.list`` -- ``GET /iaas/api/fabric-images``."""
    query: dict[str, Any] = {k: params[k] for k in ("$filter", "$top", "$skip") if k in params}
    return await connector._request_json(
        target,
        "GET",
        TENANT_FABRIC_IMAGES_PATH,
        operator=operator,
        params={**_API_VERSION, **query},
    )


# ---------------------------------------------------------------------------
# Image enumeration (wait for the request tracker)
# ---------------------------------------------------------------------------


def _tracker_view(tracker: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tracker.get("id"),
        "status": tracker.get("status"),
        "progress": tracker.get("progress"),
        "message": tracker.get("message"),
    }


async def tenant_cloud_account_enumerate_images(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    params: dict[str, Any],
    *,
    prefix: str = TENANT_PREFIX,
) -> dict[str, Any]:
    """``<prefix>.cloud_account.enumerate_images`` -- start, then wait for the tracker.

    Waits at most :data:`ENUMERATION_WAIT_SECONDS`. A request still running
    then is a result (``in_progress``), not an error: the enumeration is
    safe to start again.
    """
    path = TENANT_IMAGE_ENUMERATION_PATH.format(id=_segment(params["cloud_account_id"]))
    tracker = await connector._post_json(target, path, operator=operator, params=_API_VERSION)
    tracker_id = tracker.get("id")
    if not tracker_id:
        raise RuntimeError(f"POST {path} answered without a request id")
    tracker_path = TENANT_REQUEST_TRACKER_PATH.format(id=_segment(tracker_id))
    deadline = time.monotonic() + ENUMERATION_WAIT_SECONDS
    while str(tracker.get("status")).upper() not in _TRACKER_DONE:
        if time.monotonic() >= deadline:
            return {
                "status": "in_progress",
                "request": _tracker_view(tracker),
                "guidance": "the request is still running; run this op again in a minute",
            }
        await asyncio.sleep(ENUMERATION_POLL_SECONDS)
        tracker = await connector._request_json(
            target, "GET", tracker_path, operator=operator, params=_API_VERSION
        )
    status = _TRACKER_DONE[str(tracker.get("status")).upper()]
    return {
        "status": status,
        "request": _tracker_view(tracker),
        "guidance": (
            f"read {prefix}.fabric_image.list to find the new templates"
            if status == "finished"
            else "the enumeration failed; see request.message"
        ),
    }


# ---------------------------------------------------------------------------
# Image name change
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MappingPlan:
    """What one image name change does to a profile. Built without any call."""

    profile_id: str
    profile_name: str | None
    region_id: str | None
    image_name: str
    action: str | None
    old_target: dict[str, Any] | None
    new_target: dict[str, Any] | None
    other_names: list[str] = field(default_factory=list)
    body: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "profile": {
                "id": self.profile_id,
                "name": self.profile_name,
                "region_id": self.region_id,
            },
            "change": {
                "image_name": self.image_name,
                "action": self.action,
                "old_target": self.old_target,
                "new_target": self.new_target,
            },
            "other_names": self.other_names,
        }


def _region_id(profile: dict[str, Any]) -> str | None:
    """The profile's own region id, from ``_links.region.href``."""
    links = profile.get("_links")
    region = links.get("region") if isinstance(links, dict) else None
    href = region.get("href") if isinstance(region, dict) else None
    if not isinstance(href, str) or not href.strip("/"):
        return None
    return href.rstrip("/").rsplit("/", 1)[-1]


def _mapping(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mappings = profile.get("imageMappings")
    raw = mappings.get("mapping") if isinstance(mappings, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {name: entry for name, entry in raw.items() if isinstance(entry, dict)}


def _target(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "fabric_image_id": entry.get("id"),
        "name": entry.get("name"),
        "externalId": entry.get("externalId"),
    }


def _spec_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """An existing mapping entry as the ``FabricImageDescription`` the PATCH takes."""
    if entry.get("id"):
        spec: dict[str, Any] = {"id": entry["id"]}
    elif entry.get("name"):
        spec = {"name": entry["name"]}
    else:
        spec = {"externalId": entry.get("externalId")}
    for key in ("constraints", "cloudConfig"):
        if entry.get(key):
            spec[key] = entry[key]
    return spec


def _action(old: dict[str, Any] | None, new_id: str | None) -> str | None:
    if new_id is None:
        return "remove" if old is not None else None
    if old is None:
        return "add"
    return None if old.get("id") == new_id else "repoint"


def plan_mapping_change(
    profile: dict[str, Any],
    image_name: str,
    *,
    fabric_image: dict[str, Any] | None,
) -> MappingPlan:
    """Plan one name change; ``fabric_image=None`` removes the name.

    ``action`` is ``None`` when nothing would change. ``body`` is the full
    ``PATCH`` body: every other name is sent back as it is; a re-pointed
    name keeps its constraints and cloud config.
    """
    mapping = _mapping(profile)
    old = mapping.get(image_name)
    new_id = fabric_image.get("id") if fabric_image is not None else None
    region_id = _region_id(profile)
    spec = {name: _spec_entry(entry) for name, entry in mapping.items() if name != image_name}
    if fabric_image is not None:
        kept = {key: old[key] for key in ("constraints", "cloudConfig") if old and old.get(key)}
        spec[image_name] = {"id": new_id, **kept}
    body: dict[str, Any] = {"name": profile.get("name"), "imageMapping": spec}
    if profile.get("description"):
        body["description"] = profile["description"]
    if region_id is not None:
        body["regionId"] = region_id
    return MappingPlan(
        profile_id=str(profile.get("id")),
        profile_name=profile.get("name"),
        region_id=region_id,
        image_name=image_name,
        action=_action(old, new_id),
        old_target=_target(old),
        new_target=_target(fabric_image),
        other_names=sorted(name for name in mapping if name != image_name),
        body=body,
    )


def _param_problem(params: dict[str, Any]) -> str | None:
    if params.get("remove") and params.get("fabric_image_id"):
        return "give either 'fabric_image_id' or 'remove': true, not both"
    if not params.get("remove") and not params.get("fabric_image_id"):
        return "give 'fabric_image_id' (the template the name should give), or 'remove': true"
    for key in ("profile_id", "fabric_image_id"):
        if key in params and str(params[key]) in _BAD_SEGMENTS:
            return f"'{key}' must not be empty, '.' or '..'"
    return None


def _precondition_problem(plan: MappingPlan, params: dict[str, Any]) -> str | None:
    """Why the name no longer gives ``expected_old_fabric_image_id`` (if given), or ``None``.

    ``None`` as the expected value means the name must not exist yet. So a
    change approved for one old template never overwrites a newer one.
    """
    if "expected_old_fabric_image_id" not in params:
        return None
    expected = params["expected_old_fabric_image_id"]
    current = plan.old_target.get("fabric_image_id") if plan.old_target else None
    if (plan.old_target is None) == (expected is None) and current == expected:
        return None
    now = "does not exist" if plan.old_target is None else f"gives fabric image {current!r}"
    return f"the name {plan.image_name!r} {now} now, not the expected {expected!r}"


async def _read_profile(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    profile_id: str,
) -> dict[str, Any]:
    path = TENANT_IMAGE_PROFILE_PATH.format(id=_segment(profile_id))
    return await connector._request_json(
        target, "GET", path, operator=operator, params=_API_VERSION
    )


async def _read_fabric_image(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    fabric_image_id: str,
) -> dict[str, Any] | None:
    """The fabric image, or ``None`` when the appliance answers 404."""
    path = TENANT_FABRIC_IMAGE_PATH.format(id=_segment(fabric_image_id))
    try:
        return await connector._request_json(
            target, "GET", path, operator=operator, params=_API_VERSION
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _region_problem(profile: dict[str, Any], image: dict[str, Any]) -> str | None:
    want, have = profile.get("externalRegionId"), image.get("externalRegionId")
    if want and have and want != have:
        return (
            f"fabric image {image.get('id')!r} is in region {have!r}, but the profile is "
            f"for region {want!r}; pick a template of the profile's region"
        )
    # For vSphere the region id is a datacenter id, unique only inside one
    # vCenter, so a template of another vCenter can share it. The cloud
    # account tells the two vCenters apart.
    account, accounts = profile.get("cloudAccountId"), image.get("cloudAccountIds")
    if account and isinstance(accounts, list) and accounts and account not in accounts:
        return (
            f"fabric image {image.get('id')!r} belongs to cloud account(s) {accounts!r}, "
            f"but the profile is for cloud account {account!r}; pick a template of the "
            "profile's cloud account"
        )
    return None


@dataclass(slots=True)
class _ProfileLock:
    lock: asyncio.Lock
    users: int = 0


#: In-process locks by key; an entry lives while a call holds or waits for it.
_profile_locks: dict[int, _ProfileLock] = {}


def _lock_key(target: VcfTargetLike, profile_id: str) -> int:
    """A stable signed-63-bit key for one profile of one target (a PG ``bigint``)."""
    raw = f"{target.tenant_id}\0{target.id}\0{profile_id}".encode()
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


@asynccontextmanager
async def _profile_lock(target: VcfTargetLike, profile_id: str) -> AsyncIterator[None]:
    """Hold the profile's lock in this process and across workers, or fail after the wait.

    Only taking the locks is bounded by :data:`PROFILE_LOCK_WAIT_SECONDS`.
    Once held, the locks stay for the whole block, however long the reads
    and the write take: a timer over the write could cancel it while the
    appliance still applies it, and then say that nothing was written.
    """
    key = _lock_key(target, profile_id)
    held = _profile_locks.setdefault(key, _ProfileLock(asyncio.Lock()))
    held.users += 1
    try:
        async with AsyncExitStack() as locks:
            try:
                async with asyncio.timeout(PROFILE_LOCK_WAIT_SECONDS):
                    await locks.enter_async_context(held.lock)
                    locked = await locks.enter_async_context(
                        advisory_lock(
                            key,
                            subsystem="vcfa_image_profile",
                            wait_seconds=PROFILE_LOCK_WAIT_SECONDS,
                        )
                    )
                    if not locked:
                        raise TimeoutError
            except TimeoutError:
                raise RuntimeError(
                    f"another change to image profile {profile_id!r} is still running; "
                    "nothing was written, run this op again in a minute"
                ) from None
            yield
    finally:
        held.users -= 1
        if held.users == 0:
            _profile_locks.pop(key, None)


async def _resolve(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    params: dict[str, Any],
    prefix: str,
) -> tuple[MappingPlan | None, str | None]:
    """Read what the change needs and plan it; ``(None, problem)`` when it cannot run."""
    problem = _param_problem(params)
    if problem is not None:
        return None, problem
    profile = await _read_profile(connector, operator, target, params["profile_id"])
    image: dict[str, Any] | None = None
    if not params.get("remove"):
        image = await _read_fabric_image(connector, operator, target, params["fabric_image_id"])
        if image is None:
            return None, (
                f"no fabric image with id {params['fabric_image_id']!r}; "
                f"find it with {prefix}.fabric_image.list"
            )
        problem = _region_problem(profile, image)
        if problem is not None:
            return None, problem
    return plan_mapping_change(profile, params["image_name"], fabric_image=image), None


async def tenant_image_profile_mapping_update(
    connector: HttpConnector,
    operator: Operator,
    target: VcfTargetLike,
    params: dict[str, Any],
    *,
    prefix: str = TENANT_PREFIX,
) -> dict[str, Any]:
    """``<prefix>.image_profile.mapping.update`` -- change one name, keep the others.

    The profile is read and written under :func:`_profile_lock`, so two
    changes to the same profile run one after the other and the second
    one reads the first one's result.
    """
    problem = _param_problem(params)
    if problem is not None:
        return {"status": "invalid_request", "guidance": f"{problem}; nothing was written"}
    async with _profile_lock(target, str(params["profile_id"])):
        plan, problem = await _resolve(connector, operator, target, params, prefix)
        if plan is None:
            return {"status": "invalid_request", "guidance": f"{problem}; nothing was written"}
        problem = _precondition_problem(plan, params)
        if problem is not None:
            return {
                "status": "precondition_failed",
                **plan.view(),
                "guidance": (
                    f"{problem}; nothing was written. Read {prefix}.image_profile.list "
                    "and ask again"
                ),
            }
        if plan.action is None:
            return {
                "status": "unchanged",
                **plan.view(),
                "guidance": (
                    f"the name {plan.image_name!r} already is like that; nothing was written"
                ),
            }
        path = TENANT_IMAGE_PROFILE_PATH.format(id=_segment(plan.profile_id))
        updated = await connector._post_json(
            target, path, operator=operator, verb="PATCH", params=_API_VERSION, json=plan.body
        )
    return {
        "status": "updated",
        **plan.view(),
        "guidance": _answer_guidance(plan, updated, prefix),
    }


def _answer_guidance(plan: MappingPlan, updated: dict[str, Any], prefix: str) -> str | None:
    """What the ``PATCH`` answer lacks, or ``None`` when it carries every name sent.

    An answer with an empty mapping is checked like any other: after a
    nonempty write it lacks every name. Only an answer without any
    mapping (no body, no ``imageMappings.mapping`` object) cannot be
    checked.
    """
    mappings = updated.get("imageMappings")
    raw = mappings.get("mapping") if isinstance(mappings, dict) else None
    if not isinstance(raw, dict):
        return (
            f"the appliance answered without a mapping; read {prefix}.image_profile.list to check"
        )
    missing = sorted(set(plan.body["imageMapping"]) - set(raw))
    if not missing:
        return None
    return (
        f"the appliance answered without these names: {missing}; "
        f"read {prefix}.image_profile.list to check"
    )


# ---------------------------------------------------------------------------
# Approval preview
# ---------------------------------------------------------------------------


def register_mapping_update_preview(prefix: str) -> None:
    """Register the approval preview of ``<prefix>.image_profile.mapping.update``.

    It shows the old and the new template, read at park time. Without a
    connector (``preview_operation``) it echoes the request; a failed read
    gives the echo plus ``read_error``.
    """

    async def _preview(ctx: PreviewContext) -> dict[str, Any] | None:
        p = ctx.params
        if not isinstance(p.get("profile_id"), str) or not isinstance(p.get("image_name"), str):
            return None
        echo = {
            "action": "update_image_mapping",
            "profile_id": p["profile_id"],
            "image_name": p["image_name"],
            "fabric_image_id": p.get("fabric_image_id"),
            "remove": bool(p.get("remove")),
        }
        if "expected_old_fabric_image_id" in p:
            echo["expected_old_fabric_image_id"] = p["expected_old_fabric_image_id"]
        if ctx.connector_instance is None:
            return {**echo, "old_target": None, "read": "skipped"}
        connector = cast("HttpConnector", ctx.connector_instance)
        try:
            plan, problem = await _resolve(connector, ctx.operator, ctx.target, p, prefix)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return {**echo, "old_target": None, "read": "failed", "read_error": type(exc).__name__}
        if plan is None:
            return {**echo, "old_target": None, "read": "done", "problem": problem}
        problem = _precondition_problem(plan, p)
        extra = {"problem": problem} if problem is not None else {}
        return {**echo, **plan.view(), "read": "done", **extra}

    register_preview_builder(f"{prefix}.image_profile.mapping.update", _preview)


register_mapping_update_preview(TENANT_PREFIX)
