# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Kubernetes Job backend for ephemeral ingestion.

Creates K8s Jobs via the kubernetes-asyncio BatchV1Api to run Docling
conversion in ephemeral pods. Each job processes a single document and
exits, reclaiming all memory (including PyTorch C++ allocator pools).
"""

from __future__ import annotations

from meho_app.worker.backends.protocol import (
    JobState,
    JobStatus,
    ResourceProfile,
)


class KubernetesBackend:
    """IngestionBackend that dispatches work to Kubernetes Jobs.

    Creates a K8s Job for each document ingestion request. The job runs
    the MEHO worker image with environment variables pointing to the
    source document (input_url) and result destination (output_url).

    Attributes:
        namespace: K8s namespace for ingestion jobs.
        image: Docker image for the worker container.
        server_url: K8s API server URL (empty = in-cluster).
        token: Bearer token for K8s API auth (None = in-cluster).
        ca_cert: Path to CA certificate (None = skip TLS / in-cluster).
        service_account: Pod service account name (None = default).
    """

    def __init__(
        self,
        namespace: str = "default",
        image: str = "",
        server_url: str = "",
        token: str | None = None,
        ca_cert: str | None = None,
        service_account: str | None = None,
    ) -> None:
        self.namespace = namespace
        self.image = image
        self.server_url = server_url
        self.token = token
        self.ca_cert = ca_cert
        self.service_account = service_account

    async def _get_batch_api(self) -> kubernetes_asyncio.client.BatchV1Api:  # type: ignore[name-defined]  # noqa: F821
        """Create a BatchV1Api client from configuration.

        Returns:
            A configured BatchV1Api client instance.
        """
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client import ApiClient, Configuration

        if not self.server_url:
            # In-cluster config
            from kubernetes_asyncio.config import load_incluster_config

            load_incluster_config()
            api_client = ApiClient()
        else:
            config = Configuration()
            config.host = self.server_url

            if self.token:
                config.api_key = {"authorization": self.token}
                config.api_key_prefix = {"authorization": "Bearer"}

            if self.ca_cert:
                config.ssl_ca_cert = self.ca_cert
            else:
                config.verify_ssl = False

            api_client = ApiClient(configuration=config)

        return k8s_client.BatchV1Api(api_client)

    async def dispatch(
        self,
        job_id: str,
        input_url: str,
        output_url: str,
        profile: ResourceProfile,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """Dispatch an ingestion job to Kubernetes.

        Creates a K8s Job with resource limits from the profile, sets
        environment variables for the worker, and returns the job name
        as the execution_id.

        Args:
            job_id: Unique job identifier.
            input_url: Presigned URL or path to the source document.
            output_url: Presigned URL or path for the Arrow IPC output.
            profile: Resource requirements for the worker container.
            env_overrides: Additional environment variables for the worker.

        Returns:
            K8s Job name as execution_id.
        """
        from kubernetes_asyncio import client as k8s_client

        job_name = f"meho-ingest-{job_id[:8]}"

        # Build environment variables
        env_vars = [
            k8s_client.V1EnvVar(name="WORKER_JOB_ID", value=job_id),
            k8s_client.V1EnvVar(name="WORKER_INPUT_URL", value=input_url),
            k8s_client.V1EnvVar(name="WORKER_OUTPUT_URL", value=output_url),
            k8s_client.V1EnvVar(name="OMP_NUM_THREADS", value="4"),
            k8s_client.V1EnvVar(name="MALLOC_TRIM_THRESHOLD_", value="131072"),
        ]

        # Add env overrides
        if env_overrides:
            for key, value in env_overrides.items():
                env_vars.append(k8s_client.V1EnvVar(name=key, value=value))

        # Build resource requirements
        resources = k8s_client.V1ResourceRequirements(
            requests={
                "memory": f"{profile.memory_gb}Gi",
                "cpu": str(profile.cpu),
            },
            limits={
                "memory": f"{profile.memory_gb}Gi",
                "cpu": str(profile.cpu),
            },
        )

        # Build container
        container = k8s_client.V1Container(
            name="ingestion-worker",
            image=self.image,
            command=["python", "-m", "meho_app.worker"],
            env=env_vars,
            resources=resources,
        )

        # Build pod spec
        pod_spec = k8s_client.V1PodSpec(
            containers=[container],
            restart_policy="Never",
        )
        if self.service_account:
            pod_spec.service_account_name = self.service_account

        # Build job spec
        job_spec = k8s_client.V1JobSpec(
            template=k8s_client.V1PodTemplateSpec(
                spec=pod_spec,
            ),
            backoff_limit=0,
            active_deadline_seconds=profile.timeout_seconds,
            ttl_seconds_after_finished=3600,
        )

        # Build job
        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                labels={
                    "app": "meho-ingestion",
                    "job-id": job_id,
                },
            ),
            spec=job_spec,
        )

        batch_api = await self._get_batch_api()
        result = await batch_api.create_namespaced_job(
            namespace=self.namespace,
            body=job,
        )

        return str(result.metadata.name)

    async def get_status(self, execution_id: str) -> JobStatus:
        """Get status of a Kubernetes ingestion job.

        Maps K8s job status fields to JobState:
        - succeeded > 0 -> SUCCEEDED
        - failed > 0 -> FAILED
        - active > 0 -> RUNNING
        - else -> PENDING

        Args:
            execution_id: K8s Job name from dispatch().

        Returns:
            Current job status.
        """
        batch_api = await self._get_batch_api()
        job = await batch_api.read_namespaced_job_status(
            name=execution_id,
            namespace=self.namespace,
        )

        status = job.status
        started_at = getattr(status, "start_time", None)
        completion_time = getattr(status, "completion_time", None)

        # Determine state
        if status.succeeded and status.succeeded > 0:
            state = JobState.SUCCEEDED
        elif status.failed and status.failed > 0:
            state = JobState.FAILED
        elif status.active and status.active > 0:
            state = JobState.RUNNING
        else:
            state = JobState.PENDING

        # Extract error message from conditions if failed
        error_message: str | None = None
        if state == JobState.FAILED:
            conditions = getattr(status, "conditions", None) or []
            for condition in conditions:
                if getattr(condition, "type", "") == "Failed":
                    error_message = getattr(condition, "message", None)
                    break

        return JobStatus(
            state=state,
            execution_id=execution_id,
            started_at=started_at,
            finished_at=completion_time,
            error_message=error_message,
        )

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running Kubernetes ingestion job.

        Deletes the Job with Background propagation policy, which
        cascades deletion to all owned pods.

        Args:
            execution_id: K8s Job name from dispatch().
        """
        from kubernetes_asyncio import client as k8s_client

        batch_api = await self._get_batch_api()
        await batch_api.delete_namespaced_job(
            name=execution_id,
            namespace=self.namespace,
            body=k8s_client.V1DeleteOptions(
                propagation_policy="Background",
            ),
        )
