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

import asyncio
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import hvac
import pytest
from structlog.testing import capture_logs

from meho_backplane.scheduler.credentials import (
    AgentCredentialsUnresolvedError,
    resolve_agent_credentials,
)
from meho_backplane.scheduler.vault_credentials import (
    SECRET_FIELD,
    read_agent_secret,
    renew_scheduler_token,
    verify_scheduler_token,
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
    await write_agent_secret(_IDENTITY_REF, _AGENT_SECRET)
    root = hvac.Client(url=vault_dev_addr, token=_DEV_ROOT_TOKEN)
    payload = await asyncio.to_thread(
        root.secrets.kv.v2.read_secret_version,
        path="agents/AGENT_REPORTER/credentials",
        mount_point="secret",
        raise_on_deleted_version=False,
    )
    assert payload["data"]["data"] == {SECRET_FIELD: _AGENT_SECRET}


# --- compressed renewal + periodic-guard soak (#2827) -----------------
#
# The unit layer (``tests/test_scheduler_vault_credentials.py``) mocks
# ``renew-self`` / ``lookup-self``, so it proves the *logic* but never a
# real token surviving repeated renewals, or actually crossing an
# ``explicit_max_ttl`` cap. These two soaks drive #2668's renewal timer
# and periodic guard against the **live** dev Vault over a seconds-long
# compressed timeline — the same behaviour the multi-day deployed soak
# (#2826) will exercise for real, de-risked here first. Runtime is bounded
# to seconds; the module-scoped container boot is shared with the tests
# above.

#: Narrow policy granting exactly what the broker's renew/lookup path
#: needs: ``renew-self`` (update) and ``lookup-self`` (read). Minted
#: tokens carry **only** this policy (``no_default_policy``), so the soak
#: also proves the policy is sufficient for the renewal timer and the
#: guard's self-lookup — not merely riding on Vault's ``default`` policy.
_SOAK_POLICY_NAME: str = "meho-scheduler-soak"
_SOAK_POLICY_HCL: str = """\
path "auth/token/renew-self" {
  capabilities = ["update"]
}
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
"""

#: Short renewal period for the periodic token. Small enough that the
#: renew loop spans more than one period in a handful of seconds (proving
#: renewal keeps a short-period token alive exactly as it must keep a
#: 768h one alive over weeks); large enough to clear localhost
#: renew/lookup round-trip jitter between renewals. Were renewal a no-op,
#: this fuse would blow mid-loop and a later verify would flip to
#: ``ok is False``.
_SOAK_PERIOD_SECONDS: int = 4
#: Renew cycles driven across the soak (≥3, per the acceptance criteria).
_SOAK_RENEW_CYCLES: int = 4
#: Gap between renewals — comfortably under ``_SOAK_PERIOD_SECONDS`` so a
#: live token is never allowed to age out between renew calls, while the
#: total elapsed time (cycles * gap) still exceeds one period.
_SOAK_RENEW_GAP_SECONDS: float = 2.0


def _ensure_soak_policy(root: hvac.Client) -> None:
    """Create/update the narrow renew+lookup policy on the dev Vault."""
    root.sys.create_or_update_policy(name=_SOAK_POLICY_NAME, policy=_SOAK_POLICY_HCL)


def _mint_scheduler_token(
    root: hvac.Client,
    *,
    period: str | None = None,
    explicit_max_ttl: str | None = None,
) -> str:
    """Mint a scheduler token on the narrow policy; return its value.

    ``period`` mints a **periodic** token (the renewal-soak subject);
    ``explicit_max_ttl`` mints a token with a hard lifetime cap (the
    periodic-guard subject). ``no_default_policy`` scopes the token to
    exactly the renew/lookup grants under test.
    """
    resp = root.auth.token.create(
        policies=[_SOAK_POLICY_NAME],
        no_default_policy=True,
        renewable=True,
        period=period,
        explicit_max_ttl=explicit_max_ttl,
    )
    return str(resp["auth"]["client_token"])


@pytest.fixture
def _scheduler_soak_env(
    vault_dev_addr: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], None]]:
    """Pin Settings to the live Vault; the caller supplies the token.

    Mirrors :func:`_scheduler_vault_env`'s env-pin + ``cache_clear``
    discipline, but the scheduler token is a freshly-minted periodic /
    max-TTL token (not the dev-root), so it is pinned by the returned
    callable once the test has minted it against the live Vault.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", vault_dev_addr)

    def _pin(token: str) -> None:
        monkeypatch.setenv("VAULT_SCHEDULER_TOKEN", token)
        get_settings.cache_clear()

    yield _pin
    get_settings.cache_clear()


async def test_periodic_token_survives_renewal_soak(
    vault_dev_addr: str, _scheduler_soak_env: Callable[[str], None]
) -> None:
    """A real periodic token stays live across renew cycles spanning >1 period.

    AC #1. Mint a real periodic token with a short period, drive
    :func:`renew_scheduler_token` across ``_SOAK_RENEW_CYCLES`` cycles
    whose total elapsed time exceeds the period, and assert the token
    never dies: :func:`verify_scheduler_token` reports ``ok`` on every
    cycle, ``scheduler_vault_token_renewed`` is observed, and
    ``scheduler_vault_token_dead`` is never logged. Renewal is what keeps
    it alive — were it a no-op, the ``_SOAK_PERIOD_SECONDS`` fuse would
    blow mid-loop and a later verify would flip to ``ok is False``.
    """
    root = hvac.Client(url=vault_dev_addr, token=_DEV_ROOT_TOKEN)
    await asyncio.to_thread(_ensure_soak_policy, root)
    token = await asyncio.to_thread(_mint_scheduler_token, root, period=f"{_SOAK_PERIOD_SECONDS}s")
    _scheduler_soak_env(token)

    # Live at the outset, and a healthy periodic token draws no guard.
    baseline = await verify_scheduler_token(reason="soak-baseline")
    assert baseline.ok is True
    assert baseline.will_expire_reason is None

    with capture_logs() as logs:
        for _ in range(_SOAK_RENEW_CYCLES):
            await renew_scheduler_token(reason="periodic")
            await asyncio.sleep(_SOAK_RENEW_GAP_SECONDS)
            status = await verify_scheduler_token(reason="soak")
            assert status.ok is True

    events = [entry["event"] for entry in logs]
    assert events.count("scheduler_vault_token_renewed") >= 3
    assert "scheduler_vault_token_dead" not in events


async def test_explicit_max_ttl_token_trips_periodic_guard(
    vault_dev_addr: str, _scheduler_soak_env: Callable[[str], None]
) -> None:
    """A real ``explicit_max_ttl`` token trips the #2668 periodic guard.

    AC #2. Mint a token carrying an ``explicit_max_ttl`` (a hard lifetime
    cap renewal cannot cross), point the broker at it, and assert
    :func:`verify_scheduler_token` — reading the **live** ``lookup-self``
    payload, not a mock — sets ``will_expire_reason`` while the token is
    still ``ok``, and emits the loud ``scheduler_vault_token_will_expire``
    ERROR. The cap sits well above the test runtime, so the token is
    unambiguously alive at lookup and the verdict comes from the cap, not
    from an already-expired token.
    """
    root = hvac.Client(url=vault_dev_addr, token=_DEV_ROOT_TOKEN)
    await asyncio.to_thread(_ensure_soak_policy, root)
    token = await asyncio.to_thread(_mint_scheduler_token, root, explicit_max_ttl="60s")
    _scheduler_soak_env(token)

    with capture_logs() as logs:
        status = await verify_scheduler_token(reason="soak-guard")

    assert status.ok is True  # live right now …
    assert status.will_expire_reason is not None
    assert "explicit_max_ttl" in status.will_expire_reason  # … but doomed despite renewal
    warn = next(e for e in logs if e["event"] == "scheduler_vault_token_will_expire")
    assert warn["log_level"] == "error"
    assert "explicit_max_ttl" in warn["reasons"]
