# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and guidance of ``vmware.migration.inventory``."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.inventory.contracts import inventory_json_schema

__all__ = [
    "MIGRATION_GROUP_WHEN_TO_USE",
    "MIGRATION_INVENTORY_DESCRIPTION",
    "MIGRATION_INVENTORY_LLM_INSTRUCTIONS",
    "MIGRATION_INVENTORY_OP_ID",
    "MIGRATION_INVENTORY_PARAMETER_SCHEMA",
    "MIGRATION_INVENTORY_RESPONSE_SCHEMA",
    "MIGRATION_INVENTORY_SUMMARY",
]

MIGRATION_INVENTORY_OP_ID: Final = "vmware.migration.inventory"

MIGRATION_INVENTORY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vcf_operations_target": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": (
                "Name of a VCF Operations target that watches this vCenter. When set, "
                "utilization comes from there (latest values only). When not set, it "
                "comes from vCenter's own performance history."
            ),
        },
        "utilization_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "default": 30,
            "description": "Days of vCenter performance history to read. Default 30.",
        },
        "cluster": {
            "type": "string",
            "pattern": "^domain-c[0-9]+$",
            "description": "Only the VMs on the hosts of this cluster (id, e.g. 'domain-c8').",
        },
        "folder": {
            "type": "string",
            "pattern": "^group-[a-z][0-9]+$",
            "description": "Only the VMs in this VM folder and below it (id, e.g. 'group-v3').",
        },
    },
    "additionalProperties": False,
    "not": {"required": ["cluster", "folder"]},
    "examples": [
        {},
        {"cluster": "domain-c8", "utilization_days": 7},
        {"vcf_operations_target": "<vcf-operations-target>"},
    ],
}

MIGRATION_INVENTORY_RESPONSE_SCHEMA: dict[str, Any] = inventory_json_schema()

MIGRATION_INVENTORY_SUMMARY: Final = (
    "Read one vCenter as a migration inventory: one server record per VM, with counts."
)

MIGRATION_INVENTORY_DESCRIPTION: Final = (
    "Reads every VM of the vCenter (or of one cluster or VM folder): CPU, memory, disks, "
    "NICs, OS, boot type, power state, host, cluster and utilization. Templates and "
    "unreadable VMs are listed as omissions. Flags duplicates and checks the counts. "
    "Read-only."
)

MIGRATION_GROUP_WHEN_TO_USE: Final = (
    "Use to collect a vCenter's VMs for a migration assessment, in MEHO's common "
    "inventory shape (vmware.migration.inventory, safe). The result is always "
    "stored, and its handle feeds the migration operations (for example "
    "inventory.export)."
)

MIGRATION_INVENTORY_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to list a vCenter's VMs for a migration to Azure or another platform: "
        "sizes, disks, OS, boot type and measured use, one record per VM."
    ),
    "parameter_hints": {
        "vcf_operations_target": (
            "Optional. A VCF Operations target name from list_targets. It gives only "
            "the latest values; leave it out for a history window from vCenter."
        ),
        "utilization_days": "Optional, 1 to 365, default 30.",
        "cluster": "Optional cluster id. Do not combine with folder.",
        "folder": "Optional VM folder id. Do not combine with cluster.",
    },
    "output_shape": (
        "One inventory: {schema_version, source, records: [server records], omissions, "
        "counts: {server, source_totals, reconciliation: [{kind, source_total, records, "
        "omitted, difference, matches}]}, duplicates}. A small inventory comes back "
        "whole, with a handle next to it. A large one is stored whole and the answer "
        "is a short summary with the handle. Both handles feed the migration operations."
    ),
    "next_step": (
        "Pass the handle to inventory.export for an import file, for example format "
        "'azure-migrate-csv'. Read a "
        "stored inventory with result_query and a path, for example '/counts' or "
        "'/duplicates'. "
        "Check counts.reconciliation (matches true, else name the difference; empty "
        "when the count read failed, then an omission says so) and the duplicates."
    ),
    # The migration operations load the inventory by its handle, so a small
    # inventory (one VM) is stored too and comes back whole with a handle (#5143).
    "result_handle": {"always": True},
}
