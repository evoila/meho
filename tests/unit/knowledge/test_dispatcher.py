# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for the IngestionDispatcher factory and routing logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.worker.backends.protocol import JobState, JobStatus, ResourceProfile


# ---------------------------------------------------------------------------
# create_backend() tests
# ---------------------------------------------------------------------------


class TestCreateBackend:
    """Tests for the create_backend() factory function."""

    @patch("meho_app.worker.dispatcher.get_config")
    def test_local_backend(self, mock_config: MagicMock) -> None:
        """create_backend() with ingestion_backend='local' returns LocalBackend."""
        mock_config.return_value = MagicMock(ingestion_backend="local")

        from meho_app.worker.dispatcher import create_backend

        backend = create_backend()

        from meho_app.worker.backends.local import LocalBackend

        assert isinstance(backend, LocalBackend)

    @patch("meho_app.worker.dispatcher.get_config")
    def test_kubernetes_backend(self, mock_config: MagicMock) -> None:
        """create_backend() with ingestion_backend='kubernetes' returns KubernetesBackend."""
        mock_config.return_value = MagicMock(
            ingestion_backend="kubernetes",
            k8s_ingestion_namespace="meho-ingestion",
            worker_image="meho:latest",
            k8s_ingestion_server="",
            k8s_ingestion_token=None,
            k8s_ingestion_ca_cert=None,
            k8s_ingestion_service_account=None,
        )

        from meho_app.worker.dispatcher import create_backend

        backend = create_backend()

        from meho_app.worker.backends.kubernetes import KubernetesBackend

        assert isinstance(backend, KubernetesBackend)

    @patch("meho_app.worker.dispatcher.get_config")
    def test_cloudrun_backend(self, mock_config: MagicMock) -> None:
        """create_backend() with ingestion_backend='cloudrun' returns CloudRunBackend."""
        mock_config.return_value = MagicMock(
            ingestion_backend="cloudrun",
            cloudrun_project="my-project",
            cloudrun_region="us-central1",
            cloudrun_job_name="meho-ingest",
        )

        from meho_app.worker.dispatcher import create_backend

        backend = create_backend()

        from meho_app.worker.backends.cloudrun import CloudRunBackend

        assert isinstance(backend, CloudRunBackend)

    @patch("meho_app.worker.dispatcher.get_config")
    def test_docker_backend(self, mock_config: MagicMock) -> None:
        """create_backend() with ingestion_backend='docker' returns DockerBackend."""
        mock_config.return_value = MagicMock(
            ingestion_backend="docker",
            docker_ingestion_host="ssh://user@gpu-host",
            worker_image="meho:latest",
        )

        from meho_app.worker.dispatcher import create_backend

        backend = create_backend()

        from meho_app.worker.backends.docker import DockerBackend

        assert isinstance(backend, DockerBackend)

    @patch("meho_app.worker.dispatcher.get_config")
    def test_invalid_backend_raises_value_error(self, mock_config: MagicMock) -> None:
        """create_backend() with invalid value raises ValueError."""
        mock_config.return_value = MagicMock(ingestion_backend="invalid_backend")

        from meho_app.worker.dispatcher import create_backend

        with pytest.raises(ValueError, match="Unknown ingestion backend"):
            create_backend()


# ---------------------------------------------------------------------------
# IngestionDispatcher tests
# ---------------------------------------------------------------------------


class TestIngestionDispatcher:
    """Tests for the IngestionDispatcher class."""

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.estimate_resources")
    @patch("meho_app.worker.dispatcher.create_backend")
    async def test_dispatch_calls_estimate_resources(
        self, mock_create: MagicMock, mock_estimate: MagicMock
    ) -> None:
        """dispatch() calls estimate_resources() with page_count."""
        profile = ResourceProfile(
            memory_gb=8, cpu=4, gpu=False, timeout_seconds=3600, size_category="medium"
        )
        mock_estimate.return_value = profile
        mock_backend = AsyncMock()
        mock_backend.dispatch.return_value = "exec-123"
        mock_create.return_value = mock_backend

        from meho_app.worker.dispatcher import IngestionDispatcher

        dispatcher = IngestionDispatcher()
        await dispatcher.dispatch(
            job_id="job-1",
            input_url="https://example.com/input",
            output_url="https://example.com/output",
            page_count=200,
        )

        mock_estimate.assert_called_once_with(200)

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.estimate_resources")
    @patch("meho_app.worker.dispatcher.create_backend")
    async def test_dispatch_passes_profile_to_backend(
        self, mock_create: MagicMock, mock_estimate: MagicMock
    ) -> None:
        """dispatch() passes ResourceProfile to backend.dispatch()."""
        profile = ResourceProfile(
            memory_gb=4, cpu=2, gpu=False, timeout_seconds=600, size_category="small"
        )
        mock_estimate.return_value = profile
        mock_backend = AsyncMock()
        mock_backend.dispatch.return_value = "exec-456"
        mock_create.return_value = mock_backend

        from meho_app.worker.dispatcher import IngestionDispatcher

        dispatcher = IngestionDispatcher()
        result = await dispatcher.dispatch(
            job_id="job-2",
            input_url="https://in.url",
            output_url="https://out.url",
            page_count=30,
        )

        assert result == "exec-456"
        mock_backend.dispatch.assert_called_once_with(
            job_id="job-2",
            input_url="https://in.url",
            output_url="https://out.url",
            profile=profile,
            env_overrides=None,
        )

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.estimate_resources")
    @patch("meho_app.worker.dispatcher.create_backend")
    async def test_dispatch_passes_env_overrides(
        self, mock_create: MagicMock, mock_estimate: MagicMock
    ) -> None:
        """dispatch() passes env_overrides through to backend."""
        profile = ResourceProfile(
            memory_gb=2, cpu=1, gpu=False, timeout_seconds=120, size_category="tiny"
        )
        mock_estimate.return_value = profile
        mock_backend = AsyncMock()
        mock_backend.dispatch.return_value = "exec-789"
        mock_create.return_value = mock_backend

        from meho_app.worker.dispatcher import IngestionDispatcher

        dispatcher = IngestionDispatcher()
        env = {"WORKER_CHUNK_PREFIX": "some-prefix"}
        await dispatcher.dispatch(
            job_id="job-3",
            input_url="https://in.url",
            output_url="https://out.url",
            page_count=5,
            env_overrides=env,
        )

        mock_backend.dispatch.assert_called_once_with(
            job_id="job-3",
            input_url="https://in.url",
            output_url="https://out.url",
            profile=profile,
            env_overrides=env,
        )

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.create_backend")
    async def test_get_status_delegates_to_backend(self, mock_create: MagicMock) -> None:
        """get_status() delegates to backend.get_status()."""
        expected_status = JobStatus(state=JobState.RUNNING, execution_id="exec-1")
        mock_backend = AsyncMock()
        mock_backend.get_status.return_value = expected_status
        mock_create.return_value = mock_backend

        from meho_app.worker.dispatcher import IngestionDispatcher

        dispatcher = IngestionDispatcher()
        status = await dispatcher.get_status("exec-1")

        assert status == expected_status
        mock_backend.get_status.assert_called_once_with("exec-1")

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.create_backend")
    async def test_cancel_delegates_to_backend(self, mock_create: MagicMock) -> None:
        """cancel() delegates to backend.cancel()."""
        mock_backend = AsyncMock()
        mock_create.return_value = mock_backend

        from meho_app.worker.dispatcher import IngestionDispatcher

        dispatcher = IngestionDispatcher()
        await dispatcher.cancel("exec-2")

        mock_backend.cancel.assert_called_once_with("exec-2")
