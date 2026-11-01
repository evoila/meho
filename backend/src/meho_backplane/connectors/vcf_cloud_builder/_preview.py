# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the approver sees for a Cloud Builder change that waits for approval.

The three changes are credential writes, so MEHO shows no generic copy of
their params. These builders show what changes instead: the spec's identity
(SDDC id, vCenter, SDDC Manager, hosts, networks) and which spec fields get a
value from which Vault path. They read only a fixed list of fields, so they
never show a password or a licence key. Registered when this package loads.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_cloud_builder.typed_writes import (
    SDDC_RETRY_OP_ID,
    SDDC_START_OP_ID,
    VALIDATION_START_OP_ID,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = ["spec_summary"]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def spec_summary(spec: Any) -> dict[str, Any]:
    """The identity and network fields of a bring-up spec. No secret field is read."""
    spec = _dict(spec)
    hosts = [_dict(h) for h in _list(spec.get("hostSpecs"))]
    return {
        "sddc_id": spec.get("sddcId"),
        "vcenter_hostname": _dict(spec.get("vcenterSpec")).get("vcenterHostname"),
        "sddc_manager_hostname": _dict(spec.get("sddcManagerSpec")).get("hostname"),
        "host_count": len(hosts),
        "host_names": [h["hostname"] for h in hosts if isinstance(h.get("hostname"), str)],
        "networks": [
            {key: _dict(n).get(key) for key in ("networkType", "vlanId", "subnet")}
            for n in _list(spec.get("networkSpecs"))
        ],
    }


def _secret_sources(params: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"path": _dict(ref).get("path"), "secret_ref": _dict(ref).get("secret_ref")}
        for ref in _list(params.get("secrets"))
    ]


def _builder(operation: str, what_changes: str) -> Any:
    async def build(ctx: PreviewContext) -> dict[str, Any] | None:
        preview: dict[str, Any] = {
            "operation": operation,
            "target": str(getattr(ctx.target, "name", "")),
            "what_changes": what_changes,
        }
        if ctx.params.get("id"):
            preview["bringup_id"] = ctx.params["id"]
        if "spec" in ctx.params:
            preview["spec"] = spec_summary(ctx.params.get("spec"))
            preview["secrets_from_vault"] = _secret_sources(ctx.params)
        return preview

    return build


register_preview_builder(
    VALIDATION_START_OP_ID,
    _builder(
        "Check a VCF 5.x bring-up spec on Cloud Builder",
        "Cloud Builder checks the spec. It deploys nothing.",
    ),
)
register_preview_builder(
    SDDC_START_OP_ID,
    _builder(
        "Start a VCF 5.x management domain bring-up",
        "Cloud Builder deploys vCenter, NSX and SDDC Manager on the listed hosts. "
        "It runs for hours and is not undone by MEHO.",
    ),
)
register_preview_builder(
    SDDC_RETRY_OP_ID,
    _builder(
        "Retry a failed VCF 5.x bring-up",
        "Cloud Builder runs the bring-up again from its failed task, with the stored "
        "spec or the fixed spec below.",
    ),
)
