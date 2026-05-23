# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.9-T2 — live Vault dev-mode harness for the basic-credentials helper.

Boots a real ``hashicorp/vault:1.18`` server in dev mode via
testcontainers, seeds a KV-v2 secret holding a ``{username, password}``
pair, then exercises
:func:`meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
against the **live** Vault. The unit suite
(``tests/test_connectors_vault_creds.py``) mocks hvac via the in-process
fake; this is the integration layer that proves the helper actually
round-trips against a running Vault KV-v2 store — the rubric State-2 bar
the issue's first acceptance criterion mandates ("verified against a live
Vault dev-mode harness (not a mock)").

CI selection
============

This module lives under ``tests/integration/``, which the unit CI lane
deselects (``pytest --ignore=tests/integration``) and the dedicated
integration lane runs (``pytest -x tests/integration/``). A
Docker-socket-absent sandbox skips cleanly via the same heuristic every
other testcontainers suite uses — so the secret-free unit gate stays
green without this live read, exactly as the issue requires.

The Vault client seam
=====================

Dev mode has no OIDC auth method wired, so this harness monkeypatches
:func:`meho_backplane.auth.vault.vault_client_for_operator` (the symbol
the helper imported) to yield a root-token :class:`hvac.Client` bound to
the container. The full helper code path (read → structural unwrap →
field extraction → no-secret log) runs unchanged; only the credential
*acquisition* is swapped, exactly as a production OIDC login would have
produced an authenticated client. This is the same single-seam approach
``tests/integration/test_connectors_vault_dev_e2e.py`` uses.

The dev-root token is generated *into* the container via
``VAULT_DEV_ROOT_TOKEN_ID`` and only ever held in module state — never
written to a workflow log, a committed file, or an assertion message.
The seeded ``{username, password}`` is a throwaway dev-mode credential on
an in-memory Vault that never persists and is never reachable off the
runner.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import hvac
import pytest
from structlog.testing import capture_logs

import meho_backplane.connectors._shared.vault_creds as vault_creds_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_basic_credentials,
)

# ---------------------------------------------------------------------------
# Docker-availability gate — identical heuristic to
# tests/integration/conftest.py so every testcontainers suite skips on
# the same signal.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

#: Dev-mode root token, generated *into* the container. A throwaway value
#: scoped to a per-test-run in-memory Vault that never persists and is
#: never reachable off the runner. Never logged or echoed into an
#: assertion message.
_DEV_ROOT_TOKEN: str = "meho-dev-root-941"

#: KV-v2 mount dev mode provides out of the box (``-dev`` mounts
#: ``secret/`` as v2). The helper's ``DEFAULT_KV_MOUNT`` is ``"secret"``,
#: so the read resolves here without an explicit ``mount``.
_KV_MOUNT: str = "secret"

#: The seeded secret path + values. Throwaway dev credentials.
_SECRET_PATH: str = "targets/vc-lab-01"
_SEEDED_USERNAME: str = "svc-meho"
_SEEDED_PASSWORD: str = "dev-only-pw-941"


@dataclass
class _Target:
    """Minimal target satisfying ``BasicCredentialsTargetLike``."""

    name: str = "vc-lab-01"
    host: str = "vc-lab-01.example.test"
    secret_ref: str | None = _SECRET_PATH


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt so the fail-closed gate passes."""
    return Operator(
        sub="op-vault-creds-e2e",
        name=None,
        email=None,
        raw_jwt="dev.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Vault dev-mode container — module-scoped (one boot, seeded once)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vault_dev_addr() -> Iterator[str]:
    """Boot ``hashicorp/vault:1.18 -dev``, seed it, yield its address.

    Module scope amortises the container boot across every test. Image
    overridable via ``MEHO_TEST_VAULT_IMAGE`` so the CI runner pulls
    through the in-cluster Harbor proxy (same env-knob shape as
    ``MEHO_TEST_PGVECTOR_IMAGE``).
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local import: testcontainers transitively imports the docker SDK
    # which probes the socket on import. Keeping it inside the fixture
    # lets the module collect on a no-Docker sandbox and skip cleanly.
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

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
        # Any boot failure → clean skip, not a red suite — same stance
        # the other container fixtures take.
        pytest.skip(f"vault dev container failed to start ({type(exc).__name__}): {exc}")

    try:
        wait_for_logs(container, "Vault server started!", timeout=60)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8200)
        addr = f"http://{host}:{port}"
        _seed_vault(addr)
        yield addr
    finally:
        container.stop()


def _root_client(addr: str) -> hvac.Client:
    """Construct a root-token hvac client bound to the dev container."""
    return hvac.Client(url=addr, token=_DEV_ROOT_TOKEN)


def _seed_vault(addr: str) -> None:
    """Seed the KV-v2 secret the helper reads.

    * ``targets/vc-lab-01`` — a full ``{username, password}`` pair (the
      happy path).
    * ``targets/no-password`` — only ``username`` (the missing-field
      contract against a real store).
    """
    client = _root_client(addr)
    client.secrets.kv.v2.create_or_update_secret(
        path=_SECRET_PATH,
        secret={"username": _SEEDED_USERNAME, "password": _SEEDED_PASSWORD},
        mount_point=_KV_MOUNT,
    )
    client.secrets.kv.v2.create_or_update_secret(
        path="targets/no-password",
        secret={"username": _SEEDED_USERNAME},
        mount_point=_KV_MOUNT,
    )


@pytest.fixture
def operator_context_vault(
    vault_dev_addr: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Swap ``vault_client_for_operator`` for a root-token client at the dev Vault.

    Patches the symbol the helper imported
    (``vault_creds_module.vault_client_for_operator``) so the helper's
    own ``async with vault_client_for_operator(operator) as client``
    yields a client bound to the live container. No revoke is needed for
    a root token on a throwaway in-memory dev Vault.
    """

    @asynccontextmanager
    async def _root_client_cm(_operator: Any) -> AsyncIterator[hvac.Client]:
        yield _root_client(vault_dev_addr)

    monkeypatch.setattr(vault_creds_module, "vault_client_for_operator", _root_client_cm)
    yield vault_dev_addr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_load_basic_credentials_against_dev_vault(
    operator_context_vault: str,
) -> None:
    """The helper reads the seeded {username, password} from the live Vault."""
    creds = await load_basic_credentials(_Target(), _make_operator())

    assert creds == {"username": _SEEDED_USERNAME, "password": _SEEDED_PASSWORD}


async def test_missing_field_against_dev_vault(operator_context_vault: str) -> None:
    """A real secret missing 'password' raises the helper error naming it."""
    with pytest.raises(VaultCredentialsReadError) as exc:
        await load_basic_credentials(_Target(secret_ref="targets/no-password"), _make_operator())

    assert "missing required field 'password'" in str(exc.value)


async def test_missing_path_against_dev_vault(operator_context_vault: str) -> None:
    """Reading an unseeded path surfaces as a read-phase error, not a crash.

    hvac raises ``InvalidPath`` for a non-existent KV-v2 path; that
    propagates out of the helper (it is not a login-phase
    ``VaultClientError``, so a caller maps it to the read phase). The
    assertion here is that the call fails rather than returning a bogus
    credential — the exact exception type is hvac's, surfaced verbatim.
    """
    import hvac.exceptions

    with pytest.raises((VaultCredentialsReadError, hvac.exceptions.VaultError)):
        await load_basic_credentials(_Target(secret_ref="targets/does-not-exist"), _make_operator())


async def test_no_secret_in_logs_against_dev_vault(
    operator_context_vault: str,
) -> None:
    """The live read logs only target/host/field-names — no credential value."""
    with capture_logs() as captured:
        creds = await load_basic_credentials(_Target(), _make_operator())

    assert creds["password"] == _SEEDED_PASSWORD  # sanity: the read worked

    serialised = repr(captured)
    assert _SEEDED_PASSWORD not in serialised
    assert _SEEDED_USERNAME not in serialised

    loaded = [e for e in captured if e.get("event") == "vault_basic_credentials_loaded"]
    assert len(loaded) == 1
    assert loaded[0]["target"] == "vc-lab-01"
