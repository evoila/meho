# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The HCX Manager setup: vCenter, single sign-on, NSX, location, role mapping and activation.

Each function is the body of one method of
:class:`~meho_backplane.connectors.hcx.manager_setup_handlers.HcxManagerSetupOps`;
the op metadata lives in :mod:`.manager_setup_ops`. Every call goes to the
appliance management API on port 9443 (:mod:`.appliance_api`). The restart
and its status read live in :mod:`.manager_restart`.

Sources: Broadcom's "HCX Manager Appliance Management APIs" reference for
HCX 9.1, VMware's Terraform provider for HCX and VMware's PowerCLI example
module for HCX. HCX 4.x activates with a key
(``POST /api/admin/global/config/hcx``); the 9.1 reference has no such
call and reports the state at ``GET /license/hcx/activationStatus``, because
HCX 9 takes its licence from vCenter. HCX takes the vCenter and NSX
passwords as base64 text. HCX 9.1 renamed the roles "System
Administrator" and "Enterprise Administrator" to "Migration Admin" and
"Migration User"; the role mapping keeps the names the Manager uses.

A secret is never an input: a change names a Vault reference, and the
value is read under the caller's identity just before the call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.hcx.appliance_api import (
    ApplianceApi,
    HcxApplianceError,
    HcxManagerSetupError,
    RefTarget,
    appliance_session,
    base64_text,
)
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import rows_of

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "APPLIANCE_VENDOR_CALLS",
    "LEGACY_4X_CALLS",
    "hcx_manager_activate_impl",
    "hcx_manager_config_show_impl",
    "hcx_manager_location_set_impl",
    "hcx_manager_nsx_set_impl",
    "hcx_manager_role_mapping_set_impl",
    "hcx_manager_vcenter_set_impl",
]

_VCENTER_PATH: Final = "/api/admin/global/config/vcenter"
_LOOKUP_SERVICE_PATH: Final = "/api/admin/global/config/lookupservice"
_NSX_PATH: Final = "/api/admin/global/config/nsx"
_LOCATION_PATH: Final = "/api/admin/global/config/location"
_SEARCH_CITIES_PATH: Final = "/api/admin/global/config/searchCities"
_ROLE_MAPPINGS_PATH: Final = "/api/admin/global/config/roleMappings"
_HCX_CONFIG_PATH: Final = "/api/admin/global/config/hcx"
_ACTIVATION_STATUS_PATH: Final = "/license/hcx/activationStatus"
_APPENGINE_PATH: Final = "/components/appengine"
_APPENGINE_STATUS_PATH: Final = "/components/appengine/status"

#: The appliance management calls (port 9443), as ``(method, path)``.
APPLIANCE_VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _VCENTER_PATH),
        ("POST", _VCENTER_PATH),
        ("GET", _LOOKUP_SERVICE_PATH),
        ("POST", _LOOKUP_SERVICE_PATH),
        ("GET", _NSX_PATH),
        ("POST", _NSX_PATH),
        ("GET", _LOCATION_PATH),
        ("PUT", _LOCATION_PATH),
        ("GET", _SEARCH_CITIES_PATH),
        ("GET", _ROLE_MAPPINGS_PATH),
        ("PUT", _ROLE_MAPPINGS_PATH),
        ("GET", _HCX_CONFIG_PATH),
        ("POST", _HCX_CONFIG_PATH),
        ("GET", _ACTIVATION_STATUS_PATH),
        ("POST", _APPENGINE_PATH),
        ("GET", _APPENGINE_STATUS_PATH),
    }
)

#: Calls that only HCX 4.x has (the key activation). The 9.1 reference lacks them.
LEGACY_4X_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("GET", _HCX_CONFIG_PATH), ("POST", _HCX_CONFIG_PATH)}
)

#: The activation server HCX 4.x activates against when the caller names none.
_DEFAULT_ACTIVATION_URL: Final = "https://connect.hcx.vmware.com"

#: Role name pairs: (name before HCX 9.1, name from HCX 9.1).
_SYSTEM_ADMIN_ROLES: Final = ("System Administrator", "Migration Admin")
_ENTERPRISE_ADMIN_ROLES: Final = ("Enterprise Administrator", "Migration User")

