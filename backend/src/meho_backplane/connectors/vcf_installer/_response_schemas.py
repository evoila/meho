# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas of the VCF Installer operations.

Each schema names the main fields an operation returns, so the AI knows the
shape before it calls. The schemas stay open (``additionalProperties: true``):
they describe the answer, they do not check it.

Vendor field names come from the pinned ``vcf-installer-9.1`` OpenAPI file
(``components.schemas``: ``Validation``, ``SddcTask``, ``PageOfSddcTask``,
``DepotSettings``, ``DepotSyncInfo``, ``PageOfTrustedCertificate``,
``PageOfBundle``, ``PageOfBundleDownloadStatusInfo``). The envelope fields of
the writes and of the multi-step bring-up (``status``, ``settings``,
``results``, ``sddc_task`` and so on) are the keys MEHO's own handlers build.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BRINGUP_COMPOSITE_RESPONSE",
    "BUNDLES_DOWNLOAD_RESPONSE",
    "BUNDLES_DOWNLOAD_STATUS_RESPONSE",
    "BUNDLES_LIST_RESPONSE",
    "DEPOT_GET_RESPONSE",
    "DEPOT_SET_RESPONSE",
    "DEPOT_SYNC_STATUS_RESPONSE",
    "SDDC_LIST_RESPONSE",
    "SDDC_TASK_RESPONSE",
    "TRUSTED_CERTIFICATES_LIST_RESPONSE",
    "TRUSTED_CERTIFICATE_ADD_RESPONSE",
    "VALIDATION_RESPONSE",
]


def _field(type_: str | list[str], description: str) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


_PAGE_METADATA = _object(
    "Paging data of the vendor page.",
    {
        "pageNumber": _field("integer", "The current page number."),
        "pageSize": _field("integer", "The number of elements in this page."),
        "totalElements": _field("integer", "The total number of elements."),
        "totalPages": _field("integer", "The total number of pages."),
    },
)

_ERROR_FIELDS: dict[str, Any] = {
    "errorCode": _field("string", "The vendor error code."),
    "errorType": _field("string", "The vendor error type."),
    "message": _field("string", "The error message."),
    "remediationMessage": _field("string", "What to do to fix the error."),
    "referenceToken": _field("string", "A token that points to the detailed vendor logs."),
    "nestedErrors": {"type": "array", "description": "Errors inside this error."},
}

_MESSAGE_PACK = _object(
    "A message the vendor can show in the user's language.",
    {
        "component": _field("string", "The component the message belongs to."),
        "messageKey": _field("string", "The machine-readable key of the message."),
        "message": _field("string", "The message text."),
        "arguments": {"type": "array", "description": "The values put into the message."},
    },
)

_CHECK_SUMMARY_FIELDS: dict[str, Any] = {
    "description": _field("string", "What the check tests."),
    "severity": _field("string", "The severity of the check."),
    "resultStatus": _field("string", "The result of the check, for example FAILED or WARNING."),
}

# --- Bring-up: validation and task ------------------------------------------

VALIDATION_RESPONSE: dict[str, Any] = _object(
    "One spec validation run (vendor Validation).",
    {
        "id": _field("string", "The validation id. Use it with installer.sddc.validation.status."),
        "description": _field("string", "What the validation run checks."),
        "executionStatus": _field(
            "string",
            "Where the run is: IN_PROGRESS, COMPLETED, FAILED, UNKNOWN, SKIPPED, "
            "CANCELLED or CANCELLATION_IN_PROGRESS.",
        ),
        "resultStatus": _field(
            "string",
            "The verdict once the run is done: SUCCEEDED, WARNING, FAILED, UNKNOWN "
            "or CANCELLATION_IN_PROGRESS.",
        ),
        "validationChecks": _list_of(
            "The checks of this run.",
            {
                **_CHECK_SUMMARY_FIELDS,
                "acknowledge": _field("boolean", "True if the check must be acknowledged."),
                "errorResponse": _object("Why the check failed.", _ERROR_FIELDS),
                "nestedValidationChecks": {
                    "type": "array",
                    "description": "Checks inside this check, with the same fields.",
                },
            },
        ),
    },
)

_SDDC_TASK_SUMMARY_FIELDS: dict[str, Any] = {
    "id": _field("string", "The bring-up task id. Use it with installer.sddc.bringup.status."),
    "name": _field("string", "The deployment name."),
    "status": _field(
        "string",
        "The task status: IN_PROGRESS, COMPLETED_WITH_SUCCESS, ROLLBACK_SUCCESS "
        "or COMPLETED_WITH_FAILURE.",
    ),
    "deploymentType": _field("string", "The deployment type."),
    "vcfInstanceName": _field("string", "The VCF instance name."),
    "creationTimestamp": _field("string", "When the task was created."),
}

