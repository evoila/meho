# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared fake for the ``vm.migrate_cross_vcenter`` tests: two vCenters, one connector.

:class:`CrossVimFake` is the teardown vim fake of the source vCenter. The
other vCenter is :attr:`CrossVimFake.dest`, a second store, read through
:class:`FakeDestination`, the stand-in for the pinned session
(``open_destination``). A ``RelocateVM_Task`` start records the call and
returns a new task that runs. Names and ids are made up.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import _objects
from meho_backplane.connectors.vmware_rest.composites._vm_cross_vcenter_destination import (
    DestinationCertificateMismatchError,
    certificate_thumbprint,
)
from meho_backplane.connectors.vmware_rest.vim_body import retrieve_properties_body
from tests._vmware_teardown_fake import VimFake, moref

VM = "vm-42"
SOURCE_NAME = "example-vcenter-a"
DEST_NAME = "example-vcenter-b"
#: A made-up certificate and its SHA-1 hash in the form vCenter shows.
CERT = b"example-certificate-der"
THUMBPRINT = ":".join(f"{b:02X}" for b in hashlib.new("sha1", CERT, usedforsecurity=False).digest())
DEST_UUID = "1a2b3c4d-0000-4000-8000-00000000000b"
LOGIN = {"username": "<user>", "password": "<password>"}


@dataclass
class Target:
    """The target shape the handler reads (a source or a destination vCenter)."""

    name: str
    host: str
    product: str = "vmware"
    port: int | None = 443
    fqdn: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    fingerprint: Any = None
    secret_ref: str | None = "vsphere/example"
    auth_model: str | None = "shared_service_account"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))


def source_target() -> Target:
    return Target(name=SOURCE_NAME, host="vc-a.example.test")


def dest_target(**extras: Any) -> Target:
    return Target(
        name=DEST_NAME,
        host="vc-b.example.test",
        extras={"ssl_thumbprint": THUMBPRINT, **extras},
    )


_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


class FakeDestination:
    """Stand-in for the pinned session on the other vCenter: reads come from *store*."""

    def __init__(self, store: VimFake, login: dict[str, str]) -> None:
        self.store = store
        self.login = login
        self.instance_uuid = DEST_UUID
        self.closed = False

    async def read_object(
        self, *, mo_type: str, moid: str, props: list[str]
    ) -> dict[str, Any] | None:
        body = retrieve_properties_body(mo_type, [moid], props)
        self.store.calls.append((_RETRIEVE_PATH, copy.deepcopy(body)))
        try:
            result = self.store._retrieve(body, True)
        except ConnectorResourceNotFoundError:
            return None
        found = _objects(result).get(moid)
        return found[0] if found is not None else None

    async def aclose(self) -> None:
        self.closed = True


class CrossVimFake(VimFake):
    """The source vCenter's vim fake; :attr:`dest` is the other vCenter's store.

    :meth:`open_destination` replaces the real one in the tests: it checks
    :attr:`cert` against the target's thumbprint (the way the pinned
    connection does), reads the login through ``_session_loader``, then
    answers with a :class:`FakeDestination`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dest = VimFake()
        self.start_fault: Exception | None = None
        self.login_fault: Exception | None = None
        self.content_fault: Exception | None = None
        self.cert: bytes | Exception = CERT
        self.loader_calls: list[str] = []
        self.sessions: list[FakeDestination] = []

    async def _session_loader(self, target: Any, operator: Operator) -> dict[str, str]:
        self.loader_calls.append(target.name)
        if self.login_fault is not None:
            raise self.login_fault
        return dict(LOGIN)

    def _target_is_esxi(self, target: Any) -> bool:
        return False

    async def open_destination(
        self, connector: Any, dest: Any, operator: Operator, thumbprint: str
    ) -> FakeDestination:
        assert connector is self
        if isinstance(self.cert, Exception):
            raise self.cert
        shown = certificate_thumbprint(self.cert)
        if shown != thumbprint:
            raise DestinationCertificateMismatchError(thumbprint, shown)
        login = await self._session_loader(dest, operator)
        if self.content_fault is not None:
            raise self.content_fault
        session = FakeDestination(self.dest, login)
        self.sessions.append(session)
        return session

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
        assert getattr(target, "name", None) != DEST_NAME, (
            "the other vCenter is read on the pinned session, not on the connector"
        )
        method = path.rsplit("/", 1)[-1]
        if method == "RelocateVM_Task":
            self.calls.append((path, copy.deepcopy(json)))
            if self.start_fault is not None:
                raise self.start_fault
            moid = f"task-{len(self._tasks) + 1}"
            self._tasks[moid] = {"state": "running", "descriptionId": "VirtualMachine.relocate"}
            return moref("Task", moid)
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
            session_used=session_used,
            session_token=session_token,
        )

    def task(self, moid: str, **info: Any) -> None:
        self._tasks[moid] = info

    def starts(self) -> list[tuple[str, Any]]:
        return self.calls_to("RelocateVM_Task")


def nic(key: int, portgroup_key: str = "dvportgroup-5") -> dict[str, Any]:
    """A ``VirtualVmxnet3`` card on a distributed port group of the source vCenter."""
    return {
        "_typeName": "VirtualVmxnet3",
        "key": key,
        "macAddress": "00:50:56:00:00:01",
        "backing": {
            "_typeName": "VirtualEthernetCardDistributedVirtualPortBackingInfo",
            "port": {
                "_typeName": "DistributedVirtualSwitchPortConnection",
                "switchUuid": "source-switch-uuid",
                "portgroupKey": portgroup_key,
            },
        },
    }


def seed(conn: CrossVimFake, *, nics: int = 1) -> None:
    """A running VM with *nics* network cards; the other vCenter has every target object."""
    devices: list[dict[str, Any]] = [nic(4000 + i) for i in range(nics)]
    devices.append({"_typeName": "VirtualDisk", "key": 2000, "backing": {"_typeName": "x"}})
    conn.add(
        "VirtualMachine",
        VM,
        **{"name": "<vm-name>", "config.hardware.device": devices, "recentTask": []},
    )
    dest = conn.dest
    dest.add("HostSystem", "host-21", name="<host-name>")
    dest.add("ResourcePool", "resgroup-8", name="Resources")
    dest.add("Datastore", "datastore-12", name="<datastore-name>")
    dest.add("Folder", "group-v3", name="vm")
    dest.add(
        "DistributedVirtualPortgroup",
        "dvportgroup-34",
        **{
            "name": "<portgroup-name>",
            "key": "dvportgroup-34",
            "config.distributedVirtualSwitch": moref("VmwareDistributedVirtualSwitch", "dvs-7"),
        },
    )
    dest.add("VmwareDistributedVirtualSwitch", "dvs-7", uuid="dest-switch-uuid")
    dest.add("Network", "network-15", name="<network-name>")


def params(**extra: Any) -> dict[str, Any]:
    return {
        "vm": VM,
        "destination_target": DEST_NAME,
        "host": "host-21",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-12",
        "folder": "group-v3",
        "network": "dvportgroup-34",
        **extra,
    }
