# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The seven cloud template ops, written once for both Automation connectors.

Each entry is a :class:`~.vcfa_content.ContentOpSpec`. Texts use ``{ns}`` for
the op id prefix (``vcfa.tenant`` on VCF Automation 9, ``vcfa.vra8`` on Aria
Automation 8). Reads are ``safe``; every change needs a person's approval.
The delete is ``destructive`` and has a preview builder (:mod:`.vcfa_templates`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_shared.vcfa_content import (
    ContentOpSpec,
    change_answer,
    field,
    flat_page,
    guidance,
    list_of,
    nullable,
    obj,
)
from meho_backplane.connectors.vmware_shared.vcfa_templates import (
    BLUEPRINT_PATH,
    BLUEPRINT_VALIDATION_PATH,
    BLUEPRINT_VERSIONS_PATH,
    BLUEPRINTS_PATH,
)

__all__ = ["PAGING_FIELDS", "PROJECT_ID_FIELD", "TEMPLATE_OPS"]

_STATUSES = "DRAFT, VERSIONED or RELEASED"

_ID: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "The template id (from the template list, or from {ns}.blueprint.create).",
}
PROJECT_ID_FIELD: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "The project id (from {ns}.project.list).",
}
PAGING_FIELDS: dict[str, Any] = {
    "$top": {"type": "integer", "minimum": 1, "description": "Optional number of rows to return."},
    "$skip": {"type": "integer", "minimum": 0, "description": "Optional number of rows to skip."},
}
_MESSAGES: dict[str, Any] = list_of(
    "Problems the template check found.",
    {
        "message": field("string", "What is wrong."),
        "type": field("string", "ERROR, WARNING or INFO."),
        "path": field(["string", "null"], "Where in the YAML."),
        "resourceName": field(["string", "null"], "The resource of the template."),
    },
)
_SUMMARY_FIELDS: dict[str, Any] = {
    "id": field(["string", "null"], "The template id."),
    "name": field(["string", "null"], "The template name."),
    "description": field(["string", "null"], "The template description."),
    "projectId": field(["string", "null"], "The project id."),
    "projectName": field(["string", "null"], "The project name."),
    "status": field(["string", "null"], f"The template state: {_STATUSES}."),
    "valid": field(["boolean", "null"], "True when the YAML has no errors."),
    "totalVersions": field(["integer", "null"], "How many versions it has."),
    "totalReleasedVersions": field(["integer", "null"], "How many versions are released."),
    "requestScopeOrg": field(
        ["boolean", "null"], "True when every project of the org may request it."
    ),
    "createdAt": field(["string", "null"], "When it was created."),
    "updatedAt": field(["string", "null"], "When it was last changed."),
    "validationMessages": _MESSAGES,
}
_VERSION_FIELDS: dict[str, Any] = {
    "id": field(["string", "null"], "The version id."),
    "version": field(["string", "null"], "The version label, for example '1.0'."),
    "status": field(["string", "null"], f"The version state: {_STATUSES}."),
    "versionDescription": field(["string", "null"], "The version description."),
    "versionChangeLog": field(["string", "null"], "The change log of the version."),
    "createdAt": field(["string", "null"], "When the version was made."),
    "createdBy": field(["string", "null"], "Who made the version."),
}

_BLUEPRINT_GET = ContentOpSpec(
    name="blueprint.get",
    handler_attr="vcfa_blueprint_get",
    path=BLUEPRINT_PATH,
    summary="Read one cloud template with its YAML and its inputs.",
    description=(
        "Reads GET /blueprint/api/blueprints/{blueprintId} and its inputs-schema. "
        "Returns the template fields, the draft YAML and the inputs a request needs."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"id": _ID},
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<template-id>"}],
    },
    response_schema=obj(
        "The template and its inputs.",
        {
            "blueprint": obj(
                "The template.",
                {**_SUMMARY_FIELDS, "content": field(["string", "null"], "The draft YAML.")},
            ),
            "inputs_schema": obj(
                "The inputs a request takes, as a JSON schema.",
                {
                    "properties": {
                        "type": "object",
                        "description": "One schema per input, by input name.",
                    },
                    "required": {
                        "type": "array",
                        "description": "The inputs a request must give.",
                        "items": {"type": "string", "description": "One input name."},
                    },
                },
            ),
        },
    ),
    group="templates",
    tags=("read-only", "vcfa", "blueprint", "template"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call="Call to see what a cloud template builds and which inputs it needs.",
        output_shape=(
            "{blueprint: {id, name, projectId, status, valid, totalVersions, "
            "totalReleasedVersions, content (YAML), validationMessages}, inputs_schema: "
            "{properties, required}}."
        ),
        next_step=(
            "To change the YAML call {ns}.blueprint.update; to see the versions call "
            "{ns}.blueprint.version.list."
        ),
    ),
)

