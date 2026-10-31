# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the Installer settings operations and of the resource calculation.

Six typed operations; their bodies are in :mod:`.system_settings` and
:mod:`.resources_calc`. :mod:`.typed_ops` adds them to
:data:`~.typed_ops.INSTALLER_TYPED_OPS`, so one registrar upserts them all.

| op_id | Risk | Versions |
|---|---|---|
| ``installer.system.proxy.get`` | ``safe`` | 9.0, 9.1 |
| ``installer.system.proxy.set`` | ``dangerous`` + approval | 9.0, 9.1 |
| ``installer.system.settings.get`` | ``safe`` | 9.0, 9.1 |
| ``installer.system.settings.set`` | ``caution`` + approval | 9.0, 9.1 |
| ``installer.system.trusted-certificates.remove`` | ``dangerous`` + approval | 9.0, 9.1 |
| ``installer.sddc.resources.calculate`` | ``safe`` | 9.1+ |
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_installer._op_meta import (
    GROUP_BRINGUP,
    GROUP_DEPOT,
    GROUP_SYSTEM,
    InstallerTypedOp,
    build_instructions,
)
from meho_backplane.connectors.vcf_installer._response_schemas import (
    PROXY_GET_RESPONSE,
    PROXY_SET_RESPONSE,
    RESOURCES_CALCULATE_RESPONSE,
    SETTINGS_GET_RESPONSE,
    SETTINGS_SET_RESPONSE,
    TRUSTED_CERTIFICATE_REMOVE_RESPONSE,
)
from meho_backplane.connectors.vcf_installer.resources_calc import (
    DEFAULT_TIMEOUT_SECONDS,
    INSTALLER_RESOURCES_CALCULATE_OP_ID,
)
from meho_backplane.connectors.vcf_installer.system_settings import (
    INSTALLER_PROXY_GET_OP_ID,
    INSTALLER_PROXY_SET_OP_ID,
    INSTALLER_SETTINGS_GET_OP_ID,
    INSTALLER_SETTINGS_SET_OP_ID,
    INSTALLER_TRUSTED_CERTIFICATE_REMOVE_OP_ID,
)

__all__ = ["INSTALLER_SYSTEM_TYPED_OPS"]

#: The advice every vendor-job start gives about a lost reply and async mode.
_JOB_ADVICE = (
    "Call it with async: true, so MEHO keeps the answer (with the job id) under a run "
    "handle. If the reply is lost, do not start again blindly: read the status read "
    "first (without task_id it shows the settings; a running job there means wait)."
)


def _task_id_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional. The vendor job id a change returned. With it, the answer "
                    "also has the job state."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{"task_id": "<task-id>"}],
    }


_PASSWORD_REF_PROPS: dict[str, Any] = {
    "password_secret_ref": {
        "type": "string",
        "minLength": 1,
        "description": "Vault KV-v2 path that holds the proxy password. Never the password.",
    },
    "password_secret_mount": {
        "type": "string",
        "minLength": 1,
        "description": "KV-v2 mount of password_secret_ref (default 'secret').",
    },
    "password_secret_key": {
        "type": "string",
        "minLength": 1,
        "description": "Field of the secret that holds the password (default 'password').",
    },
}

_TASK_OUTPUT = (
    "task (only with task_id): {id, state, outcome: queued|running|succeeded|failed|"
    "unknown|not_reachable, progress?: {sub_tasks_done, sub_tasks_total}, errors: "
    "[text], checked_at}. not_reachable means the appliance did not answer; it is not "
    "a failure."
)

