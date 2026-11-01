# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.cronjob.run`` -- start a CronJob's job now, like
``kubectl create job <name> --from=cronjob/<cronjob>``.

The op reads the CronJob (``BatchV1Api.read_namespaced_cron_job``), builds
one Job from its ``spec.jobTemplate`` and creates it
(``BatchV1Api.create_namespaced_job``). The Job is built the way kubectl's
``createJobFromCronJob`` builds it:

* ``metadata.annotations``: ``cronjob.kubernetes.io/instantiate: manual``
  plus the annotations of the job template;
* ``metadata.labels``: the labels of the job template;
* ``metadata.ownerReferences``: one entry for the CronJob
  (``batch/v1`` ``CronJob``, its name and uid, ``controller: true``).
  ``blockOwnerDeletion`` stays unset: setting it needs an extra right on
  ``cronjobs/finalizers``;
* ``spec``: the job template's spec, unchanged.

Without ``job_name`` the Job gets ``metadata.generateName``
``<cronjob>-manual-``; the API server appends five random characters and
shortens a long prefix. The result carries the name the API server chose.

A suspended CronJob (``spec.suspend: true``) still runs, as with kubectl.
A missing CronJob raises
:class:`~meho_backplane.connectors.kubernetes.ops_workload.WorkloadNotFoundError`
before anything is created.

The params and the result hold only names, so the op is not a credential
op and is not declared ``credential_write`` in
``kubernetes/op_classes.py``. The
approval preview (``ops_write_preview.py``) reads the CronJob and shows its
schedule, flags, running Jobs and containers; env var names, never values.

References
----------
* kubectl ``createJobFromCronJob``:
  https://github.com/kubernetes/kubectl/blob/master/pkg/cmd/create/create_job.go
* ``kubernetes_asyncio`` 36.1.0 ``BatchV1Api`` / ``V1OwnerReference``:
  https://github.com/tomplus/kubernetes_asyncio/tree/master/kubernetes_asyncio/docs
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_workload import WorkloadNotFoundError

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1CronJob, V1Job

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "CRONJOB_OPS",
    "INSTANTIATE_ANNOTATION",
    "build_job_from_cronjob",
    "k8s_cronjob_run",
    "model_to_json",
    "read_cronjob",
]

#: kubectl marks a manually started Job with this annotation.
INSTANTIATE_ANNOTATION: str = "cronjob.kubernetes.io/instantiate"


def model_to_json(obj: Any) -> Any:
    """Turn a ``kubernetes_asyncio`` model into plain JSON data with API field names.

    The same walk as ``ApiClient.sanitize_for_serialization`` without an
    ``ApiClient`` (which opens an HTTP session). ``None`` fields are dropped
    and attribute names become their camelCase API names (``envFrom``).
    """
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, list | tuple):
        return [model_to_json(item) for item in obj]
    if isinstance(obj, dict):
        return {str(key): model_to_json(value) for key, value in obj.items()}
    attribute_map = getattr(obj, "attribute_map", None)
    if not isinstance(attribute_map, dict):
        return obj
    return {
        json_name: model_to_json(getattr(obj, attr))
        for attr, json_name in attribute_map.items()
        if getattr(obj, attr, None) is not None
    }