SDDC_TASK_RESPONSE: dict[str, Any] = _object(
    "One bring-up task (vendor SddcTask).",
    {
        **_SDDC_TASK_SUMMARY_FIELDS,
        "localizableNamePack": _MESSAGE_PACK,
        "sddcSubTasks": _list_of(
            "Every step of the bring-up. On a live bring-up this list is long and "
            "comes back as a result handle.",
            {
                "sddcId": _field("string", "The SDDC id."),
                "name": _field("string", "The step name."),
                "description": _field("string", "What the step does."),
                "processingStateName": _field(
                    "string", "The stage the step belongs to, for example VC Deployment."
                ),
                "processingStateDescription": _field("string", "The stage description."),
                "status": _field("string", "The step status, for example IN_PROGRESS or FAILED."),
                "creationTimestamp": _field("string", "When the step was created."),
                "updateTimestamp": _field("string", "When the step was last updated."),
                "errors": _list_of("Why the step failed.", _ERROR_FIELDS),
                "milestoneTask": _field("string", "The milestone this step belongs to."),
            },
        ),
        "milestones": _list_of(
            "The main stages of the bring-up.",
            {
                "name": _field("string", "The milestone name."),
                "description": _field("string", "The milestone description."),
                "status": _field("string", "The milestone status."),
                "creationTimestamp": _field("string", "When the milestone was created."),
                "updateTimestamp": _field("string", "When the milestone was last updated."),
                "completedTasks": _field(
                    ["integer", "null"], "Steps done in this milestone; null before it starts."
                ),
                "totalTasks": _field(
                    ["integer", "null"], "All steps of this milestone; null before it starts."
                ),
            },
        ),
    },
)

SDDC_LIST_RESPONSE: dict[str, Any] = _object(
    "Every bring-up task, cut down to its summary fields.",
    {
        "elements": _list_of("One row per bring-up task.", _SDDC_TASK_SUMMARY_FIELDS),
        "pageMetadata": _PAGE_METADATA,
    },
)

BRINGUP_COMPOSITE_RESPONSE: dict[str, Any] = _object(
    "The outcome of the multi-step bring-up.",
    {
        "status": _field(
            "string",
            "deploying: the bring-up started. validation_failed or validation_timeout: "
            "the spec check stopped it and nothing was deployed.",
        ),
        "validation_id": _field("string", "The id of the spec validation run."),
        "execution_status": _field(
            "string", "On a stop: the executionStatus of the validation run."
        ),
        "result_status": _field("string", "On a stop: the resultStatus of the validation run."),
        "failed_checks": _list_of("On a stop: the checks that failed.", _CHECK_SUMMARY_FIELDS),
        "sddc_task": _object(
            "On deploying: the bring-up task.",
            {
                "id": _field("string", "The bring-up task id."),
                "status": _field("string", "The task status."),
                "name": _field("string", "The deployment name."),
                "vcfInstanceName": _field("string", "The VCF instance name."),
            },
        ),
        "poll_with": _field("string", "The operation to follow the task with."),
        "validation_result_status": _field(
            "string", "On deploying: the verdict of the validation, SUCCEEDED or WARNING."
        ),
        "validation_warnings": _list_of(
            "On deploying: the checks that ended with a warning.", _CHECK_SUMMARY_FIELDS
        ),
    },
)

# --- Depot, trust store and bundles -------------------------------------------

_DEPOT_ACCOUNT_FIELDS: dict[str, Any] = {
    "username": _field("string", "The depot user name."),
    "status": _field(
        "string",
        "The connection status, for example DEPOT_CONNECTION_SUCCESSFUL or "
        "DEPOT_INVALID_CREDENTIAL.",
    ),
    "message": _field("string", "What the status means."),
}

_DEPOT_SETTINGS_FIELDS: dict[str, Any] = {
    "vmwareAccount": _object("The online Broadcom depot account.", _DEPOT_ACCOUNT_FIELDS),
    "offlineAccount": _object("The offline depot account.", _DEPOT_ACCOUNT_FIELDS),
    "depotConfiguration": _object(
        "Where the depot is.",
        {
            "isOfflineDepot": _field("boolean", "True for an offline depot."),
            "hostname": _field("string", "The depot host name or IP address."),
            "port": _field("integer", "The depot port."),
            "url": _field("string", "The depot URL (offline depots only)."),
        },
    ),
}

DEPOT_GET_RESPONSE: dict[str, Any] = _object(
    "The depot settings (vendor DepotSettings). Password fields are removed.",
    _DEPOT_SETTINGS_FIELDS,
)

