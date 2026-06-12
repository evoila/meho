# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.19-T2 (#1478) — live Vault dev-mode harness for the scheduler broker.

Boots a real ``hashicorp/vault:1.18`` server in dev mode via testcontainers
and exercises the scheduler-service-token credential broker
(:mod:`meho_backplane.scheduler.vault_credentials`) and the Vault-first
:func:`meho_backplane.scheduler.credentials.resolve_agent_credentials`
against the **live** Vault — not a mock. This is the layer that proves the
DoD: an agent's ``client_credentials`` secret written at registration is
read back by the operator-less scheduler with **no pod env var set**.

What it covers
==============

* :func:`write_agent_secret` persists a secret at the configured Vault
  path under a static service token, and :func:`read_agent_secret` reads
  it back — full hvac KV-v2 round-trip, real ``data/`` infix handling.
* :func:`resolve_agent_credentials` returns the Vault-sourced secret with
  **no** ``MEHO_AGENT_SECRET_*`` env var present (the autonomous-loop AC).
* A secret absent from Vault and absent from the env var raises
  :class:`AgentCredentialsUnresolvedError` (loud, trigger-preserving).

CI selection
============

Lives under ``tests/integration/`` (deselected by the unit lane, run by
the integration lane). A Docker-socket-absent sandbox skips cleanly via
the same heuristic every other testcontainers suite uses.

Secrets
=======

The dev-root token is generated *into* the container via
``VAULT_DEV_ROOT_TOKEN_ID`` and only held in module state. The scheduler
service token is the same dev-root token (dev mode has root policy);
production binds a narrow read/write policy. The seeded ``client_secret``
is a throwaway value on an in-memory Vault that never persists.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import hvac
import pytest

from meho_backplane.scheduler.credentials import (
    AgentCredentialsUnresolvedError,
    resolve_agent_credentials,
)
from meho_backplane.scheduler.vault_credentials import (
    SECRET_FIELD,
    read_agent_secret,
    write_agent_secret,
)
from meho_backplane.settings import get_settings


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

#: Dev-mode root token, generated *into* the container. Throwaway, scoped
#: to a per-test-run in-memory Vault. Doubles as the scheduler service
#: token for the test (dev mode grants it root policy).
_DEV_ROOT_TOKEN: str = "meho-dev-root-1478"

_IDENTITY_REF: str = "agent:reporter"
_AGENT_SECRET: str = "dev-only-client-secret-1478"


@pytest.fixture(scope="module")
def vault_dev_addr() -> Iterator[str]:
    """Boot ``hashicorp/vault:1.18 -dev`` and yield its address.

    Module scope amortises the container boot. Image overridable via
    ``MEHO_TEST_VAULT_IMAGE`` so the CI runner pulls through the in-cluster
    Harbor proxy (same env-knob shape as the other Vault dev fixtures).
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    image = os.environ.get("MEHO_TEST_VAULT_IMAGE", "hashicorp/vault:1.18")
    container = (
        DockerContainer(image)
        .with_env("VAULT_DEV_ROOT_TOKEN_ID", _DEV_ROOT_TOKEN)
        .with_env("VAULT_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
        .with_exposed_ports(8200)
        .with_kwargs(cap_add=["IPC_LOCK"])
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"vault dev container failed to start ({type(exc).__name__}): {exc}")

    try:
        wait_for_log_message(container, "Vault server started!", timeout=60)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8200)
        yield f"http://{host}:{port}"
    finally:
        container.stop()


@pytest.fixture
def _scheduler_vault_env(vault_dev_addr: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin Settings to the live Vault + scheduler service token.

    Crucially does **not** set any ``MEHO_AGENT_SECRET_*`` env var — the
    DoD is that the secret resolves from Vault with no pod env var.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", vault_dev_addr)
    monkeypatch.setenv("VAULT_SCHEDULER_TOKEN", _DEV_ROOT_TOKEN)
    # Belt-and-suspenders: ensure no env-var secret is present so a pass
    # cannot come from the fallback path.
    monkeypatch.delenv("MEHO_AGENT_SECRET_AGENT_REPORTER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_write_then_read_round_trip(_scheduler_vault_env: None) -> None:
    """write_agent_secret -> read_agent_secret round-trips against live Vault."""
    api_path = await write_agent_secret(_IDENTITY_REF, _AGENT_SECRET)
    assert api_path == "secret/data/agents/AGENT_REPORTER/credentials"

    read_back = await read_agent_secret(_IDENTITY_REF)
    assert read_back == _AGENT_SECRET


async def test_read_missing_returns_none(_scheduler_vault_env: None) -> None:
    """A never-written agent path reads back as ``None`` (env fallback signal)."""
    assert await read_agent_secret("agent:never-written") is None


async def test_resolve_is_vault_sourced_with_no_env_var(
    _scheduler_vault_env: None,
) -> None:
    """The scheduler resolves the secret from Vault with NO pod env var set.

    This is the headline DoD: an API-registered agent (whose secret was
    persisted to Vault at registration) is schedulable without an operator
    wiring ``MEHO_AGENT_SECRET_*`` into the pod env.
    """
    await write_agent_secret(_IDENTITY_REF, _AGENT_SECRET)

    client_id, secret = await resolve_agent_credentials(_IDENTITY_REF)

    assert client_id == _IDENTITY_REF
    assert secret == _AGENT_SECRET
    # Prove the env var really was absent (no accidental fallback).
    assert os.environ.get("MEHO_AGENT_SECRET_AGENT_REPORTER") is None


async def test_resolve_raises_when_neither_vault_nor_env(
    _scheduler_vault_env: None,
) -> None:
    """Secret in neither Vault nor env -> AgentCredentialsUnresolvedError."""
    with pytest.raises(AgentCredentialsUnresolvedError):
        await resolve_agent_credentials("agent:no-secret-anywhere")


async def test_seeded_payload_field_shape(vault_dev_addr: str, _scheduler_vault_env: None) -> None:
    """The persisted payload uses the agreed SECRET_FIELD key.

    A direct root-client read confirms the write shape independently of the
    broker's own read path (defends against a write/read key drift).
    """
    import asyncio

    await write_agent_secret(_IDENTITY_REF, _AGENT_SECRET)
    root = hvac.Client(url=vault_dev_addr, token=_DEV_ROOT_TOKEN)
    payload = await asyncio.to_thread(
        root.secrets.kv.v2.read_secret_version,
        path="agents/AGENT_REPORTER/credentials",
        mount_point="secret",
        raise_on_deleted_version=False,
    )
    assert payload["data"]["data"] == {SECRET_FIELD: _AGENT_SECRET}
