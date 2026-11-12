# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``policy.apply`` operation: import a whole policy file, after a second admin approves.

``meho policy import`` and ``POST /api/v1/policy/import`` plan the file
and then call this operation. The call waits for approval with the plan
on the approval card (:func:`build_policy_apply_preview`): the file's
SHA-256 and the SHA-256 of the stored policy it was planned against
(``based_on``). Nothing changes yet.

An import is a change of rights:

* the operation is ``dangerous`` and always waits; a person approves each
  call, and no standing grant and no run approval covers it;
* every request carries the rights-change mark, so the person who asked
  can never approve it, and the approver is another admin of the tenant;
* a person needs the ``tenant_admin`` role to ask.

When it runs (after the approval) it plans again
(:func:`~meho_backplane.policy.service.apply_policy`). When the stored
policy changed after the plan, it writes nothing and says so. The
install's policy (``scope: install``) is imported only when a platform
admin approves it.

The operation exists only while the ``POLICY_CLASSIFICATION`` switch is
on, like ``policy.classify``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.secondary_target_policy import approved_requester_sub
from meho_backplane.operations.typed_register import (
    register_typed_operation,
    remove_built_in_connector_rows,
)
from meho_backplane.policy.document import MAX_POLICY_BYTES
from meho_backplane.policy.service import apply_policy, build_plan
from meho_backplane.policy.store import classification_enabled
from meho_backplane.settings import parse_bool_env

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations._preview import PreviewContext
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "POLICY_APPLY_OP_ID",
    "SWITCH_OFF_IMPORT",
    "build_policy_apply_preview",
    "policy_apply",
    "register_policy_apply_operation",
]

#: The op id of a policy file import.
POLICY_APPLY_OP_ID: Final[str] = "policy.apply"

#: What an import says while the switch is off.
SWITCH_OFF_IMPORT: Final[str] = (
    "The policy file import is off on this MEHO. An admin turns it on with the "
    "POLICY_CLASSIFICATION setting."
)

#: Why an install-wide import did not run.
INSTALL_NEEDS_PLATFORM_ADMIN: Final[str] = (
    "The install's policy changes only when a platform admin approves it."
)

#: Why an import did not run: the login that asked for it ran it.
IMPORT_NEEDS_SECOND_ADMIN: Final[str] = (
    "A policy import runs only from a second admin's approval. Run the import again."
)

_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scope", "file", "file_sha256", "based_on"],
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["tenant", "install"],
            "description": "tenant: your tenant's policy. install: the whole install's policy.",
        },
        "file": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_POLICY_BYTES,
            "description": "The whole policy file (policy: v1), as text.",
        },
        "file_sha256": {**_SHA256, "description": "The SHA-256 of the file."},
        "based_on": {
            **_SHA256,
            "description": (
                "The SHA-256 of the stored policy the import was planned against, from "
                "the preview (POST /api/v1/policy/import?preview=true)."
            ),
        },
    },
    "examples": [
        {
            "scope": "tenant",
            "file": "policy: v1\nclassifications: []\n",
            "file_sha256": "<sha256-of-the-file>",
            "based_on": "<sha256-from-the-preview>",
        }
    ],
}

RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "scope": {"type": "string", "description": "tenant or install."},
        "applied": {"type": "boolean", "description": "False when the file changed nothing."},
        "message": {"type": "string", "description": "What happened, in one sentence."},
        "revision_id": {
            "type": ["string", "null"],
            "description": "The id of the new entry in `meho policy history`.",
        },
        "summary": {
            "type": "object",
            "description": "How many entries were added, changed and removed.",
        },
    },
    "required": ["scope", "applied", "message", "revision_id"],
}

_GROUP_KEY = "policy-file"
_GROUP_WHEN_TO_USE = (
    "Import a whole policy file (policy: v1): the categories and classifications of "
    "your tenant, or of the whole install. An import is a change of rights and waits "
    "until a second admin approves it. Use `meho policy import` to plan it first."
)

_DESCRIPTION = (
    "Replace the whole policy of your tenant (or of the install) with a policy file. "
    "A change of rights: it waits until another admin approves it, then checks that "
    "the stored policy did not change since the plan."
)

_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "An admin wants to import a policy file. Prefer `meho policy import FILE` or "
        "POST /api/v1/policy/import: they plan the file and fill file_sha256 and based_on."
    ),
    "preconditions": (
        "Needs the tenant_admin role. The call always waits for approval, and the person "
        "who asked can never approve it. scope install needs a platform admin to approve."
    ),
    "output_shape": "{'scope', 'applied', 'message', 'revision_id', 'summary'}",
    "next_step": "Ask another admin to approve the request; then `meho policy history`.",
}


async def policy_apply(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Import the file in *params* for the operator's tenant, or for the install.

    Op-id: ``policy.apply``. Targetless. Runs after the approval; the
    tenant comes from *operator*, never from *params*.

    Only the admin who approved runs it. A run by the login that asked
    (for example its re-run of a failed approved request) writes nothing,
    so the history never names the importer as the approver.
    """
    if not classification_enabled():
        raise ValueError(SWITCH_OFF_IMPORT)
    importer = approved_requester_sub()
    if importer is None or importer == operator.sub:
        raise ValueError(IMPORT_NEEDS_SECOND_ADMIN)
    scope = params["scope"]
    if scope == "install" and not operator.platform_admin:
        raise ValueError(INSTALL_NEEDS_PLATFORM_ADMIN)
    async with get_sessionmaker()() as session, session.begin():
        return await apply_policy(
            session,
            scope=scope,
            tenant_id=operator.tenant_id,
            content=str(params["file"]).encode("utf-8"),
            expected_sha256=params["file_sha256"],
            based_on=params["based_on"],
            imported_by=importer,
            approved_by=operator.sub,
        )


async def build_policy_apply_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The plan on the approval card: what the file adds, changes and removes."""
    params = ctx.params
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope=params["scope"],
            tenant_id=ctx.operator.tenant_id,
            content=str(params["file"]).encode("utf-8"),
        )
    return {
        "scope": plan.scope,
        "file_sha256": plan.file_sha256,
        "based_on": plan.based_on,
        "planned_against_current": plan.based_on == params.get("based_on"),
        "mistakes": plan.mistakes,
        "warnings": plan.warnings,
        "no_change": plan.no_change,
        "diff": plan.diff,
    }


async def register_policy_apply_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``policy.apply`` typed op; delete it while the switch is off."""
    if not parse_bool_env(os.environ.get("POLICY_CLASSIFICATION")):
        await remove_built_in_connector_rows(
            product="policy", version="1.x", impl_id="policy-registry"
        )
        return
    await register_typed_operation(
        product="policy",
        version="1.x",
        impl_id="policy-registry",
        op_id=POLICY_APPLY_OP_ID,
        handler=policy_apply,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Import a whole policy file, after a second admin approves it.",
        description=_DESCRIPTION,
        parameter_schema=PARAMETER_SCHEMA,
        response_schema=RESPONSE_SCHEMA,
        tags=["policy", "write", "rights"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