_BLUEPRINT_CREATE = ContentOpSpec(
    name="blueprint.create",
    handler_attr="vcfa_blueprint_create",
    path=BLUEPRINTS_PATH,
    summary="Create a cloud template from YAML in a project.",
    description=(
        "Sends POST /blueprint/api/blueprints. The new template is a draft; it is in "
        "the catalog only after a released version. A template with this name in "
        "the project answers 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "project_id": PROJECT_ID_FIELD,
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "The template name, unique in the project.",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The template YAML (formatVersion, inputs, resources). Use a secret "
                    "input for a password, never the value."
                ),
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional template description.",
            },
            "request_scope_org": {
                "type": "boolean",
                "description": "True to let every project of the org request it. Default false.",
            },
        },
        "required": ["project_id", "name", "content"],
        "additionalProperties": False,
        "examples": [
            {
                "project_id": "<project-id>",
                "name": "<template-name>",
                "content": "formatVersion: 1\ninputs: {}\nresources: {}\n",
            }
        ],
    },
    response_schema=change_answer(
        ["created", "unchanged"], "blueprint", obj("The template.", _SUMMARY_FIELDS)
    ),
    group="templates",
    tags=("write", "vcfa", "blueprint", "template"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to add a new cloud template to a project.",
        preconditions=(
            "Check the YAML with {ns}.blueprint.validate first. A person must approve the call."
        ),
        output_shape="{status: created | unchanged, blueprint: {id, name, status, valid}, "
        "guidance}.",
        next_step="Release a version with {ns}.blueprint.version.release.",
    ),
)

_BLUEPRINT_UPDATE = ContentOpSpec(
    name="blueprint.update",
    handler_attr="vcfa_blueprint_update",
    path=BLUEPRINT_PATH,
    summary="Change the YAML, name or description of a cloud template.",
    description=(
        "Reads the template, then sends PUT /blueprint/api/blueprints/{blueprintId} "
        "with the changes. Only the draft changes; released versions stay. No "
        "difference answers 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": _ID,
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "The new template YAML. Use a secret input for a password.",
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "A new template name.",
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "A new template description.",
            },
            "request_scope_org": {
                "type": "boolean",
                "description": "True to let every project of the org request it.",
            },
        },
        "required": ["id"],
        "anyOf": [
            {"required": ["content"]},
            {"required": ["name"]},
            {"required": ["description"]},
            {"required": ["request_scope_org"]},
        ],
        "additionalProperties": False,
        "examples": [
            {"id": "<template-id>", "content": "formatVersion: 1\ninputs: {}\nresources: {}\n"}
        ],
    },
    response_schema=change_answer(
        ["updated", "unchanged"],
        "blueprint",
        obj("The template.", _SUMMARY_FIELDS),
        {
            "changed_fields": {
                "type": "array",
                "description": "The template fields the op changed.",
                "items": {"type": "string", "description": "One field name."},
            }
        },
    ),
    group="templates",
    tags=("write", "vcfa", "blueprint", "template"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to change what a cloud template builds.",
        preconditions=(
            "Read the template with {ns}.blueprint.get and check the new YAML with "
            "{ns}.blueprint.validate. A second person must approve the call."
        ),
        output_shape=(
            "{status: updated | unchanged, blueprint: {id, name, status, valid, "
            "validationMessages}, changed_fields, guidance}."
        ),
        next_step="Release a new version with {ns}.blueprint.version.release.",
    ),
)

_BLUEPRINT_DELETE = ContentOpSpec(
    name="blueprint.delete",
    handler_attr="vcfa_blueprint_delete",
    path=BLUEPRINT_PATH,
    summary="Delete a cloud template and all its versions.",
    description=(
        "Sends DELETE /blueprint/api/blueprints/{blueprintId}. The YAML and every "
        "version are gone, and released versions leave the catalog. The vendor refuses "
        "a template that still has deployments."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"id": _ID},
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<template-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string", "What the op did: deleted or unchanged.", enum=["deleted", "unchanged"]
            ),
            "id": field("string", "The template id."),
            "guidance": field("string", "A short note on what to do next."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group="templates",
    tags=("write", "vcfa", "blueprint", "template", "delete", "destructive"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to remove a cloud template nobody uses any more.",
        preconditions=(
            "Save the YAML with {ns}.blueprint.get first. Call preview_operation with the "
            "same inputs, then call_operation with its preview_hash. Only a person may run "
            "it, and a second person must approve."
        ),
        output_shape="{status: deleted | unchanged, id, guidance}.",
        next_step="Call {ns}.catalog.source.list to check the catalog import.",
    ),
)

