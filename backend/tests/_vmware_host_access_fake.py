# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared vim fake for the host local-user and Active Directory tests.

:class:`AccessVim` is the teardown fake (:class:`tests._vmware_teardown_fake.VimFake`)
plus the calls of the seven host access operations: the service content, the
user list, the access roles, the three account calls and the two domain
tasks. The changes change the fake store, so the handlers' reads after a call
see the result. It also answers the vCenter host list (``GET /vcenter/host``).
All names, ids and passwords are made up.
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.soap_fault import SoapFault, attach_soap_fault
from tests._vmware_teardown_fake import VimFake, moref

HOST = "host-1001"
ESXI_HOST = "ha-host"
AUTH_MANAGER = "authManager-1"
AD_STORE = "adAuth-1"
ACCESS_MANAGER = "hostAccessManager-1"
ACCOUNT_MANAGER = "ha-localacctmgr"
USER_DIRECTORY = "ha-user-directory"
PASSWORD = "<password-from-vault>"
DOMAIN = "corp.example"


class VcenterTarget:
    """A vCenter target (never probed, so it reads as a vCenter)."""

    name = "vcenter-1"
    host = "vcenter-1.test.invalid"
    fingerprint = None


class EsxiTarget:
    """A single ESXi host target (its probe says ``product: esxi``)."""

    name = "esxi-1"
    host = "esxi-1.test.invalid"
    fingerprint: ClassVar[dict[str, Any]] = {
        "product": "esxi",
        "reachable": True,
        "version": "8.0.3",
    }


def soap_fault_error(fault_type: str, text: str) -> RuntimeError:
    """The error the connector raises for a SOAP fault of the host."""
    err = RuntimeError(f"vmware vim call failed: vim fault {fault_type}: {text}")
    attach_soap_fault(
        err, SoapFault(faultcode="ServerFaultCode", faultstring=text, fault_type=fault_type)
    )
    return err