_LOCATION_FIELDS: Final = ("city", "country", "cityAscii", "province", "latitude", "longitude")
_SHOWN_IDS_MAX: Final = 10


def _step(name: str, state: str, error: str | None = None) -> dict[str, Any]:
    return {"step": name, "state": state, "error": error}


def _error_text(exc: Exception) -> str:
    """The text of a failed vendor call for a ``steps`` entry."""
    if isinstance(exc, HcxApplianceError):
        return f"HTTP {exc.http_status}: {exc.upstream_message}"
    return f"no answer from the HCX Manager: {type(exc).__name__}"


def _configs(payload: Any) -> list[dict[str, Any]]:
    """The ``config`` objects of a ``{"data": {"items": [{"config": ...}]}}`` answer."""
    configs = []
    for row in rows_of(payload):
        config = row.get("config")
        if isinstance(config, dict):
            configs.append(config)
    return configs


def _first_config(payload: Any) -> dict[str, Any] | None:
    configs = _configs(payload)
    return configs[0] if configs else None


def _vcenter_row(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return {
        "url": config.get("url"),
        "user_name": config.get("userName"),
        "name": config.get("name"),
        "version": config.get("version"),
        "build": config.get("buildNumber"),
        "vc_uuid": config.get("vcuuid"),
        "uuid": config.get("UUID"),
    }


def _lookup_row(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return {
        "url": config.get("lookupServiceUrl"),
        "provider_type": config.get("providerType"),
        "uuid": config.get("UUID"),
    }


def _nsx_row(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return {
        "url": config.get("url"),
        "user_name": config.get("userName"),
        "version": config.get("version"),
        "type": config.get("type"),
        "nsx_uuid": config.get("nsxuuid"),
        "uuid": config.get("UUID"),
    }


def _location_row(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not payload.get("city"):
        return None
    return {
        "city": payload.get("city"),
        "country": payload.get("country"),
        "province": payload.get("province"),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
    }


def _role_rows(payload: Any) -> list[dict[str, Any]]:
    """The role mappings as ``[{role, user_groups}]``, from a bare list or ``items``."""
    rows = payload if isinstance(payload, list) else rows_of(payload)
    return [
        {"role": row.get("role"), "user_groups": list(row.get("userGroups") or [])}
        for row in rows
        if isinstance(row, dict)
    ]


async def _activation_state(api: ApplianceApi) -> dict[str, Any]:
    """The activation state: the 4.x key config first, then the 9.x activation status."""
    config = await api.get_or_none(_HCX_CONFIG_PATH)
    if config is not None:
        item = _first_config(config) or {}
        activated = bool(item.get("activationKey"))
        return {
            "activated": activated,
            "status": "ACTIVATED" if activated else "NOT_ACTIVATED",
            "type": "activation_key",
            "source": "hcx_config",
            "server_url": item.get("url"),
            "grace_period_expiration": None,
        }
    status = await api.get_or_none(_ACTIVATION_STATUS_PATH)
    if status is not None:
        data = status.get("data") if isinstance(status, dict) else None
        data = data if isinstance(data, dict) else {}
        return {
            "activated": data.get("activationDone") is True,
            "status": data.get("activationStatus"),
            "type": data.get("activationType"),
            "source": "activation_status",
            "server_url": None,
            "grace_period_expiration": data.get("gracePeriodExpiration"),
        }
    return {
        "activated": None,
        "status": None,
        "type": None,
        "source": None,
        "server_url": None,
        "grace_period_expiration": None,
    }


async def hcx_manager_config_show_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.config.show`` -- vCenter, SSO, NSX, location, role mapping and activation.

    A section whose call the Manager does not have (``404``) is ``null`` and
    named in ``missing``.
    """
    del params
    async with appliance_session(connector, operator, target) as api:
        vcenter = await api.get_or_none(_VCENTER_PATH)
        lookup = await api.get_or_none(_LOOKUP_SERVICE_PATH)
        nsx = await api.get_or_none(_NSX_PATH)
        location = await api.get_or_none(_LOCATION_PATH)
        roles = await api.get_or_none(_ROLE_MAPPINGS_PATH)
        activation = await _activation_state(api)
    answers = {
        "vcenter": vcenter,
        "lookup_service": lookup,
        "nsx": nsx,
        "location": location,
        "role_mappings": roles,
    }
    missing = [name for name, answer in answers.items() if answer is None]
    if activation["source"] is None:
        missing.append("activation")
    return {
        "vcenter": _vcenter_row(_first_config(vcenter)),
        "lookup_service": _lookup_row(_first_config(lookup)),
        "nsx": _nsx_row(_first_config(nsx)),
        "location": _location_row(location),
        "role_mappings": _role_rows(roles),
        "activation": activation,
        "missing": missing,
    }


async def _read_secret_fields(
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
    param: str,
    fields: tuple[str, ...],
) -> dict[str, str]:
    """Read *fields* from the secret that *param* names, under the caller's identity."""
    ref = str(params.get(param) or "").strip()
    if not ref:
        raise HcxManagerSetupError(f"{param} is empty: name the Vault path of the secret")
    data = await load_param_secret_data(
        RefTarget(name=target.name, host=target.host, secret_ref=ref), operator, param=param
    )
    values: dict[str, str] = {}
    for field in fields:
        value = data.get(field) if isinstance(data, dict) else None
        text = strip_credential_value(value) if isinstance(value, str) else ""
        if not text:
            raise HcxManagerSetupError(
                f"the secret named by {param} has no text in the field {field!r}",
                remediation=f"Store the fields {', '.join(fields)} in that secret.",
            )
        values[field] = text
    return values


def _login_body(url: str, login: dict[str, str]) -> dict[str, Any]:
    """The vCenter or NSX config body: the password goes as base64 text."""
    config = {
        "url": url,
        "userName": login["username"],
        "password": base64_text(login["password"]),
    }
    return {"data": {"items": [{"config": config}]}}


async def hcx_manager_vcenter_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.vcenter.set`` -- register vCenter, then the SSO lookup service.

    Two steps. A failure at the first step raises: nothing changed. A
    failure at the second step returns ``status: failed`` with the steps;
    the vCenter registration stays.
    """
    login = await _read_secret_fields(
        operator, target, params, "vcenter_secret_ref", ("username", "password")
    )
    lookup_config = {"lookupServiceUrl": params["lookup_service_url"], "providerType": "PSC"}
    lookup_body = {"data": {"items": [{"config": lookup_config}]}}
    async with appliance_session(connector, operator, target) as api:
        vcenter = await api.call(
            "POST",
            _VCENTER_PATH,
            json=_login_body(params["vcenter_url"], login),
            secrets=(login["password"],),
        )
        try:
            lookup = await api.call("POST", _LOOKUP_SERVICE_PATH, json=lookup_body)
        except (HcxApplianceError, httpx.TransportError) as exc:
            return {
                "status": "failed",
                "vcenter": _vcenter_row(_first_config(vcenter)),
                "lookup_service": None,
                "steps": [
                    _step("register_vcenter", "done"),
                    _step("register_lookup_service", "failed", _error_text(exc)),
                ],
                "restart_needed": True,
            }
    return {
        "status": "done",
        "vcenter": _vcenter_row(_first_config(vcenter)),
        "lookup_service": _lookup_row(_first_config(lookup)),
        "steps": [_step("register_vcenter", "done"), _step("register_lookup_service", "done")],
        "restart_needed": True,
    }


async def hcx_manager_nsx_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.nsx.set`` -- ``POST /api/admin/global/config/nsx``."""
    login = await _read_secret_fields(
        operator, target, params, "nsx_secret_ref", ("username", "password")
    )
    async with appliance_session(connector, operator, target) as api:
        nsx = await api.call(
            "POST",
            _NSX_PATH,
            json=_login_body(params["nsx_url"], login),
            secrets=(login["password"],),
        )
    return {"status": "done", "nsx": _nsx_row(_first_config(nsx)), "restart_needed": True}


def _same(left: Any, right: str) -> bool:
    return isinstance(left, str) and left.strip().casefold() == right.strip().casefold()


def _city_label(row: dict[str, Any]) -> str:
    parts = [row.get("city"), row.get("province"), row.get("country")]
    return ", ".join(str(part) for part in parts if part)


def _pick_city(rows: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    """The one city row that matches the inputs; raises when none or several match."""
    city = str(params["city"])
    matches = [
        row for row in rows if _same(row.get("city"), city) or _same(row.get("cityAscii"), city)
    ]
    for field in ("country", "province"):
        wanted = params.get(field)
        if wanted:
            matches = [row for row in matches if _same(row.get(field), str(wanted))]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        close = "; ".join(_city_label(row) for row in rows[:_SHOWN_IDS_MAX]) or "none"
        raise HcxManagerSetupError(
            f"HCX's city list has no city {city!r} with the given country and province. "
            f"Names the search found: {close}",
            remediation="Check the spelling, or pick the nearest larger city from that list.",
        )
    labels = "; ".join(_city_label(row) for row in matches[:_SHOWN_IDS_MAX])
    raise HcxManagerSetupError(
        f"HCX's city list has {len(matches)} cities named {city!r}: {labels}",
        remediation="Set country, and province if needed, so only one city matches.",
    )


async def hcx_manager_location_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.location.set`` -- search the city, then ``PUT`` the one match."""
    async with appliance_session(connector, operator, target) as api:
        found = await api.get(_SEARCH_CITIES_PATH, params={"searchString": params["city"]})
        rows = found if isinstance(found, list) else rows_of(found)
        city = _pick_city([row for row in rows if isinstance(row, dict)], params)
        body = {field: city.get(field) for field in _LOCATION_FIELDS}
        await api.call("PUT", _LOCATION_PATH, json=body)
    return {"status": "done", "location": _location_row(body)}


def _role_name(current: list[dict[str, Any]], names: tuple[str, str]) -> str:
    """The name the Manager uses for a role: the one it already has, else by its naming style."""
    for row in current:
        if row["role"] in names:
            return str(row["role"])
    new_style = any(str(row["role"] or "").startswith("Migration ") for row in current)
    return names[1] if new_style else names[0]


def _merged_roles(current: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """The full role mapping to ``PUT``: the current one with the requested roles replaced."""
    merged = [dict(row) for row in current]
    for param, names in (
        ("system_admin_groups", _SYSTEM_ADMIN_ROLES),
        ("enterprise_admin_groups", _ENTERPRISE_ADMIN_ROLES),
    ):
        groups = params.get(param)
        if groups is None:
            continue
        role = _role_name(current, names)
        rows = [row for row in merged if row["role"] == role]
        if rows:
            rows[0]["user_groups"] = list(groups)
        else:
            merged.append({"role": role, "user_groups": list(groups)})
    return merged


async def hcx_manager_role_mapping_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.role_mapping.set`` -- read the mapping, change the named roles, ``PUT`` all.

    The vendor call replaces the whole mapping, so the roles the call does
    not name are sent back as they are.
    """
    async with appliance_session(connector, operator, target) as api:
        current = _role_rows(await api.get(_ROLE_MAPPINGS_PATH))
        merged = _merged_roles(current, params)
        body = [{"role": row["role"], "userGroups": row["user_groups"]} for row in merged]
        answer = await api.call("PUT", _ROLE_MAPPINGS_PATH, json=body)
    message = answer.get("message") if isinstance(answer, dict) else None
    return {"status": "done", "role_mappings": merged, "message": message}


async def hcx_manager_activate_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.manager.activate`` -- activate with a key (4.x), or report the state.

    An active Manager (for example one that took its licence from vCenter)
    answers ``already_active`` and reads no key. A Manager with only the
    9.x activation status is not activated by a key: the call refuses and
    says where the licence goes.
    """
    async with appliance_session(connector, operator, target) as api:
        state = await _activation_state(api)
        if state["activated"]:
            return {"status": "already_active", "activation": state}
        if state["source"] != "hcx_config":
            raise HcxManagerSetupError(
                "this HCX Manager is not active and has no activation key call: HCX 9 takes "
                "its licence from vCenter (VCF licensing)",
                remediation=(
                    "Add the VCF licence in vCenter, then read hcx.manager.config.show again."
                ),
            )
        key = (
            await _read_secret_fields(
                operator, target, params, "activation_key_secret_ref", ("activation_key",)
            )
        )["activation_key"]
        config = {"url": params.get("activation_server_url") or _DEFAULT_ACTIVATION_URL}
        config["activationKey"] = key
        await api.call(
            "POST", _HCX_CONFIG_PATH, json={"data": {"items": [{"config": config}]}}, secrets=(key,)
        )
        state = await _activation_state(api)
    return {"status": "activated", "activation": state}
