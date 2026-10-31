# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The metadata of the five integration ops, for both Automation connectors.

Each text uses ``{ns}`` where the op id prefix goes;
:func:`.vcfa_integration_schema.render` puts in ``vcfa.tenant`` (connector
``vcfa-rest-9.0``) or ``vcfa.vra8`` (connector ``vcfa-vra8-8.0``).
:mod:`.vcfa_integration_mixin` registers the rendered ops.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_shared.vcfa_git_sources import CONTENT_TYPES
from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    CONTENT_SOURCES_PATH,
    INTEGRATION_PATH,
    INTEGRATIONS_PATH,
    IPAM_PACKAGE_IMPORT_PATH,
)
from meho_backplane.connectors.vmware_shared.vcfa_integration_schema import (
    ASYNC_HINT,
    INTEGRATION,
    JOB,
    SECRET_REF,
    STEPS,
    TRACKER,
    WAIT,
    IntegrationOpSpec,
    field,
    obj,
    rows,
    strings,
)
from meho_backplane.connectors.vmware_shared.vcfa_integrations import INTEGRATION_TYPES

__all__ = ["GROUP_TEXT", "INTEGRATION_OPS"]


#: The group text. Each connector renders ``{ns}``.
GROUP_TEXT: Final[str] = (
    "Integrations of the organization and Git content sources. Read: the integrations "
    "(Orchestrator, IPAM, Git and others), the Git sources with their last sync, and the "
    "state of a sync or of a vendor request ({ns}.integration.list). Changes: add or change "
    "an Orchestrator, IPAM or Git integration after the vendor's test "
    "({ns}.integration.set), remove one ({ns}.integration.delete), import an IPAM provider "
    "package ({ns}.ipam_package.import), and add a Git repository to a project and sync "
    "it, or remove it ({ns}.git_repo.set). A person must approve each change."
)


_LIST = IntegrationOpSpec(
    name="integration.list",
    handler_attr="vcfa_integration_list",
    path=INTEGRATIONS_PATH,
    summary="List the integrations of the organization, its Git sources, or the state of one job.",
    description=(
        "Reads GET /iaas/api/integrations (or one by id), the Git content sources with their "
        "last sync (GET /content/api/sources), one Git sync request, or one request tracker. "
        "Flat rows; no password or token."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "view": field(
                "string",
                "integrations (the default) or git_sources (the Git content sources).",
                enum=["integrations", "git_sources"],
            ),
            "integration_type": field(
                "string", "Only integrations of this vendor type, for example 'ipam'."
            ),
            "id": field("string", "Read only this integration.", minLength=1),
            "sync_request_id": field("string", "Read the state of this Git sync.", minLength=1),
            "request_tracker_id": field(
                "string", "Read the state of this vendor request tracker.", minLength=1
            ),
        },
        "additionalProperties": False,
        "examples": [{"view": "integrations", "integration_type": "ipam"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "view": field(
                "string",
                "What was read.",
                enum=[
                    "integrations",
                    "git_sources",
                    "integration",
                    "sync_request",
                    "request_tracker",
                ],
            ),
            "rows": rows(
                "One flat row per integration or Git content source.",
                {
                    "id": field(["string", "null"], "The id."),
                    "name": field(["string", "null"], "The name."),
                    "integration_type": field(["string", "null"], "Integrations: the vendor type."),
                    "state": field(
                        ["string", "null"], "Integrations: the vendor's state, when given."
                    ),
                    "repository": field(["string", "null"], "Git sources: the repository."),
                    "branch": field(["string", "null"], "Git sources: the branch."),
                    "last_sync": field(
                        ["object", "null"], "Git sources: the last sync and its status."
                    ),
                },
            ),
            "total": field("integer", "The number of rows."),
            "job": JOB,
        },
        "required": ["view", "rows", "total"],
        "additionalProperties": True,
    },
    tags=("read-only", "vcfa", "integrations"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which Orchestrator, IPAM and Git integrations an organization has, "
            "which Git repositories feed it, and to follow a Git sync "
            "(sync_request_id, the status read of {ns}.git_repo.set) or a vendor request "
            "(request_tracker_id). Give at most one of id, sync_request_id, request_tracker_id."
        ),
        "output_shape": (
            "{view, rows: [flat rows], total, job}. job is set for sync_request_id or "
            "request_tracker_id: {kind, job_id, state, progress, message, done, checked_at}. "
            "state not_reachable means the system did not answer; it is not a failure. A 404 "
            "on the integration list on VCF Automation 9 means an 'All Apps' organization."
        ),
        "next_step": (
            "Read a job again until done is true. Use {ns}.integration.set to add or change "
            "an integration and {ns}.git_repo.set to add a Git repository."
        ),
        "result_scalars": {"keys": ["view", "total"]},
    },
)