_PROXY_GET = InstallerTypedOp(
    op_id=INSTALLER_PROXY_GET_OP_ID,
    handler_attr="system_proxy_get",
    summary="Read the Installer's proxy settings, and the state of a proxy change.",
    description=(
        "Reads GET /v1/system/proxy-configuration: on or off, host, port, HTTP or HTTPS, "
        "user. The password is never in the answer. With task_id it also reads the "
        "vendor job of a proxy change. Read-only."
    ),
    parameter_schema=_task_id_schema(),
    response_schema=PROXY_GET_RESPONSE,
    group_key=GROUP_SYSTEM,
    tags=("read-only", "vcf", "installer", "system", "proxy"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=build_instructions(
        when_to_call=(
            "Call to see which proxy the Installer uses to reach the online depot, or "
            "with task_id to follow a change made by installer.system.proxy.set."
        ),
        output_shape=(
            "{proxy: {isConfigured, isEnabled, host, port, transferProtocol, username, "
            "isAuthenticated} | null, task?}. " + _TASK_OUTPUT
        ),
        next_step=(
            "If task.outcome is queued or running, read again later. If it is failed, "
            "read task.errors, fix the cause and set the proxy again. If it is "
            "not_reachable, wait and read again with the same task_id."
        ),
        parameter_hints={"task_id": "The task_id installer.system.proxy.set returned."},
    ),
)

_PROXY_SET = InstallerTypedOp(
    op_id=INSTALLER_PROXY_SET_OP_ID,
    handler_attr="system_proxy_set",
    summary="Set or turn off the proxy the Installer uses to reach the online depot.",
    description=(
        "Sends PATCH /v1/system/proxy-configuration: host, port, HTTP or HTTPS, user, or "
        "off. The password comes from Vault. The appliance does the change as a job; the "
        "answer says accepted with the job id. A person must approve first."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "true to use a proxy; false to turn the proxy off.",
            },
            "host": {
                "type": "string",
                "minLength": 1,
                "description": "Proxy host name or IP address. Needed when enabled is true.",
            },
            "port": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "description": "Proxy port. Needed when enabled is true.",
            },
            "transfer_protocol": {
                "type": "string",
                "enum": ["HTTP", "HTTPS"],
                "description": "How the Installer talks to the proxy (default HTTP).",
            },
            "username": {
                "type": "string",
                "minLength": 1,
                "description": "Proxy user, when the proxy needs a login.",
            },
            **_PASSWORD_REF_PROPS,
        },
        "required": ["enabled"],
        "additionalProperties": False,
        "oneOf": [
            {
                "description": "Turn the proxy off.",
                "properties": {
                    "enabled": {"const": False, "description": "false: turn the proxy off."}
                },
            },
            {
                "description": "Use a proxy: host and port are needed.",
                "properties": {"enabled": {"const": True, "description": "true: use the proxy."}},
                "required": ["host", "port"],
            },
        ],
        "dependentRequired": {
            "username": ["password_secret_ref"],
            "password_secret_ref": ["username"],
        },
        "examples": [
            {
                "enabled": True,
                "host": "<proxy-host>",
                "port": 3128,
                "transfer_protocol": "HTTP",
                "username": "<proxy-user>",
                "password_secret_ref": "<vault-path>",
            }
        ],
    },
    response_schema=PROXY_SET_RESPONSE,
    group_key=GROUP_SYSTEM,
    # "credential": the request body holds the proxy password read from Vault,
    # so a trace keeps no body of this call.
    tags=("vcf", "installer", "system", "proxy", "write", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=build_instructions(
        when_to_call=(
            "Call when the Installer must reach the online depot through a proxy, when "
            "the proxy changes, or to turn the proxy off. " + _JOB_ADVICE
        ),
        output_shape=(
            "{status: accepted|already_running|rejected|unknown, task_id, vendor_status, "
            "status_read: {op_id, params: {task_id}}, proxy?: {...password removed...}}; "
            "on rejected also {http_status, error_code, message}. accepted means the job "
            "started, not that it is done."
        ),
        next_step=(
            "Call installer.system.proxy.get with the task_id until task.outcome is "
            "succeeded or failed. On already_running, follow that job instead. On "
            "unknown, read installer.system.proxy.get without task_id first."
        ),
        preconditions=(
            "Read the current settings with installer.system.proxy.get. If the proxy "
            "needs a login, store its password in Vault and pass the path. A person "
            "must approve the call before it runs. A wrong proxy cuts the Installer "
            "off from the online depot."
        ),
        parameter_hints={
            "enabled": "false turns the proxy off; then no other field is needed.",
            "password_secret_ref": "A Vault path; never the password itself.",
        },
    ),
)

