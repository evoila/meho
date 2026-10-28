# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for the appliance feature flags (provider plane).

* ``vcfa.provider.feature_flag.list`` -- safe: ``GET /cloudapi/1.0.0/featureFlags``
* ``vcfa.provider.feature_flag.set`` -- caution, approval: find by name, ``GET``
  ``/cloudapi/1.0.0/featureFlags/{id}`` (state + ``ETag``), then ``PUT`` with
  ``If-Match`` only when the state differs

VCF Automation 9.1 refuses a VM Apps (classic tenant) org while the flag
``CLASSIC_TENANT_CREATION`` is off, so these ops let the tenant set-up turn
it on through MEHO, with approval and audit. Handlers live in
:mod:`._feature_flags`. Same :class:`~.typed_ops.VcfaTypedOp` shape and
import-time plane check as the other VCFA ops.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import (
    cloudapi_page,
    field,
    guidance,
    obj,
)
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_FEATURE_FLAG_PATH,
    PROVIDER_FEATURE_FLAGS_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["VCFA_FEATURE_FLAG_OPS"]

_FLAG_FIELDS: dict[str, Any] = {
    "id": field("string", "The flag id, for example 'urn:vcloud:featureflag:<NAME>'."),
    "name": field("string", "The flag name, for example 'CLASSIC_TENANT_CREATION'."),
    "displayName": field("string", "The display name, for example 'Classic Tenant Creation'."),
    "displayDescription": field("string", "What the flag does."),
    "usage": field("string", "Where the flag is used."),
    "enabled": field("boolean", "True when the flag is on."),
}

_FEATURE_FLAG_LIST = VcfaTypedOp(
    op_id="vcfa.provider.feature_flag.list",
    handler_attr="provider_feature_flag_list",
    plane="provider",
    path=PROVIDER_FEATURE_FLAGS_PATH,
    summary="List the VCF Automation feature flags and whether each is on (provider plane).",
    description=(
        "Lists the appliance feature flags with GET /cloudapi/1.0.0/featureFlags: "
        "id, name, display name and whether the flag is on. For example, a VM Apps "
        "org needs CLASSIC_TENANT_CREATION on."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "page": {
                "type": "integer",
                "minimum": 1,
                "description": "The page number, starting at 1.",
            },
            "pageSize": {
                "type": "integer",
                "minimum": 1,
                "maximum": 128,
                "description": "Items per page (at most 128).",
            },
        },
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=cloudapi_page("feature flags", _FLAG_FIELDS),
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "feature-flag"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see which appliance feature flags are on, for example before "
            "vcfa.provider.org.create makes a VM Apps org."
        ),
        output_shape=(
            "{values: [{id, name, displayName, displayDescription, usage, enabled}], "
            "resultTotal, pageCount, page, pageSize}."
        ),
        next_step="Turn a flag on or off with vcfa.provider.feature_flag.set.",
    ),
)

_FEATURE_FLAG_SET = VcfaTypedOp(
    op_id="vcfa.provider.feature_flag.set",
    handler_attr="provider_feature_flag_set",
    plane="provider",
    path=PROVIDER_FEATURE_FLAG_PATH,
    summary="Turn one VCF Automation feature flag on or off (provider plane); safe to run twice.",
    description=(
        "Finds the flag by name, reads it with GET /cloudapi/1.0.0/featureFlags/{id}, "
        "and sends PUT with If-Match (the ETag of that read) only when the flag is not "
        "in the requested state yet. A flag already in that state answers 'unchanged' "
        "and nothing is written."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "The flag: its name (e.g. 'CLASSIC_TENANT_CREATION'), display name "
                    "or id. Case does not matter."
                ),
            },
            "enabled": {"type": "boolean", "description": "True to turn it on, false for off."},
        },
        "required": ["name", "enabled"],
        "additionalProperties": False,
        "examples": [{"name": "CLASSIC_TENANT_CREATION", "enabled": True}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did: changed, unchanged or invalid_request.",
                enum=["changed", "unchanged", "invalid_request"],
            ),
            "feature_flag": {
                **obj(
                    "The flag after the op; null on invalid_request.",
                    {key: _FLAG_FIELDS[key] for key in ("id", "name", "displayName", "enabled")},
                ),
                "type": ["object", "null"],
            },
            "previous_enabled": field(
                ["boolean", "null"], "Whether the flag was on before the op; null if not found."
            ),
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "feature-flag"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call when an appliance feature flag must change, for example to turn on "
            "CLASSIC_TENANT_CREATION before vcfa.provider.org.create makes a VM Apps org."
        ),
        preconditions=(
            "Find the exact flag name with vcfa.provider.feature_flag.list. A flag changes "
            "the whole appliance, not one org. A person must approve the call."
        ),
        output_shape=(
            "{status: changed | unchanged | invalid_request, feature_flag: {id, name, "
            "displayName, enabled}, previous_enabled, guidance}. 'unchanged' means the "
            "flag was already in that state; 'invalid_request' means no flag has that "
            "name. In both cases nothing was written."
        ),
        next_step="Run the op that needed the flag again, for example vcfa.provider.org.create.",
    ),
)

#: The feature-flag ops: the read, then the change.
VCFA_FEATURE_FLAG_OPS: Final[tuple[VcfaTypedOp, ...]] = (_FEATURE_FLAG_LIST, _FEATURE_FLAG_SET)


validate_typed_ops(VCFA_FEATURE_FLAG_OPS)