_SET = IntegrationOpSpec(
    name="integration.set",
    handler_attr="vcfa_integration_set",
    path=INTEGRATIONS_PATH,
    summary="Add or change an Orchestrator, IPAM or Git integration, after the vendor's own test.",
    description=(
        "Runs POST /iaas/api/integrations?validateOnly=true (the vendor's test), then saves "
        "with POST or PATCH /iaas/api/integrations, following each request tracker. Saves "
        "only when the test passed. The password or token comes from Vault."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": field("string", "Change this integration. Leave out to add one.", minLength=1),
            "name": field("string", "The integration name.", minLength=1),
            "integration_type": field(
                "string", "The vendor type of a new integration.", enum=list(INTEGRATION_TYPES)
            ),
            "description": field("string", "A description."),
            "user_name": field("string", "The user name of the login (IPAM and Orchestrator)."),
            **SECRET_REF,
            "integration_properties": strings(
                "The vendor's integration properties, names to text. vro: hostName (the "
                "Orchestrator URL). ipam: providerId and faasProviderEndpointId (the id of the "
                "abx.endpoint integration), hostName. Git: url. Never a password."
            ),
            "certificate": field(
                "string",
                "The PEM certificate of the system. MEHO puts it where the type reads it.",
            ),
            "custom_properties": strings("Extra vendor custom properties, names to text."),
            "wait_seconds": WAIT,
        },
        "required": ["name"],
        "anyOf": [{"required": ["id"]}, {"required": ["integration_type"]}],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<integration-name>",
                "integration_type": "vro",
                "integration_properties": {"hostName": "https://<orchestrator-host>:443"},
                "user_name": "<user-name>",
                "password_secret_ref": "<vault-path>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "saved, test_failed (nothing saved), failed, unfinished or exists (the name is "
                "taken; nothing changed).",
                enum=["saved", "test_failed", "failed", "unfinished", "exists"],
            ),
            "integration": INTEGRATION,
            "test": TRACKER,
            "save": TRACKER,
            "steps": STEPS,
            "notes": {
                "type": "array",
                "description": "Notes for the person.",
                "items": {"type": "string"},
            },
            "guidance": field("string", "What to do next."),
        },
        "required": ["status", "steps", "guidance"],
        "additionalProperties": True,
    },
    tags=("write", "vcfa", "integrations"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to connect Orchestrator, an IPAM provider or a Git server to an "
            "organization, or to change one (give id). MEHO runs the vendor's test first and "
            "saves only when it passed. " + ASYNC_HINT
        ),
        "preconditions": (
            "Store the password or token in Vault and pass password_secret_ref. IPAM: import "
            "the provider with {ns}.ipam_package.import first; pass user_name at the top level "
            "and providerId and faasProviderEndpointId in integration_properties. A person "
            "must approve the call."
        ),
        "output_shape": (
            "{status, integration, test, save, steps, notes, guidance}. steps lists test, save "
            "and read with done, failed (with the vendor's error), unknown or not_run."
        ),
        "next_step": (
            "After a lost reply, read {ns}.integration.list before you call again. On "
            "test_failed fix the address, login or certificate. On VCF Automation 9 run it on a "
            "'VM Apps' organization."
        ),
        "result_scalars": {"keys": ["status", "guidance"]},
    },
)

