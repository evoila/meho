# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``spaces.import`` operation: bring a site's ownership records into spaces.

An administrator sends a manifest (the labs, their target names, owner and
users, and the people with an account). The operation writes spaces,
members and assignments through the same store and audit path as the other
writes. ``only`` imports a part of the manifest. The approval card shows the
preview (:func:`build_import_preview`): the counts and every conflict.
The manifest builder stays at the site; this import is generic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.spaces.common import require_enabled
from meho_backplane.connectors.spaces.import_manifest import parse_manifest
from meho_backplane.connectors.spaces.import_plan import (
    BUCKETS,
    ImportResult,
    apply_import,
    preview_import,
)
from meho_backplane.connectors.spaces.writes import _reason
from meho_backplane.db.engine import get_sessionmaker

if TYPE_CHECKING:
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "IMPORT_GUIDANCE",
    "IMPORT_OP_ID",
    "IMPORT_PARAMETERS",
    "IMPORT_RESPONSE",
    "build_import_preview",
    "spaces_import",
]

IMPORT_OP_ID: Final[str] = "spaces.import"

_PERSON: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "issuer": {"type": "string", "description": "Keycloak issuer of the person."},
        "subject": {"type": "string", "description": "Keycloak subject of the person."},
        "display_name": {"type": "string", "description": "Name to show. Not stored."},
    },
    "required": ["issuer", "subject"],
}

IMPORT_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "manifest": {
            "type": "object",
            "description": (
                "The site's ownership records: `environments` (labs, each with name, "
                "targets, owner and users) and `people` (the accounts that exist)."
            ),
            "properties": {
                "environments": {
                    "type": "array",
                    "description": "The labs: name, target names, owner and users of each.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Name of the lab."},
                            "targets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Names of MEHO targets that belong to the lab.",
                            },
                            "owner": {
                                "anyOf": [_PERSON, {"type": "array", "items": _PERSON}],
                                "description": (
                                    "The owner of the lab: one person. A list of two or more "
                                    "people is accepted and listed as a conflict."
                                ),
                            },
                            "users": {
                                "type": "array",
                                "items": _PERSON,
                                "description": "The other people who work in the lab.",
                            },
                        },
                        "required": ["name"],
                    },
                },
                "people": {
                    "type": "array",
                    "items": _PERSON,
                    "description": "The accounts that exist. Every owner and user must be here.",
                },
            },
            "required": ["environments"],
        },
        "only": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Import only the labs with these names. Leave out to import all.",
        },
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "Why you import. Required. It is saved with every change.",
        },
    },
    "required": ["manifest", "reason"],
    "additionalProperties": False,
    "examples": [
        {
            "manifest": {
                "environments": [
                    {
                        "name": "<lab-name>",
                        "targets": ["<target-name>"],
                        "owner": {"issuer": "<issuer-url>", "subject": "<subject>"},
                        "users": [{"issuer": "<issuer-url>", "subject": "<subject>"}],
                    }
                ],
                "people": [
                    {"issuer": "<issuer-url>", "subject": "<subject>", "display_name": "<name>"}
                ],
            },
            "only": ["<lab-name>"],
            "reason": "<why you import>",
        }
    ],
}

_COUNTS: Final[dict[str, Any]] = {
    "type": "object",
    "description": "Per kind (space, person, assignment, member): rows per bucket.",
    "additionalProperties": {
        "type": "object",
        "properties": {b: {"type": "integer"} for b in BUCKETS},
    },
}

IMPORT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "imported"},
        "total_rows": {"type": "integer", "description": "Input rows. Equals the sum of counts."},
        "counts": _COUNTS,
        "conflict_count": {"type": "integer", "description": "Rows that were not imported."},
        "labs_written": {"type": "integer", "description": "Labs that got new records."},
        "conflicts": {
            "type": "array",
            "description": "Every row that was not imported, with the code and the reason.",
            "items": {"type": "object"},
        },
    },
    "required": ["status", "total_rows", "counts", "conflict_count", "conflicts"],
}

IMPORT_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": "A site hands you its list of labs, owners and targets, and you load it once.",
    "preconditions": (
        "Needs a reason. A person needs the `tenant_admin` role. The call waits until "
        "a `tenant_admin` approves it; the approval card shows the preview. The setting "
        "SPACES_ENABLED must be on. The people list must hold every owner and user."
    ),
    "parameter_hints": {
        "only": "Start with one lab, check the conflicts, then import the rest.",
        "manifest": "Targets by MEHO name. Owners and users by issuer and subject.",
    },
    "output_shape": (
        "{'status', 'total_rows', 'counts': {kind: {created, changed, unchanged, conflict}}, "
        "'conflict_count', 'labs_written', 'conflicts': [{kind, ref, lab, code, detail}]}"
    ),
    "next_step": (
        "Fix each conflict at the site or with `spaces.assign` and run the import again: "
        "a second run changes nothing that is already in place. Run `spaces.overview` to check."
    ),
    "result_scalars": {"keys": ["status", "total_rows", "conflict_count", "labs_written"]},
}


def _answer(result: ImportResult) -> dict[str, Any]:
    conflicts = result.conflicts()
    return {
        "total_rows": len(result.rows),
        "counts": result.counts(),
        "conflict_count": len(conflicts),
        "labs_written": result.labs_written,
        "conflicts": conflicts,
    }


async def spaces_import(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Write the manifest into spaces, one transaction per lab.

    Op-id: ``spaces.import``. Safe to run twice: the second run changes
    nothing that is already in place.
    """
    require_enabled()
    reason = _reason(params)
    manifest = parse_manifest(params.get("manifest"), params.get("only"))
    result = await apply_import(
        operator.tenant_id, manifest, changed_by=operator.sub, reason=reason
    )
    return {"status": "imported", **_answer(result)}


async def build_import_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the import would do: counts per kind and every conflict. Writes nothing."""
    require_enabled()
    manifest = parse_manifest(ctx.params.get("manifest"), ctx.params.get("only"))
    async with get_sessionmaker()() as session:
        result = await preview_import(session, ctx.operator.tenant_id, manifest)
    return _answer(result)
