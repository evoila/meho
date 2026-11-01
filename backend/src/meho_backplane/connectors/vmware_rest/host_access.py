# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared reads of the host local users and the host Active Directory domain.

The two typed reads (:mod:`.typed_ops_host_access`) and the five changes
(:mod:`.composites._host_local_users`, :mod:`.composites._host_ad`) use these
helpers, so a read and a change see the host the same way.

* **Local users** live in the host's own API: ``HostLocalAccountManager`` and
  the host's ``UserDirectory`` are on the ESXi service instance, not on
  vCenter. So the local-user operations need the ESXi host itself as the
  target (MEHO's single-host path, with the host's credentials from Vault),
  also when a vCenter manages the host. On a vCenter target they stop with
  ``needs_esxi_target`` before any call.
* **Active Directory** is on ``HostSystem.configManager.authenticationManager``,
  which vCenter and the single host both serve. On a vCenter target the call
  names the host; on an ESXi target the host is the target.

Every call goes through ``_post_vmomi_json``: VI-JSON on vCenter, SOAP on a
single host (:mod:`.soap_host_access`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.soap_fault import soap_fault_of
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    STANDALONE_ESXI_HOST_MOID,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import hide_secrets
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    fault_message,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "AD_STATE_PROPERTIES",
    "AD_STORE_TYPE",
    "HOST_ACCESS_GROUP_KEY",
    "HOST_ACCESS_WHEN_TO_USE",
    "HOST_PARAMETER",
    "OP_CREATE_USER",
    "OP_JOIN_DOMAIN",
    "OP_LEAVE_DOMAIN",
    "OP_REMOVE_USER",
    "OP_RETRIEVE_ACCESS_ENTRIES",
    "OP_RETRIEVE_SERVICE_CONTENT",
    "OP_RETRIEVE_USER_GROUPS",
    "OP_UPDATE_USER",
    "PROTECTED_USERS",
    "AccountManagers",
    "AdState",
    "find_user",
    "needs_esxi_target",
    "read_access_modes",
    "read_account_managers",
    "read_ad_state",
    "read_local_users",
    "vendor_fault",
]

HOST_ACCESS_GROUP_KEY: Final = "vmware-host-access"

HOST_ACCESS_WHEN_TO_USE: Final = (
    "Use for the local users of an ESXi host and its Active Directory domain. Reads: list "
    "the local users and their access role (host.local_user.list, safe); see if the host "
    "is in a domain, and which one (host.ad.status, safe). Changes: create a local user "
    "(host.local_user.create, dangerous); set a local user's password "
    "(host.local_user.password.set, dangerous); delete a local user "
    "(host.local_user.delete, destructive); join the host to a domain (host.ad.join, "
    "dangerous); leave the domain (host.ad.leave, dangerous). The local-user operations need "
    "the ESXi host itself as the target; the domain operations work through vCenter too. "
    "Passwords come from Vault, never as plain text."
)

#: The ``host`` parameter of the reads and changes that work through vCenter.
HOST_PARAMETER: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "description": (
        "Host name or moid as vCenter shows it, such as '<host-name>' or 'host-21'. "
        "Required on a vCenter target; ignored on an ESXi target (the host is the target)."
    ),
}

#: The host fields of the Active Directory answers.
AD_STATE_PROPERTIES: dict[str, Any] = {
    "host": {"type": ["string", "null"], "description": "Host moid (ha-host on an ESXi target)."},
    "joined": {
        "type": ["boolean", "null"],
        "description": "True when the host is in an Active Directory domain.",
    },
    "domain": {
        "type": ["string", "null"],
        "description": "The domain the host joined; null when not joined.",
    },
    "trusted_domains": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Domains the joined domain trusts.",
    },
    "membership_status": {
        "type": ["string", "null"],
        "description": (
            "Health of the membership: ok, noServers, clientTrustBroken, serverTrustBroken, "
            "inconsistentTrust, otherProblem or unknown. null when not joined."
        ),
    },
}

# The vendor calls (``METHOD:/path`` of the pinned vi-json.yaml).
OP_RETRIEVE_SERVICE_CONTENT: Final = "POST:/ServiceInstance/{moId}/RetrieveServiceContent"
OP_RETRIEVE_USER_GROUPS: Final = "POST:/UserDirectory/{moId}/RetrieveUserGroups"
OP_RETRIEVE_ACCESS_ENTRIES: Final = (
    "POST:/HostAccessManager/{moId}/RetrieveHostAccessControlEntries"
)
OP_CREATE_USER: Final = "POST:/HostLocalAccountManager/{moId}/CreateUser"
OP_UPDATE_USER: Final = "POST:/HostLocalAccountManager/{moId}/UpdateUser"
OP_REMOVE_USER: Final = "POST:/HostLocalAccountManager/{moId}/RemoveUser"
OP_JOIN_DOMAIN: Final = "POST:/HostActiveDirectoryAuthentication/{moId}/JoinDomain_Task"
OP_LEAVE_DOMAIN: Final = "POST:/HostActiveDirectoryAuthentication/{moId}/LeaveCurrentDomain_Task"