_DELETE = IntegrationOpSpec(
    name="integration.delete",
    handler_attr="vcfa_integration_delete",
    path=INTEGRATION_PATH,
    summary="Remove an integration, after listing what uses it.",
    description=(
        "Lists what uses the integration (its external IP ranges, the network profiles with "
        "them, content sources) and stops unless confirm_in_use is true. Then sends DELETE "
        "/iaas/api/integrations/{id} and follows the request tracker."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": field("string", "The integration id.", minLength=1),
            "confirm_in_use": field(
                "boolean",
                "Remove it even when other objects use it. Set only after a person agreed.",
                default=False,
            ),
            "wait_seconds": WAIT,
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<integration-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "removed, unchanged (no such id), in_use (nothing removed), failed or unfinished.",
                enum=["removed", "unchanged", "in_use", "failed", "unfinished"],
            ),
            "integration": INTEGRATION,
            "users": rows(
                "What uses the integration.",
                {
                    "kind": field("string", "ip_range, network_profile or content_source."),
                    "id": field(["string", "null"], "Its id."),
                    "name": field(["string", "null"], "Its name."),
                },
            ),
            "tracker": TRACKER,
            "steps": STEPS,
            "guidance": field("string", "What to do next."),
        },
        "required": ["status", "steps", "guidance"],
        "additionalProperties": True,
    },
    tags=("write", "vcfa", "integrations"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to disconnect an Orchestrator, IPAM or Git integration. Only a person runs "
            "it: call preview_operation with the same inputs, then call_operation with its "
            "preview_hash. " + ASYNC_HINT
        ),
        "preconditions": (
            "Read the integration with {ns}.integration.list. Show the person the users in "
            "the preview; set confirm_in_use only after they agreed."
        ),
        "output_shape": "{status, integration, users, tracker, steps, guidance}.",
        "next_step": (
            "On in_use, ask the person. After a lost reply, read {ns}.integration.list with "
            "the id: a 404 there means it is removed."
        ),
        "result_scalars": {"keys": ["status", "guidance"]},
    },
)

_IMPORT = IntegrationOpSpec(
    name="ipam_package.import",
    handler_attr="vcfa_ipam_package_import",
    path=IPAM_PACKAGE_IMPORT_PATH,
    summary="Import an IPAM provider package (a zip from a URL) so an IPAM integration can use it.",
    description=(
        "Downloads the zip, checks it, uploads it with the vendor's resumable upload to "
        "/provisioning/ipam/api/providers/packages/import, imports it and reads the "
        "providers. The package code runs at every IP allocation."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "package_url": field(
                "string",
                "The https:// URL of the zip. No login, query or fragment; no redirect followed.",
                minLength=9,
            ),
            "expected_sha256": field(
                "string",
                "The SHA-256 the zip must have (64 hex characters). Strongly advised.",
                pattern="^[A-Fa-f0-9]{64}$(?!\\n)",
            ),
        },
        "required": ["package_url"],
        "additionalProperties": False,
        "examples": [{"package_url": "https://<file-server>/<ipam-package>.zip"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "imported, unchanged (that provider version is there) or failed.",
                enum=["imported", "unchanged", "failed"],
            ),
            "package": obj(
                "The downloaded package.",
                {
                    "file_name": field("string", "The file name."),
                    "size": field("integer", "Bytes."),
                    "sha256": field(["string", "null"], "The SHA-256 MEHO counted."),
                    "source": field("string", "The URL without query (scheme, host, path)."),
                    "target": field("string", "The vendor path it went to."),
                    "provider_name": field(["string", "null"], "From registration.yaml."),
                    "provider_version": field(["string", "null"], "From registration.yaml."),
                },
                nullable=True,
            ),
            "provider": obj(
                "The imported provider.",
                {
                    "id": field(["string", "null"], "Use it as providerId of an IPAM integration."),
                    "name": field(["string", "null"], "The provider name."),
                    "version": field(["string", "null"], "The provider version."),
                },
                nullable=True,
            ),
            "steps": STEPS,
            "leftovers_removed": {
                "type": "array",
                "description": "Temporary files of an earlier run that died, removed now.",
                "items": {"type": "string"},
            },
            "temp_file_removed": field("boolean", "True when MEHO removed its temporary file."),
            "guidance": field("string", "What to do next."),
        },
        "required": ["status", "steps", "guidance"],
        "additionalProperties": True,
    },
    tags=("write", "vcfa", "integrations", "ipam"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call before an IPAM integration, to bring the provider's package into the "
            "organization. Only a person runs it, after preview_operation and with its "
            "preview_hash. " + ASYNC_HINT
        ),
        "preconditions": (
            "The zip is on an https URL MEHO can reach without a login. Give expected_sha256 "
            "from the vendor's download page. A person must approve it."
        ),
        "output_shape": (
            "{status, package: {file_name, size, sha256, source, target, ...}, provider: {id, "
            "name, version}, steps, leftovers_removed, temp_file_removed, guidance}."
        ),
        "next_step": (
            "Pass provider.id as integration_properties.providerId to {ns}.integration.set. "
            "After a lost reply or a restart, run it again: an imported version answers "
            "unchanged."
        ),
        "result_scalars": {"keys": ["status", "guidance"]},
    },
)