_SETTINGS_GET = InstallerTypedOp(
    op_id=INSTALLER_SETTINGS_GET_OP_ID,
    handler_attr="system_settings_get",
    summary="Read the Installer's CEIP state and system settings, and a CEIP change.",
    description=(
        "Reads GET /v1/system/ceip (whether the Installer sends usage data to the vendor: "
        "on, off or changing) and GET /v1/system (the most domains allowed in "
        "subscription mode). With task_id it also reads the vendor job. Read-only."
    ),
    parameter_schema=_task_id_schema(),
    response_schema=SETTINGS_GET_RESPONSE,
    group_key=GROUP_SYSTEM,
    tags=("read-only", "vcf", "installer", "system", "ceip"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=build_instructions(
        when_to_call=(
            "Call to see whether CEIP is on and how many domains subscription mode "
            "allows, or with task_id to follow a change made by "
            "installer.system.settings.set."
        ),
        output_shape=(
            "{ceip: {status, instanceId} | null, system: {id, "
            "maxAllowedDomainsInSubscription, vcfInstanceName?} | null, task?}. "
            "ceip.status is ENABLED, DISABLED, ENABLING, DISABLING, ENABLING_FAILED or "
            "DISABLING_FAILED. " + _TASK_OUTPUT
        ),
        next_step=(
            "If ceip.status is ENABLING or DISABLING, or task.outcome is queued or "
            "running, read again later. If task.outcome is failed, read task.errors."
        ),
        parameter_hints={"task_id": "The task_id installer.system.settings.set returned."},
    ),
)

_SETTINGS_SET = InstallerTypedOp(
    op_id=INSTALLER_SETTINGS_SET_OP_ID,
    handler_attr="system_settings_set",
    summary="Turn CEIP on or off, or change the most domains allowed in subscription mode.",
    description=(
        "Sends PATCH /v1/system/ceip (a vendor job; the body shape follows the "
        "Installer version) and/or PATCH /v1/system. Returns each step: accepted, done, "
        "failed or not run. A person must approve first."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "ceip_enabled": {
                "type": "boolean",
                "description": "true turns CEIP on (send usage data); false turns it off.",
            },
            "max_domains_in_subscription": {
                "type": "integer",
                "minimum": 1,
                "description": "The most domains allowed in subscription mode. 1 or more.",
            },
        },
        "additionalProperties": False,
        "anyOf": [
            {"required": ["ceip_enabled"]},
            {"required": ["max_domains_in_subscription"]},
        ],
        "examples": [{"ceip_enabled": False}],
    },
    response_schema=SETTINGS_SET_RESPONSE,
    group_key=GROUP_SYSTEM,
    tags=("vcf", "installer", "system", "ceip", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=build_instructions(
        when_to_call=(
            "Call to turn CEIP on or off on the Installer, or to change the most "
            "domains allowed in subscription mode. " + _JOB_ADVICE
        ),
        output_shape=(
            "{status: accepted|done|already_running|partial|failed, steps: [{step: "
            "ceip|system, result: accepted|done|failed|unknown|not_run, task_id?, "
            "error?: {http_status, error_code, message}}], task_id?, status_read}. "
            "MEHO runs CEIP first, then system; a failed step stops the rest and "
            "nothing is undone."
        ),
        next_step=(
            "When a CEIP job was accepted, call installer.system.settings.get with the "
            "task_id until task.outcome is succeeded or failed. On partial or failed, "
            "read the failed step's error; run only the steps that did not run again."
        ),
        preconditions=(
            "Read the current values with installer.system.settings.get. A CEIP change "
            "that runs (ENABLING or DISABLING) makes this call start nothing. A person "
            "must approve the call before it runs."
        ),
        parameter_hints={
            "ceip_enabled": "Omit to leave CEIP as it is.",
            "max_domains_in_subscription": "Omit to leave it as it is.",
        },
    ),
)

_TRUSTED_CERTIFICATE_REMOVE = InstallerTypedOp(
    op_id=INSTALLER_TRUSTED_CERTIFICATE_REMOVE_OP_ID,
    handler_attr="system_trusted_certificate_remove",
    summary="Remove a certificate from the Installer's trust store by its alias.",
    description=(
        "Sends DELETE /v1/sddc-manager/trusted-certificates/{alias}. A depot or other "
        "system that needs the certificate can then no longer be reached over HTTPS. "
        "The answer warns when it belongs to the depot that is set. A person must "
        "approve first."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "alias": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The alias of the certificate, from installer.system.trusted-certificates.list."
                ),
            },
        },
        "required": ["alias"],
        "additionalProperties": False,
        "examples": [{"alias": "<certificate-alias>"}],
    },
    response_schema=TRUSTED_CERTIFICATE_REMOVE_RESPONSE,
    group_key=GROUP_DEPOT,
    tags=("vcf", "installer", "certificates", "system", "write", "delete"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=build_instructions(
        when_to_call=(
            "Call to remove a certificate the Installer should no longer trust, for "
            "example an old or wrong depot certificate."
        ),
        output_shape="{status: 'removed', alias, warning?}.",
        next_step=(
            "If warning is set, add the depot's new certificate with "
            "installer.system.trusted-certificates.add, or the depot cannot be reached."
        ),
        preconditions=(
            "Find the alias with installer.system.trusted-certificates.list. An "
            "unknown alias answers not found. A person must approve the call before "
            "it runs. It cannot be undone except by adding the certificate again."
        ),
        parameter_hints={"alias": "The exact alias from the trust store list."},
    ),
)