#: Local users MEHO never deletes: without root, nobody can log in to fix the host.
PROTECTED_USERS: Final = frozenset({"root"})

#: The vim fault for a refused user name and password.
LOGIN_FAULT_TYPE: Final = "InvalidLogin"

AD_STORE_TYPE: Final = "HostActiveDirectoryAuthentication"
_AD_INFO_TYPE: Final = "HostActiveDirectoryInfo"
_AUTH_MANAGER_TYPE: Final = "HostAuthenticationManager"
_SERVICE_CONTENT_PATH: Final = "/ServiceInstance/ServiceInstance/RetrieveServiceContent"
_RETRIEVE_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_list(value: Any) -> list[Any]:
    value = unwrap_vim_value(value)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def needs_esxi_target(target: Any) -> dict[str, Any] | None:
    """Why the target cannot run a local-user operation, or ``None`` when it can."""
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return refusal
    if flavor == HOST_FLAVOR_ESXI:
        return None
    return {
        "status": "needs_esxi_target",
        "guidance": (
            "Local users live on the ESXi host, not on vCenter. Register the host as its own "
            "target (with its own credentials in Vault) and call again with that target. "
            "Nothing was sent."
        ),
    }


def _request_login_fault(exc: ConnectorAuthError) -> BaseException | None:
    """The fault to read when *exc* is an ``InvalidLogin`` of the call itself.

    Both transports turn an ``InvalidLogin`` fault into a
    :class:`ConnectorAuthError`. One from a session login (``stage``
    ``establish`` or ``after_relogin``) is the target's credential, and
    stays an auth error. One from a call on a live session (``stage``
    ``call``) can only be a login the call itself carried, because a lost
    session answers ``NotAuthenticated``, never ``InvalidLogin``. Returns
    the error that carries the fault (the SOAP error itself, or the HTTP
    error under a VI-JSON one), else ``None``.
    """
    if exc.stage != "call":
        return None
    soap = soap_fault_of(exc)
    if soap is not None:
        return exc if soap.fault_type == LOGIN_FAULT_TYPE else None
    cause = exc.__cause__
    if exc.cause == f"vim_fault_{LOGIN_FAULT_TYPE}" and isinstance(cause, httpx.HTTPStatusError):
        return cause
    return None


def vendor_fault(
    exc: Exception, secrets: tuple[str, ...], *, request_login: bool = False
) -> tuple[str | None, str] | None:
    """``(fault type, text)`` of a vendor fault, without any text that holds a secret.

    A VI-JSON fault is an HTTP error with a fault body; a SOAP fault is a
    ``RuntimeError`` that carries the parsed fault. ``None`` for anything else
    (a login problem, a lost connection): the caller lets it go on to the
    dispatcher.

    ``request_login``: the call carries a login of its own (the join account
    of ``JoinDomain_Task``). Then an ``InvalidLogin`` fault of the call is a
    vendor fault too: the vendor refused that login, not the target's
    credential (:func:`_request_login_fault`). The dispatcher would
    otherwise tell the caller to fix the target's credentials.
    """
    if isinstance(exc, ConnectorAuthError):
        carrier = _request_login_fault(exc) if request_login else None
        if carrier is None:
            return None
        exc = carrier  # type: ignore[assignment]
    soap = soap_fault_of(exc)
    if soap is not None:
        text = hide_secrets(soap.faultstring, secrets) or soap.fault_type or "SOAP fault"
        return soap.fault_type or soap.faultcode, text
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    fault = unwrap_vim_value(body) if isinstance(body, dict) else None
    if not isinstance(fault, dict):
        return None, f"HTTP {exc.response.status_code}"
    fault_type = fault.get(VIM_TYPE_NAME_KEY)
    text = hide_secrets(fault_message(fault), secrets) or f"HTTP {exc.response.status_code}"
    return (fault_type if isinstance(fault_type, str) else None), text


# ---------------------------------------------------------------------------
# Local users (ESXi target only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountManagers:
    """The ids of the host's account manager and user directory."""

    account_manager: str
    user_directory: str