_GIT = IntegrationOpSpec(
    name="git_repo.set",
    handler_attr="vcfa_git_repo_set",
    path=CONTENT_SOURCES_PATH,
    summary="Add a Git repository to a project as a content source and sync it now, or remove it.",
    description=(
        "Add: POST /content/api/sources (Git integration, repository, branch, folder, content "
        "type), then POST /content/api/sourcecontrol/sync-requests. Answers accepted with the "
        "sync id. Remove: DELETE /content/api/sources/{id}."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "action": field("string", "add or remove.", enum=["add", "remove"]),
            "id": field("string", "remove: the content source id.", minLength=1),
            "project_id": field("string", "add: the project id.", minLength=1),
            "name": field("string", "add: the content source name.", minLength=1),
            "integration_id": field("string", "add: the id of the Git integration.", minLength=1),
            "repository": field("string", "add: the repository, as owner/name.", minLength=3),
            "branch": field("string", "add: the branch.", minLength=1),
            "path": field("string", "add: the folder in the repository (empty for the top)."),
            "content_type": field(
                "string",
                "add: what the folder holds: cloud templates, ABX scripts or Terraform files.",
                enum=list(CONTENT_TYPES),
            ),
            "sync_enabled": field(
                "boolean", "add: let the vendor sync it by itself (default true)."
            ),
            "description": field("string", "add: a description."),
        },
        "required": ["action"],
        "oneOf": [
            {
                "properties": {"action": {"const": "add", "description": "Add a repository."}},
                "required": [
                    "project_id",
                    "name",
                    "integration_id",
                    "repository",
                    "branch",
                    "content_type",
                ],
            },
            {
                "properties": {"action": {"const": "remove", "description": "Remove a source."}},
                "required": ["id"],
            },
        ],
        "additionalProperties": False,
        "examples": [
            {
                "action": "add",
                "project_id": "<project-id>",
                "name": "<source-name>",
                "integration_id": "<git-integration-id>",
                "repository": "<owner>/<repository>",
                "branch": "<branch>",
                "path": "<folder>",
                "content_type": "BLUEPRINT",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "accepted (the sync started or already runs; not done), exists, failed, removed "
                "or unchanged.",
                enum=["accepted", "exists", "failed", "removed", "unchanged"],
            ),
            "source": obj(
                "The content source.",
                {
                    "id": field(["string", "null"], "The content source id."),
                    "name": field(["string", "null"], "The name."),
                    "repository": field(["string", "null"], "The repository."),
                    "branch": field(["string", "null"], "The branch."),
                },
                nullable=True,
            ),
            "job": obj(
                "The vendor's sync job.",
                {
                    "job_id": field(["string", "null"], "The vendor's sync request id."),
                    "state": field(["string", "null"], "The state when MEHO answered."),
                    "already_running": field("boolean", "True when MEHO found a running sync."),
                    "status_read": field("object", "The op and params that read the sync state."),
                },
                nullable=True,
            ),
            "steps": STEPS,
            "guidance": field("string", "What to do next."),
        },
        "required": ["status", "steps", "guidance"],
        "additionalProperties": True,
    },
    tags=("write", "vcfa", "integrations", "git"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to feed a project's cloud templates, ABX actions or Terraform files from a "
            "Git repository, or to remove such a source. " + ASYNC_HINT
        ),
        "preconditions": (
            "Add the Git integration first ({ns}.integration.set with a com.github.saas, "
            "com.gitlab.saas or org.bitbucket type). A person must approve the call."
        ),
        "output_shape": (
            "{status, source, job: {job_id, state, already_running, status_read}, steps, "
            "guidance}. accepted never means the sync is done."
        ),
        "next_step": (
            "Read {ns}.integration.list with sync_request_id = job.job_id until done. After a "
            "lost reply, read {ns}.integration.list view git_sources first: the source and "
            "its last sync show what happened; do not start again blindly."
        ),
        "result_scalars": {"keys": ["status", "guidance"]},
    },
)

#: The five ops, in the order of a setup.
INTEGRATION_OPS: Final[tuple[IntegrationOpSpec, ...]] = (_LIST, _IMPORT, _SET, _GIT, _DELETE)
