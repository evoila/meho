# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for ``sddc.host.validate`` and ``sddc.host.commission`` (#4850).

Both ops send a list of ``HostCommissionSpec``, and each item holds the
ESXi user's ``password``. So both are declared ``credential_write``
(``sddc_manager/op_classes.py``), and the
generic echo of params is turned off for them. Without these builders the
approver saw only the op id and the target, not which hosts change.

The preview is ``{action, host_count, hosts}``. Each host shows only
``fqdn``, ``username``, ``storageType`` and ``networkPoolId``. The builder
copies those four fields by name and nothing else, so ``password`` (and any
other field a caller adds to the open spec) never reaches the stored
approval request. It does no network calls.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.sddc_manager.typed_writes import (
    SDDC_HOST_COMMISSION_OP_ID,
    SDDC_HOST_VALIDATE_OP_ID,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

#: The ``HostCommissionSpec`` fields the approver sees. Never ``password``.
_SHOWN_HOST_FIELDS: tuple[str, ...] = ("fqdn", "username", "storageType", "networkPoolId")


def _host_rows(ctx: PreviewContext) -> list[dict[str, Any]]:
    """One row per host spec with only :data:`_SHOWN_HOST_FIELDS`."""
    spec = ctx.params.get("spec")
    if not isinstance(spec, list):
        return []
    return [
        {field: item.get(field) for field in _SHOWN_HOST_FIELDS}
        for item in spec
        if isinstance(item, dict)
    ]


def _host_spec_preview(action: str) -> Any:
    """Build the preview builder for one of the two host ops."""

    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        hosts = _host_rows(ctx)
        return {
            "resource": "esxi_hosts",
            "action": action,
            "host_count": len(hosts),
            "hosts": hosts,
        }

    return _builder


def _register_sddc_host_preview_builders() -> None:
    """Wire the two host previews. Called at import time."""
    register_preview_builder(SDDC_HOST_VALIDATE_OP_ID, _host_spec_preview("validate"))
    register_preview_builder(SDDC_HOST_COMMISSION_OP_ID, _host_spec_preview("commission"))


_register_sddc_host_preview_builders()