async def read_account_managers(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> AccountManagers:
    """Read the account manager and the user directory from the service content."""
    content = await connector._post_vmomi_json(target, _SERVICE_CONTENT_PATH, operator=operator)
    content = unwrap_vim_value(content)
    managers: dict[str, str] = {}
    for key in ("accountManager", "userDirectory"):
        ref = content.get(key) if isinstance(content, dict) else None
        moid = _text(ref.get("value")) if isinstance(ref, dict) else None
        if moid is None:
            raise RuntimeError(
                f"the host's service content has no {key}; is the target an ESXi host?"
            )
        managers[key] = moid
    return AccountManagers(managers["accountManager"], managers["userDirectory"])


def _user_row(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or item.get("group") is True:
        return None
    name = _text(item.get("principal"))
    if name is None:
        return None
    uid = item.get("id")
    shell = item.get("shellAccess")
    return {
        "user": name,
        "full_name": _text(item.get("fullName")),
        "uid": int(uid) if isinstance(uid, (int, str)) and str(uid).lstrip("-").isdigit() else None,
        "shell_access": shell if isinstance(shell, bool) else None,
    }


async def read_local_users(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    user_directory: str,
) -> list[dict[str, Any]]:
    """The local users of the host, sorted by name (``UserDirectory.RetrieveUserGroups``)."""
    payload = await connector._post_vmomi_json(
        target,
        f"/UserDirectory/{user_directory}/RetrieveUserGroups",
        operator=operator,
        json={"searchStr": "", "exactMatch": False, "findUsers": True, "findGroups": False},
    )
    rows = [row for row in map(_user_row, _as_list(payload)) if row is not None]
    return sorted(rows, key=lambda row: str(row["user"]))


async def read_access_modes(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> dict[str, str]:
    """``{user: access mode}`` of the users with an access role on the host.

    ``HostAccessManager.RetrieveHostAccessControlEntries``; groups are left
    out. The access mode is ``accessAdmin``, ``accessReadOnly``,
    ``accessNoAccess`` or ``accessOther``.
    """
    from meho_backplane.connectors.vmware_rest.composites._host import (
        _resolve_config_manager_moid,
    )

    manager = await _resolve_config_manager_moid(
        connector,
        target,
        operator,
        host_moid=STANDALONE_ESXI_HOST_MOID,
        prop="configManager.hostAccessManager",
    )
    if manager is None:
        raise RuntimeError("the host has no configManager.hostAccessManager")
    payload = await connector._post_vmomi_json(
        target,
        f"/HostAccessManager/{manager}/RetrieveHostAccessControlEntries",
        operator=operator,
    )
    modes: dict[str, str] = {}
    for entry in _as_list(payload):
        if not isinstance(entry, dict) or entry.get("group") is True:
            continue
        principal = _text(entry.get("principal"))
        mode = _text(unwrap_vim_value(entry.get("accessMode")))
        if principal is not None and mode is not None:
            modes[principal] = mode
    return modes


def find_user(users: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The row of user *name* (exact match, as ESXi compares names)."""
    return next((row for row in users if row["user"] == name), None)


# ---------------------------------------------------------------------------
# Active Directory (vCenter or ESXi target)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdState:
    """One read of a host's Active Directory membership."""

    host: str
    store: str | None
    joined: bool
    domain: str | None
    trusted_domains: tuple[str, ...]
    membership_status: str | None

    def as_fields(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "joined": self.joined,
            "domain": self.domain,
            "trusted_domains": list(self.trusted_domains),
            "membership_status": self.membership_status,
        }


def _ad_info(info: Any) -> dict[str, Any]:
    info = unwrap_vim_value(info)
    configs = info.get("authConfig") if isinstance(info, dict) else None
    for item in _as_list(configs):
        if isinstance(item, dict) and item.get(VIM_TYPE_NAME_KEY) == _AD_INFO_TYPE:
            return item
    return {}


async def read_ad_state(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host: str | None,
) -> tuple[AdState | None, dict[str, Any] | None]:
    """Read the host's domain membership: ``(state, None)`` or ``(None, refusal)``.

    Resolves the host (by name or moid on vCenter; the host itself on an ESXi
    target) and its ``configManager.authenticationManager``, then reads the
    manager's ``info`` and ``supportedStore`` in one call. The refusal is the
    host resolution envelope (``host_required``, ``host_not_found``,
    ``ambiguous_host``, ``unsupported_host_target``,
    ``config_manager_unreadable``).
    """
    from meho_backplane.connectors.vmware_rest.composites._host import (
        _resolve_host_and_manager,
    )

    host_moid, manager, refusal = await _resolve_host_and_manager(
        connector, target, operator, host=host, prop="configManager.authenticationManager"
    )
    if refusal is not None or host_moid is None or manager is None:
        return None, refusal or {"status": "host_not_found", "host": host}
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PATH,
        operator=operator,
        json=retrieve_properties_body(_AUTH_MANAGER_TYPE, [manager], ["info", "supportedStore"]),
    )
    props = _manager_props(result)
    store = next(
        (
            _text(ref.get("value"))
            for ref in _as_list(props.get("supportedStore"))
            if isinstance(ref, dict) and ref.get("type") == AD_STORE_TYPE
        ),
        None,
    )
    info = _ad_info(props.get("info"))
    trusted = tuple(t for t in _as_list(info.get("trustedDomain")) if isinstance(t, str))
    return (
        AdState(
            host=host_moid,
            store=store,
            joined=info.get("enabled") is True,
            domain=_text(info.get("joinedDomain")),
            trusted_domains=trusted,
            membership_status=_text(unwrap_vim_value(info.get("domainMembershipStatus"))),
        ),
        None,
    )


def _manager_props(result: Any) -> dict[str, Any]:
    payload = unwrap_vim_value(result)
    objects = payload.get("objects") if isinstance(payload, dict) else payload
    props: dict[str, Any] = {}
    for obj in _as_list(objects):
        if not isinstance(obj, dict):
            continue
        for entry in _as_list(obj.get("propSet")):
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                props[entry["name"]] = entry.get("val")
    return props
