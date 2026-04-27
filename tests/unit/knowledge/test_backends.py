# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for IngestionBackend implementations.

Tests all four backends (Local, Kubernetes, CloudRun, Docker) with
mocked external APIs. No real infrastructure calls are made.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.worker.backends.protocol import (
    IngestionBackend,
    JobState,
    JobStatus,
    ResourceProfile,
)

# Shared test fixtures
_TEST_PROFILE = ResourceProfile(
    memory_gb=4,
    cpu=2,
    gpu=False,
    timeout_seconds=600,
    size_category="small",
)


# ---------------------------------------------------------------------------
# LocalBackend tests
# ---------------------------------------------------------------------------


class TestLocalBackendProtocol:
    """Verify LocalBackend satisfies the IngestionBackend protocol."""

    def test_isinstance_check(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()
        assert isinstance(backend, IngestionBackend)


class TestLocalBackendDispatch:
    """Verify LocalBackend.dispatch() returns job_id and starts processing."""

    @pytest.mark.asyncio
    async def test_dispatch_returns_job_id(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()
        with patch.object(backend, "_process", new_callable=AsyncMock) as mock_process:
            execution_id = await backend.dispatch(
                job_id="test-job-123",
                input_url="file:///tmp/input.pdf",
                output_url="file:///tmp/output.arrow",
                profile=_TEST_PROFILE,
            )
        assert execution_id == "test-job-123"
        mock_process.assert_called_once()


class TestLocalBackendGetStatus:
    """Verify LocalBackend.get_status() returns correct states."""

    @pytest.mark.asyncio
    async def test_running_while_task_active(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()
        # Create a long-running task manually
        event = asyncio.Event()

        async def _long_task() -> None:
            await event.wait()

        task = asyncio.create_task(_long_task())
        backend._tasks["test-job"] = task
        backend._results["test-job"] = None

        status = await backend.get_status("test-job")
        assert status.state == JobState.RUNNING
        assert status.execution_id == "test-job"

        # Cleanup
        event.set()
        await task

    @pytest.mark.asyncio
    async def test_succeeded_after_task_completes(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()

        async def _success() -> None:
            pass

        task = asyncio.create_task(_success())
        await task  # Let it complete
        backend._tasks["test-job"] = task
        backend._results["test-job"] = None

        status = await backend.get_status("test-job")
        assert status.state == JobState.SUCCEEDED
        assert status.execution_id == "test-job"

    @pytest.mark.asyncio
    async def test_failed_with_error_message(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()

        async def _fail() -> None:
            msg = "Conversion failed"
            raise ValueError(msg)

        task = asyncio.create_task(_fail())
        try:
            await task
        except ValueError:
            pass

        backend._tasks["test-job"] = task
        backend._results["test-job"] = "Conversion failed"

        status = await backend.get_status("test-job")
        assert status.state == JobState.FAILED
        assert status.error_message == "Conversion failed"

    @pytest.mark.asyncio
    async def test_unknown_execution_id(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()
        status = await backend.get_status("nonexistent")
        assert status.state == JobState.FAILED
        assert "Unknown job" in (status.error_message or "")


class TestLocalBackendCancel:
    """Verify LocalBackend.cancel() cancels the asyncio.Task."""

    @pytest.mark.asyncio
    async def test_cancel_task(self) -> None:
        from meho_app.worker.backends.local import LocalBackend

        backend = LocalBackend()
        event = asyncio.Event()

        async def _long_task() -> None:
            await event.wait()

        task = asyncio.create_task(_long_task())
        backend._tasks["test-job"] = task
        backend._results["test-job"] = None

        await backend.cancel("test-job")
        # Yield control so the task processes the cancellation
        await asyncio.sleep(0)
        assert task.cancelled()


# ---------------------------------------------------------------------------
# KubernetesBackend tests
# ---------------------------------------------------------------------------


class TestKubernetesBackendProtocol:
    """Verify KubernetesBackend satisfies the IngestionBackend protocol."""

    def test_isinstance_check(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )
        assert isinstance(backend, IngestionBackend)


class TestKubernetesBackendDispatch:
    """Verify KubernetesBackend.dispatch() creates a K8s Job correctly."""

    @pytest.mark.asyncio
    async def test_dispatch_creates_namespaced_job(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="meho-workers",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_result = MagicMock()
        mock_result.metadata.name = "meho-ingest-test-job"

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.create_namespaced_job.return_value = mock_result
            mock_get_api.return_value = mock_api

            execution_id = await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        assert execution_id == "meho-ingest-test-job"
        mock_api.create_namespaced_job.assert_called_once()

        # Verify the Job was created in the correct namespace
        call_args = mock_api.create_namespaced_job.call_args
        assert (
            call_args.kwargs.get("namespace") == "meho-workers"
            or call_args[1].get("namespace") == "meho-workers"
        )

    @pytest.mark.asyncio
    async def test_dispatch_sets_env_vars(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_result = MagicMock()
        mock_result.metadata.name = "meho-ingest-test-job"

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.create_namespaced_job.return_value = mock_result
            mock_get_api.return_value = mock_api

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        # Extract the Job body from the call
        call_args = mock_api.create_namespaced_job.call_args
        job_body = call_args.kwargs.get("body") or call_args[1].get("body")
        container = job_body.spec.template.spec.containers[0]

        # Convert env list to dict for easy checking
        env_dict: dict[str, str] = {e.name: e.value for e in container.env}
        assert env_dict["WORKER_JOB_ID"] == "test-job-12345678"
        assert env_dict["WORKER_INPUT_URL"] == "s3://bucket/input.pdf"
        assert env_dict["WORKER_OUTPUT_URL"] == "s3://bucket/output.arrow"
        assert env_dict["OMP_NUM_THREADS"] == "4"
        assert env_dict["MALLOC_TRIM_THRESHOLD_"] == "131072"

    @pytest.mark.asyncio
    async def test_dispatch_sets_backoff_limit_and_ttl(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_result = MagicMock()
        mock_result.metadata.name = "meho-ingest-test-job"

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.create_namespaced_job.return_value = mock_result
            mock_get_api.return_value = mock_api

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        call_args = mock_api.create_namespaced_job.call_args
        job_body = call_args.kwargs.get("body") or call_args[1].get("body")
        assert job_body.spec.backoff_limit == 0
        assert job_body.spec.ttl_seconds_after_finished == 3600

    @pytest.mark.asyncio
    async def test_dispatch_sets_active_deadline(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_result = MagicMock()
        mock_result.metadata.name = "meho-ingest-test-job"

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.create_namespaced_job.return_value = mock_result
            mock_get_api.return_value = mock_api

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        call_args = mock_api.create_namespaced_job.call_args
        job_body = call_args.kwargs.get("body") or call_args[1].get("body")
        assert job_body.spec.active_deadline_seconds == 600

    @pytest.mark.asyncio
    async def test_dispatch_includes_env_overrides(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_result = MagicMock()
        mock_result.metadata.name = "meho-ingest-test-job"

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.create_namespaced_job.return_value = mock_result
            mock_get_api.return_value = mock_api

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
                env_overrides={"VOYAGE_API_KEY": "secret", "CUSTOM_VAR": "val"},
            )

        call_args = mock_api.create_namespaced_job.call_args
        job_body = call_args.kwargs.get("body") or call_args[1].get("body")
        container = job_body.spec.template.spec.containers[0]
        env_dict: dict[str, str] = {e.name: e.value for e in container.env}
        assert env_dict["VOYAGE_API_KEY"] == "secret"
        assert env_dict["CUSTOM_VAR"] == "val"

    @pytest.mark.asyncio
    async def test_dispatch_job_name_pattern(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_result = MagicMock()
        mock_result.metadata.name = "meho-ingest-abcd1234"

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.create_namespaced_job.return_value = mock_result
            mock_get_api.return_value = mock_api

            await backend.dispatch(
                job_id="abcd1234-full-uuid",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        call_args = mock_api.create_namespaced_job.call_args
        job_body = call_args.kwargs.get("body") or call_args[1].get("body")
        assert job_body.metadata.name == "meho-ingest-abcd1234"


class TestKubernetesBackendGetStatus:
    """Verify KubernetesBackend.get_status() maps K8s job states."""

    @pytest.mark.asyncio
    async def test_succeeded(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_job = MagicMock()
        mock_job.status.succeeded = 1
        mock_job.status.failed = None
        mock_job.status.active = None
        mock_job.status.start_time = datetime(2026, 1, 1, tzinfo=UTC)
        mock_job.status.completion_time = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.read_namespaced_job_status.return_value = mock_job
            mock_get_api.return_value = mock_api

            status = await backend.get_status("meho-ingest-test")

        assert status.state == JobState.SUCCEEDED

    @pytest.mark.asyncio
    async def test_failed(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_job = MagicMock()
        mock_job.status.succeeded = None
        mock_job.status.failed = 1
        mock_job.status.active = None
        mock_job.status.start_time = datetime(2026, 1, 1, tzinfo=UTC)
        mock_job.status.completion_time = None
        mock_job.status.conditions = [MagicMock(type="Failed", message="OOM killed")]

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.read_namespaced_job_status.return_value = mock_job
            mock_get_api.return_value = mock_api

            status = await backend.get_status("meho-ingest-test")

        assert status.state == JobState.FAILED

    @pytest.mark.asyncio
    async def test_running(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        mock_job = MagicMock()
        mock_job.status.succeeded = None
        mock_job.status.failed = None
        mock_job.status.active = 1
        mock_job.status.start_time = datetime(2026, 1, 1, tzinfo=UTC)
        mock_job.status.completion_time = None

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_api.read_namespaced_job_status.return_value = mock_job
            mock_get_api.return_value = mock_api

            status = await backend.get_status("meho-ingest-test")

        assert status.state == JobState.RUNNING


class TestKubernetesBackendCancel:
    """Verify KubernetesBackend.cancel() deletes with Background propagation."""

    @pytest.mark.asyncio
    async def test_cancel_deletes_job(self) -> None:
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        backend = KubernetesBackend(
            namespace="default",
            image="meho:latest",
            server_url="https://k8s.example.com",
            token="test-token",
        )

        with patch.object(backend, "_get_batch_api") as mock_get_api:
            mock_api = AsyncMock()
            mock_get_api.return_value = mock_api

            await backend.cancel("meho-ingest-test")

        mock_api.delete_namespaced_job.assert_called_once()
        call_args = mock_api.delete_namespaced_job.call_args
        assert (
            call_args.kwargs.get("name") == "meho-ingest-test"
            or call_args[1].get("name") == "meho-ingest-test"
        )
        # Verify propagation policy is Background
        body_arg = call_args.kwargs.get("body") or call_args[1].get("body")
        assert body_arg.propagation_policy == "Background"


# ---------------------------------------------------------------------------
# CloudRunBackend tests
# ---------------------------------------------------------------------------


class TestCloudRunBackendProtocol:
    """Verify CloudRunBackend satisfies the IngestionBackend protocol."""

    def test_isinstance_check(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )
        assert isinstance(backend, IngestionBackend)


class TestCloudRunBackendDispatch:
    """Verify CloudRunBackend.dispatch() calls the Cloud Run Jobs API correctly."""

    @pytest.mark.asyncio
    async def test_dispatch_calls_run_job(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        mock_operation = AsyncMock()
        mock_operation.metadata.name = (
            "projects/my-project/locations/us-central1/jobs/"
            "meho-ingestion-worker/executions/exec-001"
        )

        with patch.object(backend, "_get_jobs_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.run_job.return_value = mock_operation
            mock_get_client.return_value = mock_client

            execution_id = await backend.dispatch(
                job_id="test-job-123",
                input_url="gs://bucket/input.pdf",
                output_url="gs://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        assert "exec-001" in execution_id or "executions" in execution_id
        mock_client.run_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_sets_env_overrides(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        mock_operation = AsyncMock()
        mock_operation.metadata.name = (
            "projects/my-project/locations/us-central1/jobs/"
            "meho-ingestion-worker/executions/exec-001"
        )

        with patch.object(backend, "_get_jobs_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.run_job.return_value = mock_operation
            mock_get_client.return_value = mock_client

            await backend.dispatch(
                job_id="test-job-123",
                input_url="gs://bucket/input.pdf",
                output_url="gs://bucket/output.arrow",
                profile=_TEST_PROFILE,
                env_overrides={"VOYAGE_API_KEY": "secret"},
            )

        call_args = mock_client.run_job.call_args
        request = call_args.kwargs.get("request") or call_args[0][0]

        # Verify env overrides are included in the request overrides
        overrides = request.overrides
        container_overrides = overrides.container_overrides[0]
        env_dict: dict[str, str] = {e.name: e.value for e in container_overrides.env}
        assert env_dict["WORKER_JOB_ID"] == "test-job-123"
        assert env_dict["WORKER_INPUT_URL"] == "gs://bucket/input.pdf"
        assert env_dict["OMP_NUM_THREADS"] == "4"
        assert env_dict["MALLOC_TRIM_THRESHOLD_"] == "131072"
        assert env_dict["VOYAGE_API_KEY"] == "secret"

    @pytest.mark.asyncio
    async def test_dispatch_sets_timeout(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        mock_operation = AsyncMock()
        mock_operation.metadata.name = (
            "projects/my-project/locations/us-central1/jobs/"
            "meho-ingestion-worker/executions/exec-001"
        )

        with patch.object(backend, "_get_jobs_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.run_job.return_value = mock_operation
            mock_get_client.return_value = mock_client

            await backend.dispatch(
                job_id="test-job-123",
                input_url="gs://bucket/input.pdf",
                output_url="gs://bucket/output.arrow",
                profile=_TEST_PROFILE,  # timeout_seconds=600
            )

        call_args = mock_client.run_job.call_args
        request = call_args.kwargs.get("request") or call_args[0][0]
        overrides = request.overrides
        assert overrides.timeout.seconds == 600


class TestCloudRunBackendGetStatus:
    """Verify CloudRunBackend.get_status() maps execution states."""

    @pytest.mark.asyncio
    async def test_succeeded(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        mock_execution = MagicMock()
        mock_execution.reconciling = False
        mock_execution.succeeded_count = 1
        mock_execution.failed_count = 0

        with patch.object(backend, "_get_executions_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get_execution.return_value = mock_execution
            mock_get_client.return_value = mock_client

            status = await backend.get_status("exec-001")

        assert status.state == JobState.SUCCEEDED

    @pytest.mark.asyncio
    async def test_failed(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        mock_execution = MagicMock()
        mock_execution.reconciling = False
        mock_execution.succeeded_count = 0
        mock_execution.failed_count = 1

        with patch.object(backend, "_get_executions_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get_execution.return_value = mock_execution
            mock_get_client.return_value = mock_client

            status = await backend.get_status("exec-001")

        assert status.state == JobState.FAILED

    @pytest.mark.asyncio
    async def test_running(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        mock_execution = MagicMock()
        mock_execution.reconciling = True
        mock_execution.succeeded_count = 0
        mock_execution.failed_count = 0

        with patch.object(backend, "_get_executions_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get_execution.return_value = mock_execution
            mock_get_client.return_value = mock_client

            status = await backend.get_status("exec-001")

        assert status.state == JobState.RUNNING


class TestCloudRunBackendCancel:
    """Verify CloudRunBackend.cancel() cancels the execution."""

    @pytest.mark.asyncio
    async def test_cancel_calls_cancel_execution(self) -> None:
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        backend = CloudRunBackend(
            project="my-project",
            region="us-central1",
            job_name="meho-ingestion-worker",
        )

        with patch.object(backend, "_get_executions_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_get_client.return_value = mock_client

            await backend.cancel("exec-001")

        mock_client.cancel_execution.assert_called_once()
        call_args = mock_client.cancel_execution.call_args
        assert call_args.kwargs.get("name") == "exec-001" or (
            call_args[0] and call_args[0][0] and "exec-001" in str(call_args)
        )


# ---------------------------------------------------------------------------
# DockerBackend tests
# ---------------------------------------------------------------------------


class TestDockerBackendProtocol:
    """Verify DockerBackend satisfies the IngestionBackend protocol."""

    def test_isinstance_check(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )
        assert isinstance(backend, IngestionBackend)


class TestDockerBackendDispatch:
    """Verify DockerBackend.dispatch() runs a container via docker-py."""

    @pytest.mark.asyncio
    async def test_dispatch_calls_containers_run(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.name = "meho-ingest-test-job"
        mock_container.id = "abc123"

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.run.return_value = mock_container
            mock_create_client.return_value = mock_client

            execution_id = await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        assert execution_id == "meho-ingest-test-job"
        mock_client.containers.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_sets_mem_limit_and_cpu(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.name = "meho-ingest-test-job"

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.run.return_value = mock_container
            mock_create_client.return_value = mock_client

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        call_kwargs = mock_client.containers.run.call_args.kwargs
        assert call_kwargs["mem_limit"] == "4g"
        assert call_kwargs["cpu_count"] == 2

    @pytest.mark.asyncio
    async def test_dispatch_sets_env_vars(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.name = "meho-ingest-test-job"

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.run.return_value = mock_container
            mock_create_client.return_value = mock_client

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
                env_overrides={"VOYAGE_API_KEY": "secret"},
            )

        call_kwargs = mock_client.containers.run.call_args.kwargs
        env = call_kwargs["environment"]
        assert env["WORKER_JOB_ID"] == "test-job-12345678"
        assert env["OMP_NUM_THREADS"] == "4"
        assert env["MALLOC_TRIM_THRESHOLD_"] == "131072"
        assert env["VOYAGE_API_KEY"] == "secret"

    @pytest.mark.asyncio
    async def test_dispatch_uses_ssh_client(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.name = "meho-ingest-test-job"

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.run.return_value = mock_container
            mock_create_client.return_value = mock_client

            await backend.dispatch(
                job_id="test-job-12345678",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        mock_create_client.assert_called_once_with("ssh://user@gpu-vm")

    @pytest.mark.asyncio
    async def test_dispatch_container_name_pattern(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.name = "meho-ingest-abcd1234"

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.run.return_value = mock_container
            mock_create_client.return_value = mock_client

            await backend.dispatch(
                job_id="abcd1234-full-uuid",
                input_url="s3://bucket/input.pdf",
                output_url="s3://bucket/output.arrow",
                profile=_TEST_PROFILE,
            )

        call_kwargs = mock_client.containers.run.call_args.kwargs
        assert call_kwargs["name"] == "meho-ingest-abcd1234"


class TestDockerBackendGetStatus:
    """Verify DockerBackend.get_status() maps container states."""

    @pytest.mark.asyncio
    async def test_running(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.status = "running"

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.get.return_value = mock_container
            mock_create_client.return_value = mock_client

            status = await backend.get_status("meho-ingest-test")

        assert status.state == JobState.RUNNING

    @pytest.mark.asyncio
    async def test_exited_success(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.status = "exited"
        mock_container.attrs = {"State": {"ExitCode": 0}}

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.get.return_value = mock_container
            mock_create_client.return_value = mock_client

            status = await backend.get_status("meho-ingest-test")

        assert status.state == JobState.SUCCEEDED
        assert status.exit_code == 0

    @pytest.mark.asyncio
    async def test_exited_failure(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()
        mock_container.status = "exited"
        mock_container.attrs = {"State": {"ExitCode": 137}}

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.get.return_value = mock_container
            mock_create_client.return_value = mock_client

            status = await backend.get_status("meho-ingest-test")

        assert status.state == JobState.FAILED
        assert status.exit_code == 137


class TestDockerBackendCancel:
    """Verify DockerBackend.cancel() stops and removes the container."""

    @pytest.mark.asyncio
    async def test_cancel_stops_and_removes(self) -> None:
        from meho_app.worker.backends.docker import DockerBackend

        backend = DockerBackend(
            docker_host="ssh://user@gpu-vm",
            image="meho:latest",
        )

        mock_container = MagicMock()

        with patch("meho_app.worker.backends.docker._create_docker_client") as mock_create_client:
            mock_client = MagicMock()
            mock_client.containers.get.return_value = mock_container
            mock_create_client.return_value = mock_client

            await backend.cancel("meho-ingest-test")

        mock_container.stop.assert_called_once()
        mock_container.remove.assert_called_once()