_RESOURCES_CALCULATE = InstallerTypedOp(
    op_id=INSTALLER_RESOURCES_CALCULATE_OP_ID,
    handler_attr="sddc_resources_calculate",
    summary="Calculate the CPU, memory and storage a deploy spec needs (Installer 9.1+).",
    description=(
        "Sends the SddcSpec to POST /v1/sddcs/resources-calculation, reads the result "
        "until it ends and compares what the spec needs with what the hosts have, check "
        "by check. Changes nothing. Installer 9.1 and newer only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": (
                    "The SddcSpec of the planned deploy, without password values. Name "
                    "each password in password_refs instead."
                ),
                "additionalProperties": True,
            },
            "password_refs": {
                "type": "array",
                "description": "The passwords of the spec, each as a Vault path.",
                "items": {
                    "type": "object",
                    "properties": {
                        "pointer": {
                            "type": "string",
                            "pattern": "^/",
                            "description": (
                                "JSON pointer of the password field in the spec, for "
                                "example /vcenterSpec/rootVcenterPassword."
                            ),
                        },
                        "secret_ref": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Vault KV-v2 path that holds the password.",
                        },
                        "secret_mount": {
                            "type": "string",
                            "minLength": 1,
                            "description": "KV-v2 mount of secret_ref (default 'secret').",
                        },
                        "secret_key": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Field of the secret (default 'password').",
                        },
                    },
                    "required": ["pointer", "secret_ref"],
                    "additionalProperties": False,
                },
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 3600,
                "description": (
                    f"How long MEHO waits for the result (default {DEFAULT_TIMEOUT_SECONDS})."
                ),
            },
        },
        "required": ["spec"],
        "additionalProperties": False,
        "examples": [
            {
                "spec": {
                    "sddcId": "<sddc-id>",
                    "vcenterSpec": {"vcenterHostname": "<vcenter-fqdn>"},
                    "networkSpecs": [{"networkType": "MANAGEMENT", "subnet": "<subnet-cidr>"}],
                    "dnsSpec": {"subdomain": "<dns-subdomain>"},
                    "hostSpecs": [{"hostname": "<esx-host-fqdn>"}],
                },
                "password_refs": [
                    {"pointer": "/vcenterSpec/rootVcenterPassword", "secret_ref": "<vault-path>"}
                ],
            }
        ],
    },
    response_schema=RESOURCES_CALCULATE_RESPONSE,
    group_key=GROUP_BRINGUP,
    # "credential": the spec sent holds the passwords read from Vault.
    tags=("read-only", "vcf", "installer", "bringup", "capacity", "credential"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=build_instructions(
        when_to_call=(
            "Call before a bring-up to learn whether the hosts have enough CPU, memory "
            "and storage for the spec. Use async: true: MEHO answers at once with a run "
            "handle and the stored result has the final answer. MEHO does not resume an "
            "interrupted run; if MEHO restarts, the run ends as failed and you start it "
            "again (it changes nothing, so that is safe)."
        ),
        output_shape=(
            "{status: done|timed_out|read_failed|rejected, id, execution_status, "
            "result_status, required: {numberOfCores, memory, storage, "
            "numberOfEsxiHosts, componentsVmRequirements}, available: {...}, checks: "
            "[{description, severity, result_status, error, parent}]}; on rejected "
            "{http_status, error_code, message}."
        ),
        next_step=(
            "If result_status is FAILED, read the failed checks and fix the hosts or "
            "the spec. If it is SUCCEEDED or WARNING, check the spec with "
            "installer.sddc.spec.validate. On a 9.0 Installer this call is not "
            "available."
        ),
        parameter_hints={
            "spec": "The SddcSpec with no password values.",
            "password_refs": "One row per password field of the spec.",
        },
    ),
)

#: The six operations, in table order.
INSTALLER_SYSTEM_TYPED_OPS: tuple[InstallerTypedOp, ...] = (
    _PROXY_GET,
    _PROXY_SET,
    _SETTINGS_GET,
    _SETTINGS_SET,
    _TRUSTED_CERTIFICATE_REMOVE,
    _RESOURCES_CALCULATE,
)