_VERSION_LIST = ContentOpSpec(
    name="blueprint.version.list",
    handler_attr="vcfa_blueprint_version_list",
    path=BLUEPRINT_VERSIONS_PATH,
    summary="List the versions of a cloud template and which are released.",
    description=(
        "Reads GET /blueprint/api/blueprints/{blueprintId}/versions. Returns one flat "
        "row per version, without the YAML. A long list comes back as a result handle."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": _ID,
            "status": {
                "type": "string",
                "enum": ["VERSIONED", "RELEASED"],
                "description": "Optional: only versions in this state.",
            },
            **PAGING_FIELDS,
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<template-id>", "status": "RELEASED"}],
    },
    response_schema=flat_page("versions", _VERSION_FIELDS),
    group="templates",
    tags=("read-only", "vcfa", "blueprint", "template", "version"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call="Call to see which versions of a template exist and which are released.",
        output_shape=(
            "{content: [{id, version, status, versionDescription, versionChangeLog, "
            "createdAt, createdBy}], totalElements}."
        ),
        next_step="Release or unrelease a version with {ns}.blueprint.version.release.",
    ),
)

_VERSION_RELEASE = ContentOpSpec(
    name="blueprint.version.release",
    handler_attr="vcfa_blueprint_version_release",
    path=BLUEPRINT_VERSIONS_PATH,
    summary="Make a version of a cloud template and release it, or unrelease one.",
    description=(
        "release: a new version label is made from the draft and released; an existing "
        "version is released. unrelease: the version leaves the catalog. A version "
        "already in that state answers 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": _ID,
            "version": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "The version label, for example '1.0'.",
            },
            "action": {
                "type": "string",
                "enum": ["release", "unrelease"],
                "description": "release puts the version in the catalog; unrelease takes it out.",
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional description of a new version.",
            },
            "change_log": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional change log of a new version.",
            },
        },
        "required": ["id", "version", "action"],
        "additionalProperties": False,
        "examples": [{"id": "<template-id>", "version": "<version>", "action": "release"}],
    },
    response_schema=change_answer(
        ["created_and_released", "released", "unreleased", "unchanged"],
        "version",
        nullable(obj("The version; null when it does not exist.", _VERSION_FIELDS)),
    ),
    group="templates",
    tags=("write", "vcfa", "blueprint", "template", "version", "release"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to put a template version in the catalog, or to take it out.",
        preconditions=(
            "A new version copies the draft YAML as it is now; check it with "
            "{ns}.blueprint.validate. A second person must approve the call."
        ),
        output_shape=(
            "{status: created_and_released | released | unreleased | unchanged, version: "
            "{id, version, status}, guidance}."
        ),
        next_step=(
            "Make sure a content source imports the project's templates "
            "({ns}.catalog.source.list) and is shared ({ns}.catalog.share.list)."
        ),
    ),
)

_VALIDATE = ContentOpSpec(
    name="blueprint.validate",
    handler_attr="vcfa_blueprint_validate",
    path=BLUEPRINT_VALIDATION_PATH,
    summary="Check cloud template YAML for errors. It changes nothing.",
    description=(
        "Sends POST /blueprint/api/blueprint-validation with template YAML (content) or "
        "a stored template (blueprint_id). Returns valid and the problems found. "
        "Nothing is saved."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "project_id": PROJECT_ID_FIELD,
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "The template YAML to check. Give this or blueprint_id.",
            },
            "blueprint_id": {
                "type": "string",
                "minLength": 1,
                "description": "A stored template to check. Give this or content.",
            },
            "blueprint_version": {
                "type": "string",
                "minLength": 1,
                "description": "Optional version of the stored template; omit for the draft.",
            },
            "inputs": {
                "type": "object",
                "description": "Optional input values, by input name.",
                "additionalProperties": True,
            },
        },
        "required": ["project_id"],
        "oneOf": [{"required": ["content"]}, {"required": ["blueprint_id"]}],
        "additionalProperties": False,
        "examples": [
            {
                "project_id": "<project-id>",
                "content": "formatVersion: 1\ninputs: {}\nresources: {}\n",
            }
        ],
    },
    response_schema=obj(
        "The check result.",
        {
            "valid": field(["boolean", "null"], "True when the template has no errors."),
            "validationMessages": _MESSAGES,
        },
    ),
    group="templates",
    tags=("read-only", "vcfa", "blueprint", "template"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call="Call before {ns}.blueprint.create or {ns}.blueprint.update.",
        output_shape="{valid, validationMessages: [{type, message, path, resourceName}]}.",
        next_step="Fix each ERROR message, then create or update the template.",
    ),
)

#: The template ops, reads before changes.
TEMPLATE_OPS: Final[tuple[ContentOpSpec, ...]] = (
    _BLUEPRINT_GET,
    _VERSION_LIST,
    _VALIDATE,
    _BLUEPRINT_CREATE,
    _BLUEPRINT_UPDATE,
    _VERSION_RELEASE,
    _BLUEPRINT_DELETE,
)