class AccessVim(VimFake):
    """The shared vim fake plus the host user and Active Directory calls."""

    def __init__(self) -> None:
        super().__init__()
        #: user name -> the vendor ``PosixUserSearchResult``.
        self.users: dict[str, dict[str, Any]] = {}
        #: user name -> access mode.
        self.roles: dict[str, str] = {}
        #: method -> the error a synchronous account call raises.
        self.faults: dict[str, Exception] = {}
        self.role_fault: Exception | None = None
        self.ad: dict[str, Any] = {"enabled": False}

    # -- setup -------------------------------------------------------------
    def seed(self, host: str = HOST, *, joined: str | None = None) -> None:
        self.add(
            "HostSystem",
            host,
            name="<esxi-host-name>",
            **{
                "configManager.authenticationManager": moref(
                    "HostAuthenticationManager", AUTH_MANAGER
                ),
                "configManager.hostAccessManager": moref("HostAccessManager", ACCESS_MANAGER),
            },
        )
        if joined is not None:
            self.ad = {
                "enabled": True,
                "joinedDomain": joined,
                "trustedDomain": ["<trusted-domain>"],
                "domainMembershipStatus": "ok",
            }
        self.add_user("root", uid=0, role="accessAdmin")
        self.add_user("svc-old", uid=1001, full_name="<old service account>")

    def add_user(
        self, name: str, *, uid: int, full_name: str | None = None, role: str | None = None
    ) -> None:
        self.users[name] = {
            "_typeName": "PosixUserSearchResult",
            "principal": name,
            "fullName": full_name,
            "group": False,
            "id": uid,
            "shellAccess": True,
        }
        if role is not None:
            self.roles[name] = role

    # -- the vCenter host list -------------------------------------------------
    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.calls.append((f"GET {path}", copy.deepcopy(params)))
        assert path == "/vcenter/host", path
        hosts = [
            {"host": moid, "name": props.get("name")}
            for (mo_type, moid), props in self.objects.items()
            if mo_type == "HostSystem"
        ]
        names = (params or {}).get("filter.names")
        ids = (params or {}).get("filter.hosts")
        if names:
            hosts = [h for h in hosts if h["name"] in names]
        if ids:
            hosts = [h for h in hosts if h["host"] in ids]
        return hosts

    # -- the seam ----------------------------------------------------------
    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        mo_type, moid, method = path.strip("/").split("/")
        if mo_type == "HostAuthenticationManager" or (
            method == "RetrievePropertiesEx"
            and json["specSet"][0]["propSet"][0]["type"] == "HostAuthenticationManager"
        ):
            self.objects[("HostAuthenticationManager", AUTH_MANAGER)] = {
                "info": {
                    "_typeName": "HostAuthenticationManagerInfo",
                    "authConfig": [
                        {"_typeName": "HostLocalAuthenticationInfo", "enabled": True},
                        {"_typeName": "HostActiveDirectoryInfo", **copy.deepcopy(self.ad)},
                    ],
                },
                "supportedStore": [
                    moref("HostLocalAuthentication", "localAuth-1"),
                    moref("HostActiveDirectoryAuthentication", AD_STORE),
                ],
            }
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            return await super()._post_vmomi_json(
                target,
                path,
                operator=operator,
                json=json,
                promote_managed_object_not_found=promote_managed_object_not_found,
                session_used=session_used,
                session_token=session_token,
            )
        self.calls.append((path, copy.deepcopy(json)))
        return handler(moid, json)

    def _m_RetrieveServiceContent(self, moid: str, body: Any) -> Any:  # noqa: N802
        return {
            "_typeName": "ServiceContent",
            "accountManager": moref("HostLocalAccountManager", ACCOUNT_MANAGER),
            "userDirectory": moref("UserDirectory", USER_DIRECTORY),
        }

    def _m_RetrieveUserGroups(self, moid: str, body: Any) -> Any:  # noqa: N802
        return [copy.deepcopy(user) for user in self.users.values()]

    def _m_RetrieveHostAccessControlEntries(self, moid: str, body: Any) -> Any:  # noqa: N802
        if self.role_fault is not None:
            raise self.role_fault
        return [
            {
                "_typeName": "HostAccessControlEntry",
                "principal": name,
                "group": False,
                "accessMode": mode,
            }
            for name, mode in self.roles.items()
        ]

    def _account_call(self, method: str, apply: Any) -> dict[str, Any]:
        if method in self.faults:
            raise self.faults[method]
        if self.apply_writes:
            apply()
        return {}

    def _m_CreateUser(self, moid: str, body: Any) -> Any:  # noqa: N802
        spec = body["user"]
        return self._account_call(
            "CreateUser",
            lambda: self.add_user(spec["id"], uid=1100, full_name=spec.get("description")),
        )

    def _m_UpdateUser(self, moid: str, body: Any) -> Any:  # noqa: N802
        return self._account_call("UpdateUser", lambda: None)

    def _m_RemoveUser(self, moid: str, body: Any) -> Any:  # noqa: N802
        def apply() -> None:
            self.users.pop(body["userName"], None)
            self.roles.pop(body["userName"], None)

        return self._account_call("RemoveUser", apply)

    def _m_JoinDomain_Task(self, moid: str, body: Any) -> Any:  # noqa: N802
        def apply() -> None:
            self.ad = {
                "enabled": True,
                "joinedDomain": body["domainName"].upper(),
                "domainMembershipStatus": "ok",
            }

        if "JoinDomain_Task" in self.faults:
            raise self.faults["JoinDomain_Task"]
        return self._task("JoinDomain_Task", apply=apply)

    def _m_LeaveCurrentDomain_Task(self, moid: str, body: Any) -> Any:  # noqa: N802
        def apply() -> None:
            self.ad = {"enabled": False}

        return self._task("LeaveCurrentDomain_Task", apply=apply)