DEPOT_SET_RESPONSE: dict[str, Any] = _object(
    "The result of the depot change.",
    {
        "status": _field(
            "string", "ok: the depot is set. depot_error: the appliance refused the settings."
        ),
        "settings": _object(
            "On ok: the saved settings, password fields removed.", _DEPOT_SETTINGS_FIELDS
        ),
        "http_status": _field("integer", "On depot_error: the HTTP status of the refusal."),
        "error_code": _field(
            "string", "On depot_error: the vendor error code, e.g. VMWARE_DEPOT_CONNECT_FAILURE."
        ),
        "message": _field("string", "On depot_error: the vendor error message."),
        "remediation": _field("string", "On a TLS trust failure: what to do next."),
    },
)

DEPOT_SYNC_STATUS_RESPONSE: dict[str, Any] = _object(
    "The depot catalog sync state (vendor DepotSyncInfo).",
    {
        "syncStatus": _field("string", "The sync state, as the vendor sends it."),
        "errorMessage": _field("string", "Why the last sync failed; empty when it did not fail."),
        "lastSyncCompletionTimestamp": _field("string", "When the last sync finished."),
    },
)

TRUSTED_CERTIFICATES_LIST_RESPONSE: dict[str, Any] = _object(
    "The appliance trust store (vendor PageOfTrustedCertificate).",
    {
        "elements": _list_of(
            "One row per trusted certificate.",
            {
                "alias": _field("string", "The certificate alias."),
                "certificate": _field("string", "The certificate in PEM format."),
            },
        ),
        "pageMetadata": _PAGE_METADATA,
    },
)

TRUSTED_CERTIFICATE_ADD_RESPONSE: dict[str, Any] = _object(
    "The result of the certificate import.",
    {
        "status": _field("string", "ok when the appliance took the certificate."),
        "result": TRUSTED_CERTIFICATES_LIST_RESPONSE,
    },
)

BUNDLES_LIST_RESPONSE: dict[str, Any] = _object(
    "The bundle inventory (vendor PageOfBundle).",
    {
        "elements": _list_of(
            "One row per bundle. A release depot has about 180.",
            {
                "id": _field("string", "The bundle id."),
                "type": _field("string", "The bundle type."),
                "description": _field("string", "The bundle description."),
                "version": _field("string", "The bundle version."),
                "severity": _field("string", "The bundle severity."),
                "vendor": _field("string", "The bundle vendor."),
                "releasedDate": _field("string", "When the bundle was released."),
                "isCumulative": _field("boolean", "True if the bundle is cumulative."),
                "isCompliant": _field("boolean", "True if it fits the current VCF version."),
                "sizeMB": _field("number", "The bundle size in MB."),
                "downloadStatus": _field(
                    "string", "PENDING, SCHEDULED, IN_PROGRESS, SUCCESSFUL or FAILED."
                ),
                "components": {"type": "array", "description": "The components in the bundle."},
                "applicabilityStatus": _field("string", "Whether the bundle applies."),
            },
        ),
        "pageMetadata": _PAGE_METADATA,
    },
)

BUNDLES_DOWNLOAD_STATUS_RESPONSE: dict[str, Any] = _object(
    "Download detail per bundle (vendor PageOfBundleDownloadStatusInfo).",
    {
        "elements": _list_of(
            "One row per bundle.",
            {
                "bundleId": _field("string", "The bundle id."),
                "componentType": _field("string", "The component the bundle holds."),
                "version": _field("string", "The component version."),
                "downloadStatus": _field("string", "The download status."),
                "message": _field("string", "On a failed download: the vendor error text."),
                "isDownloadable": _field("boolean", "True if the bundle can be downloaded now."),
                "downloadedSize": _field("integer", "Bytes downloaded so far."),
                "downloadId": _field("string", "The download task id."),
                "downloadScheduledTime": _field("integer", "When the download was scheduled."),
                "downloadStartTime": _field("integer", "When the download started."),
                "downloadEndTime": _field("integer", "When the download ended or stopped."),
                "isDownloadCancellable": _field(
                    "boolean", "True if the download can be cancelled."
                ),
            },
        ),
        "pageMetadata": _PAGE_METADATA,
    },
)

BUNDLES_DOWNLOAD_RESPONSE: dict[str, Any] = _object(
    "The result of the download batch.",
    {
        "status": _field("string", "ok: every bundle was accepted. partial: some were refused."),
        "accepted": _field("integer", "How many bundles the appliance accepted."),
        "failed": _field("integer", "How many bundles the appliance refused."),
        "results": _list_of(
            "One row per bundle id.",
            {
                "id": _field("string", "The bundle id."),
                "status": _field("string", "accepted or failed."),
                "task_id": _field("string", "On accepted: the vendor download task id."),
                "task_name": _field("string", "On accepted: the vendor task name."),
                "http_status": _field("integer", "On failed: the HTTP status of the refusal."),
                "error_code": _field("string", "On failed: the vendor error code."),
                "message": _field("string", "On failed: the vendor error message."),
            },
        ),
    },
)