async def read_cronjob(batch_v1: client.BatchV1Api, namespace: str, name: str) -> V1CronJob:
    """Read one CronJob by exact name. A 404 becomes :class:`WorkloadNotFoundError`."""
    try:
        return await batch_v1.read_namespaced_cron_job(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise WorkloadNotFoundError(
                f"CronJob {name!r} not found in namespace {namespace!r}"
            ) from exc
        raise


def build_job_from_cronjob(cronjob: V1CronJob, *, job_name: str | None = None) -> V1Job:
    """Build the Job that kubectl's ``createJobFromCronJob`` builds. No API call."""
    cronjob_name: str = cronjob.metadata.name
    template = cronjob.spec.job_template
    template_meta = template.metadata
    annotations: dict[str, str] = {INSTANTIATE_ANNOTATION: "manual"}
    labels: dict[str, str] | None = None
    if template_meta is not None:
        annotations.update(template_meta.annotations or {})
        labels = dict(template_meta.labels) if template_meta.labels else None
    metadata = client.V1ObjectMeta(
        name=job_name or None,
        generate_name=None if job_name else f"{cronjob_name}-manual-",
        namespace=cronjob.metadata.namespace,
        annotations=annotations,
        labels=labels,
        owner_references=[
            client.V1OwnerReference(
                api_version="batch/v1",
                kind="CronJob",
                name=cronjob_name,
                uid=cronjob.metadata.uid,
                controller=True,
            )
        ],
    )
    return client.V1Job(api_version="batch/v1", kind="Job", metadata=metadata, spec=template.spec)


async def k8s_cronjob_run(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.cronjob.run``: one Job from the CronJob's template, now."""
    cronjob_name: str = params["cronjob"]
    namespace: str = params["namespace"]
    job_name: str | None = params.get("job_name") or None

    api_client = await connector._get_api_client(target, operator)
    batch_v1 = client.BatchV1Api(api_client)
    cronjob = await read_cronjob(batch_v1, namespace, cronjob_name)
    body = build_job_from_cronjob(cronjob, job_name=job_name)
    created = await batch_v1.create_namespaced_job(namespace=namespace, body=body)
    return {
        "job_name": created.metadata.name,
        "namespace": namespace,
        "cronjob": cronjob_name,
        "created": True,
    }


# ---------------------------------------------------------------------------
# Parameter schema, LLM instructions, response schema, registration
# ---------------------------------------------------------------------------


K8S_CRONJOB_RUN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cronjob": {
            "type": "string",
            "minLength": 1,
            "description": "Exact name of the CronJob whose job template is used.",
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the CronJob lives in. The Job is created there too.",
        },
        "job_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 63,
            "description": (
                "Name for the new Job. Without it the API server picks "
                "'<cronjob>-manual-' plus five random characters."
            ),
        },
    },
    "required": ["cronjob", "namespace"],
    "additionalProperties": False,
    "examples": [
        {"cronjob": "db-backup", "namespace": "data"},
        {"cronjob": "db-backup", "namespace": "data", "job_name": "db-backup-check"},
    ],
}


K8S_CRONJOB_RUN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_name": {"type": "string", "description": "Name of the new Job."},
        "namespace": {"type": "string", "description": "Namespace of the new Job."},
        "cronjob": {"type": "string", "description": "The CronJob the Job was made from."},
        "created": {"type": "boolean", "description": "Always true: the Job exists now."},
    },
    "required": ["job_name", "namespace", "cronjob", "created"],
    "additionalProperties": False,
}


K8S_CRONJOB_RUN_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to start a CronJob's job now instead of waiting for its "
        "schedule, for example to check a CronJob after a change. The same "
        "as 'kubectl create job <name> --from=cronjob/<cronjob>'. Waits for "
        "approval."
    ),
    "preconditions": (
        "The CronJob must exist in the namespace; find its name with "
        "k8s.cr.list (group=batch, version=v1, plural=cronjobs). A suspended "
        "CronJob still runs. The job template is used as it is, including "
        "its env and command."
    ),
    "parameter_hints": {
        "cronjob": "Exact CronJob name, no prefix matching.",
        "namespace": "Required.",
        "job_name": "Optional. Omit to let the API server choose '<cronjob>-manual-xxxxx'.",
    },
    "output_shape": (
        "{job_name, namespace, cronjob, created: true}. job_name is the name "
        "the API server chose when none was given."
    ),
    "next_step": (
        "Follow the Job with k8s.job.info and this job_name until its state "
        "is complete or failed, then read its output with k8s.logs on the "
        "Job's pod."
    ),
}


CRONJOB_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.cronjob.run",
        handler_attr="k8s_cronjob_run",
        summary="Start a CronJob's job now: one Job from its job template, owned by the CronJob.",
        description=(
            "Reads the CronJob with BatchV1Api.read_namespaced_cron_job and creates "
            "one Job from spec.jobTemplate with BatchV1Api.create_namespaced_job, "
            "the way 'kubectl create job --from=cronjob/<name>' does. Returns "
            "{job_name, namespace, cronjob, created}. Requires approval."
        ),
        parameter_schema=K8S_CRONJOB_RUN_PARAMETER_SCHEMA,
        response_schema=K8S_CRONJOB_RUN_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("write", "job", "cronjob"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=K8S_CRONJOB_RUN_LLM_INSTRUCTIONS,
    ),
)
