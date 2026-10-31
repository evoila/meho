# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Upgrade, binary and environment handlers for :class:`VcfFleetConnector`.

Seven typed ops on the legacy ``/lcm`` API of Aria Suite Lifecycle 8.x and
VCF 9.0 fleet management. The op metadata lives in
:mod:`~meho_backplane.connectors.vcf_fleet.typed_ops_lifecycle`.

* ``fleet.product.upgrade`` and ``fleet.product.upgrade.precheck`` start an
  upgrade or its pre-check of one product in one environment.
* ``fleet.binary.list`` reads the product binaries the appliance has;
  ``fleet.binary.map`` finds the binaries at a depot path and maps them.
* ``fleet.environment.precheck``, ``fleet.environment.create`` and
  ``fleet.environment.delete`` check, create and delete an environment.

Every change is a vendor job: the appliance answers with a ``requestId`` at
once and does the work later. The handlers return ``accepted`` with that id
and name the status read ``fleet.request.get``. They never wait for the end.

Before an upgrade, an upgrade pre-check or an environment create, the
handler reads the request list and looks for a running request of the same
kind on the same object. When one runs, it returns that request's id and
starts nothing. This is a help, not a lock.

The handlers sit on a mixin so the connector module stays small. The
dispatcher rebinds each one to the live connector through
:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`, the
same path the VCF Operations licence mixin uses.

Sources: the vendor API reference (VMware Aria Suite Lifecycle API 1.3.0,
8.12 to 8.16), the 8.18 programming guide (product upgrade), and the
community scripts that map binaries and create environments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "FLEET_LIFECYCLE_DECLARED_OP_IDS",
    "FLEET_STATUS_READ_OP_ID",
    "FleetLifecycleHandlers",
    "environment_delete_preview",
    "spec_secret_problem",
]

#: The status read every start names. It reads one request by its id.
FLEET_STATUS_READ_OP_ID: Final = "fleet.request.get"

_ENVIRONMENTS_PATH: Final = "/lcm/lcops/api/v2/environments"
_ENVIRONMENT_PATH: Final = "/lcm/lcops/api/v2/environments/{environmentId}"
_ENVIRONMENT_PRECHECK_PATH: Final = "/lcm/lcops/api/v2/environments/pre-validate"
_UPGRADE_PATH: Final = "/lcm/lcops/api/v2/environments/{environmentId}/products/{productId}/upgrade"
_UPGRADE_PRECHECK_PATH: Final = f"{_UPGRADE_PATH}/pre-validate"
_BINARIES_PATH: Final = "/lcm/lcops/api/v2/settings/product-binaries"
_BINARIES_MAP_PATH: Final = f"{_BINARIES_PATH}/download"
_REQUESTS_PATH: Final = "/lcm/request/api/v2/requests"

#: Every ``METHOD:/path`` these handlers call. The reconcile lane checks each
#: one against the pinned ``/lcm`` API file when the shelf has it.
FLEET_LIFECYCLE_DECLARED_OP_IDS: Final[frozenset[str]] = frozenset(
    {
        f"POST:{_UPGRADE_PATH}",
        f"POST:{_UPGRADE_PRECHECK_PATH}",
        f"GET:{_BINARIES_PATH}",
        f"POST:{_BINARIES_PATH}",
        f"POST:{_BINARIES_MAP_PATH}",
        f"POST:{_ENVIRONMENT_PRECHECK_PATH}",
        f"POST:{_ENVIRONMENTS_PATH}",
        f"GET:{_ENVIRONMENT_PATH}",
        f"DELETE:{_ENVIRONMENT_PATH}",
        f"GET:{_REQUESTS_PATH}",
    }
)

#: Request states that mean "not ended yet".
_RUNNING_STATES: Final = frozenset({"CREATED", "SUBMITTED", "INPROGRESS", "IN_PROGRESS"})

#: Words in a request's type, name or reason that mark its kind.
_UPGRADE_KIND: Final = ("upgrade",)
_CREATE_KIND: Final = ("create_environment", "createenvironment", "create environment")

#: A value that names a secret in the appliance's own store (the locker).
_LOCKER_PREFIX: Final = "locker:"

if TYPE_CHECKING:
    # The connector also inherits HttpConnector; naming it as the base here
    # only lets the type checker see the request seams.
    from meho_backplane.connectors.adapters.http import HttpConnector
    from meho_backplane.connectors.vcf_fleet.session import VcfFleetTargetLike

    _Base = HttpConnector
else:
    _Base = object


def _status_read(request_id: str) -> dict[str, Any]:
    """The status read call that follows a start: its op id and its params."""
    return {"op_id": FLEET_STATUS_READ_OP_ID, "params": {"request_id": request_id}}


def _accepted(request_id: Any, **extra: Any) -> dict[str, Any]:
    """The answer of a start the appliance accepted."""
    rid = str(request_id) if request_id else ""
    if not rid:
        raise ValueError(
            "the appliance answered without a requestId; the job may have started, so "
            "read the request list (fleet.request.list) before you call again"
        )
    return {
        "status": "accepted",
        "request_id": rid,
        "status_read": _status_read(rid),
        **extra,
    }


def _already_running(request: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The answer when a request of the same kind already runs on the object."""
    rid = str(request.get("vmid") or request.get("requestId") or "")
    return {
        "status": "already_running",
        "request_id": rid,
        "request_state": request.get("state"),
        "status_read": _status_read(rid),
        **extra,
    }


def _request_rows(payload: Any) -> list[dict[str, Any]]:
    """The request rows of a request-list answer (a bare list or a wrapper)."""
    if isinstance(payload, dict):
        for key in ("requests", "content", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _input_values(value: Any) -> set[str]:
    """Every text and number value in a request's ``inputMap``, at any depth."""
    if isinstance(value, dict):
        return {text for item in value.values() for text in _input_values(item)}
    if isinstance(value, list):
        return {text for item in value for text in _input_values(item)}
    if isinstance(value, str | int) and not isinstance(value, bool):
        return {str(value)}
    return set()


def _matches(request: dict[str, Any], kind: tuple[str, ...], words: tuple[str, ...]) -> bool:
    """True when *request* runs, is of *kind*, and its ``inputMap`` holds every one of *words*.

    Each word must equal a whole value in the ``inputMap``: a request for
    ``Production-DR`` does not match ``Production``. An empty word never matches.
    """
    if str(request.get("state") or "").upper() not in _RUNNING_STATES:
        return False
    label = " ".join(
        str(request.get(key) or "") for key in ("requestType", "requestName", "requestReason")
    ).lower()
    if not any(word in label for word in kind):
        return False
    if not words or not all(words):
        return False
    values = _input_values(request.get("inputMap"))
    return all(word in values for word in words)


def spec_secret_problem(value: Any, path: str = "spec") -> str | None:
    """Why *value* holds a secret in clear text, or ``None`` when it holds none.

    A field named like a password or secret, and a licence field, must hold a
    locker reference (``locker:...``): the appliance resolves it from its own
    secret store. A private key in any text is refused too.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}"
            named = is_secret_param_name(str(key)) or "license" in str(key).lower()
            if named and isinstance(item, str) and item and not item.startswith(_LOCKER_PREFIX):
                return f"{where} must be a locker reference (locker:...), not a value"
            problem = spec_secret_problem(item, where)
            if problem is not None:
                return problem
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problem = spec_secret_problem(item, f"{path}[{index}]")
            if problem is not None:
                return problem
    elif isinstance(value, str) and "PRIVATE KEY" in value:
        return f"{path} holds a private key; import it into the locker and use its reference"
    return None


def _upgrade_body(params: dict[str, Any], *, pre_validate: bool) -> dict[str, Any]:
    """The vendor ``UpgradeProductRequestDTO`` for an upgrade or its pre-check."""
    license_ref = params.get("license_ref")
    if license_ref and not str(license_ref).startswith(_LOCKER_PREFIX):
        raise ValueError("license_ref must be a locker reference (locker:license:...)")
    return {
        "environmentId": params["environment_id"],
        "productId": params["product_id"],
        "productVersion": params["product_version"],
        "repositoryType": params.get("repository_type", "lcmRepository"),
        "repositoryUrl": params["repository_url"],
        "licenseRef": license_ref,
        "preValidate": pre_validate,
        "takeProductSnapshot": bool(params.get("take_product_snapshot", False)),
        "retainProductSnapshot": bool(params.get("retain_product_snapshot", False)),
    }


def _vendor_error(exc: httpx.HTTPStatusError) -> str:
    """The vendor's error text of *exc*: its ``message``, or the raw body."""
    try:
        body = exc.response.json()
    except ValueError:
        return exc.response.text[:500] or f"HTTP {exc.response.status_code}"
    if isinstance(body, dict):
        text = body.get("message") or body.get("errorLabel") or body.get("errorCode")
        if text:
            return str(text)
    return f"HTTP {exc.response.status_code}"


class FleetLifecycleHandlers(_Base):
    """The upgrade, binary and environment handlers of the fleet connector."""

    async def _running_request(
        self,
        target: VcfFleetTargetLike,
        operator: Operator,
        kind: tuple[str, ...],
        words: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """The first running request of *kind* that names all *words*, or ``None``."""
        payload: Any = await self._get_json(target, _REQUESTS_PATH, operator=operator)
        return next((r for r in _request_rows(payload) if _matches(r, kind, words)), None)

    async def _start_upgrade(
        self,
        operator: Operator,
        target: VcfFleetTargetLike,
        params: dict[str, Any],
        *,
        path: str,
        pre_validate: bool,
    ) -> dict[str, Any]:
        env_id, product_id = params["environment_id"], params["product_id"]
        body = _upgrade_body(params, pre_validate=pre_validate)
        running = await self._running_request(target, operator, _UPGRADE_KIND, (env_id, product_id))
        ids = {"environment_id": env_id, "product_id": product_id}
        if running is not None:
            return _already_running(running, **ids)
        url = path.format(environmentId=env_id, productId=product_id)
        answer = await self._post_json(target, url, operator=operator, json=body)
        return _accepted(answer.get("requestId"), **ids)

    async def product_upgrade(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.product.upgrade`` — start the upgrade of one product.

        Sends ``preValidate: true`` unless ``skip_precheck`` is set, so the
        appliance checks first and does not start an upgrade that fails it.
        """
        return await self._start_upgrade(
            operator,
            target,
            params,
            path=_UPGRADE_PATH,
            pre_validate=not params.get("skip_precheck", False),
        )

    async def product_upgrade_precheck(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.product.upgrade.precheck`` — start the pre-check of an upgrade."""
        return await self._start_upgrade(
            operator, target, params, path=_UPGRADE_PRECHECK_PATH, pre_validate=True
        )

    async def binary_list(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.binary.list`` — the product binaries the appliance has, as rows."""
        del params
        payload: Any = await self._get_json(target, _BINARIES_PATH, operator=operator)
        rows = payload if isinstance(payload, list) else []
        return {"binaries": [row for row in rows if isinstance(row, dict)]}

    async def binary_map(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.binary.map`` — find the binaries at a depot path, then map them.

        Two steps: ``find`` (``POST product-binaries``, answers at once with
        the binaries it found) and ``map`` (``POST product-binaries/download``,
        a vendor job). A failed step stops the run; the answer lists the steps
        that ran, the step that failed with the vendor's error, and the steps
        that did not run. Nothing is undone.
        """
        source = {"sourceLocation": params["source_location"], "sourceType": params["source_type"]}
        try:
            found: Any = await self._post_json(
                target, _BINARIES_PATH, operator=operator, json=source
            )
        except httpx.HTTPStatusError as exc:
            return _step_failure([], "find", _vendor_error(exc), ["map"])
        rows = [row for row in (found if isinstance(found, list) else []) if isinstance(row, dict)]
        names = set(params.get("names") or [])
        chosen = [row for row in rows if not names or row.get("name") in names]
        find_step = {"step": "find", "result": "ok", "found": len(rows)}
        if not chosen:
            error = "no product binary found at the path" + (" with those names" if names else "")
            return _step_failure([find_step], "map", error, [])
        body: Any = [
            {"filePath": r.get("filePath"), "name": r.get("name"), "type": r.get("type")}
            for r in chosen
        ]
        try:
            answer = await self._post_json(target, _BINARIES_MAP_PATH, operator=operator, json=body)
        except httpx.HTTPStatusError as exc:
            return _step_failure([find_step], "map", _vendor_error(exc), [])
        steps = [find_step, {"step": "map", "result": "accepted"}]
        return _accepted(answer.get("requestId"), binaries=body, steps=steps)

    async def environment_precheck(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.environment.precheck`` — start the pre-check of a new environment."""
        spec = _checked_spec(params)
        answer = await self._post_json(
            target, _ENVIRONMENT_PRECHECK_PATH, operator=operator, json=spec
        )
        return _accepted(answer.get("requestId"), environment_name=spec.get("environmentName"))

    async def environment_create(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.environment.create`` — start creating an environment from a spec."""
        spec = _checked_spec(params)
        name = str(spec.get("environmentName") or "")
        running = await self._running_request(target, operator, _CREATE_KIND, (name,))
        if running is not None:
            return _already_running(running, environment_name=name)
        answer = await self._post_json(target, _ENVIRONMENTS_PATH, operator=operator, json=spec)
        return _accepted(answer.get("requestId"), environment_name=name)

    async def environment_delete(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.environment.delete`` — start deleting an environment.

        ``delete_vms`` (default ``false``) also deletes the product VMs from
        vCenter. The body is the vendor ``DeleteEnvironmentRequestDTO``.
        """
        env_id = params["environment_id"]
        delete_vms = bool(params.get("delete_vms", False))
        body = {
            "deleteFromInventory": True,
            "deleteFromVcenter": delete_vms,
            "deleteLbFromSddc": False,
            "deleteWindowsVMs": False,
        }
        url = _ENVIRONMENT_PATH.format(environmentId=env_id)
        answer = await self._post_json(target, url, operator=operator, verb="DELETE", json=body)
        return _accepted(answer.get("requestId"), environment_id=env_id, delete_vms=delete_vms)


def _checked_spec(params: dict[str, Any]) -> dict[str, Any]:
    """The environment spec of *params*, refused when it holds a clear secret."""
    spec: dict[str, Any] = params["spec"]
    problem = spec_secret_problem(spec)
    if problem is not None:
        raise ValueError(problem)
    return spec


def _step_failure(
    ran: list[dict[str, Any]], failed: str, error: str, not_run: list[str]
) -> dict[str, Any]:
    """The answer of a multi-step change that stopped at step *failed*."""
    return {
        "status": "failed",
        "steps": [
            *ran,
            {"step": failed, "result": "failed", "error": error},
            *({"step": step, "result": "not_run"} for step in not_run),
        ],
    }


async def environment_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius of ``fleet.environment.delete``: the environment and its products.

    Reads the environment first. Raises when it does not exist, so the delete
    is refused before it waits for approval.
    """
    connector = ctx.connector_instance
    if connector is None:
        return None
    env_id = str(ctx.params.get("environment_id") or "")
    if not env_id:
        raise ValueError("environment_id is required")
    try:
        env: Any = await connector._get_json(  # type: ignore[attr-defined]
            ctx.target, _ENVIRONMENT_PATH.format(environmentId=env_id), operator=ctx.operator
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 404):
            raise ValueError(f"environment {env_id!r} does not exist") from exc
        raise
    if not isinstance(env, dict) or not env:
        raise ValueError(f"environment {env_id!r} does not exist")
    delete_vms = bool(ctx.params.get("delete_vms", False))
    children = [
        {
            "product_id": product.get("id") or product.get("productId"),
            "version": product.get("version"),
            "nodes": len(product.get("nodes") or []),
        }
        for product in (env.get("products") or [])
        if isinstance(product, dict)
    ]
    if delete_vms:
        irreversibility = (
            "The product VMs are deleted from vCenter. They cannot be brought back, "
            "and the environment leaves the lifecycle tool."
        )
    else:
        irreversibility = (
            "The environment and its products leave the lifecycle tool. The VMs stay "
            "in vCenter, but the tool no longer manages them until they are imported again."
        )
    return {
        "blast_radius": {
            "object": {
                "environment_id": env_id,
                "environment_name": env.get("environmentName"),
            },
            "children": children,
            "irreversibility": irreversibility,
            "delete_vms": delete_vms,
        }
    }


register_preview_builder("fleet.environment.delete", environment_delete_preview)
