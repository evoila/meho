# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A target change makes connectors drop the login they keep for it (#3879).

Connectors keep a login (token, session, open connection) per target. Before
#3879 a PATCH to the target's ``secret_ref`` did not reach that kept login, so
dispatch used the old credential until the login expired. These tests pin:

* the new ``invalidate_credentials`` hooks on the GitHub, Keycloak, VCF
  Automation and SSH connectors drop only the changed target's entry;
* :func:`forget_target_in_connectors` asks every cached connector, and one
  failing hook does not stop the others;
* ``PATCH /api/v1/targets/{name}`` runs the drop when a connection field is
  sent, and not otherwise;
* the drop runs once more after the transaction commits, so a login kept
  between the PATCH and the commit is dropped too;
* end to end: after a PATCH to a narrower credential, the next GitHub call
  uses the new credential.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.api.v1.targets import (
    _AFTER_COMMIT_DROP_TASKS,
    _drop_kept_logins_on_connection_change,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.github import GitHubPATCredentials, GitHubRestConnector
from meho_backplane.connectors.keycloak.connector import KeycloakConnector, _CachedToken
from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    forget_target_in_connectors,
    reset_connector_instance_cache,
)

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_WIDE_REF = f"tenants/{DEFAULT_TENANT_ID}/gh-wide"
_NARROW_REF = f"tenants/{DEFAULT_TENANT_ID}/gh-narrow"


@dataclass(frozen=True)
class _Target:
    name: str = "t"
    host: str = "api.github.com"
    port: int | None = None
    secret_ref: str | None = _WIDE_REF
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = field(default_factory=lambda: uuid.UUID(DEFAULT_TENANT_ID))


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingConnector:
    """Fake cached connector that records which targets it was told to forget."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def invalidate_session(self, target: Any) -> None:
        self.calls.append(("invalidate_session", str(target.id)))

    async def invalidate_credentials(self, target: Any) -> None:
        self.calls.append(("invalidate_credentials", str(target.id)))


class _FailingConnector:
    async def invalidate_session(self, target: Any) -> None:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _clean_instance_cache() -> Iterator[None]:
    reset_connector_instance_cache()
    yield
    reset_connector_instance_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# New connector hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_invalidate_credentials_drops_only_that_target() -> None:
    changed, other = _Target(), _Target(name="other")
    conn = GitHubRestConnector()
    conn._pat_tokens[target_cache_key(changed)] = "pat-old"
    conn._installation_tokens[target_cache_key(changed)] = MagicMock()
    conn._pat_tokens[target_cache_key(other)] = "pat-other"

    await conn.invalidate_credentials(changed)

    assert target_cache_key(changed) not in conn._pat_tokens
    assert target_cache_key(changed) not in conn._installation_tokens
    assert conn._pat_tokens[target_cache_key(other)] == "pat-other"


@pytest.mark.asyncio
async def test_keycloak_invalidate_credentials_drops_only_that_target() -> None:
    changed, other = _Target(), _Target(name="other")
    conn = KeycloakConnector()
    conn._admin_tokens[target_cache_key(changed)] = _CachedToken("tok-old", 1e12)
    conn._admin_tokens[target_cache_key(other)] = _CachedToken("tok-other", 1e12)

    await conn.invalidate_credentials(changed)

    assert target_cache_key(changed) not in conn._admin_tokens
    assert target_cache_key(other) in conn._admin_tokens


@pytest.mark.asyncio
async def test_vcf_automation_invalidate_credentials_drops_both_planes() -> None:
    changed, other = _Target(), _Target(name="other")
    conn = VcfAutomationConnector()
    conn._provider_tokens[target_cache_key(changed)] = "provider-old"
    conn._tenant_tokens[target_cache_key(changed)] = "tenant-old"
    conn._tenant_tokens[target_cache_key(other)] = "tenant-other"

    await conn.invalidate_credentials(changed)

    assert target_cache_key(changed) not in conn._provider_tokens
    assert target_cache_key(changed) not in conn._tenant_tokens
    assert target_cache_key(other) in conn._tenant_tokens


class _FakeSshConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Ssh(SshConnector):
    """Concrete SSH connector; the abstract methods are not used here."""

    fingerprint = probe = execute = None  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_ssh_invalidate_credentials_closes_and_drops_the_pooled_connection() -> None:
    changed, other = _Target(), _Target(name="other")
    conn = _Ssh()
    old, kept = _FakeSshConnection(), _FakeSshConnection()
    conn._connections[target_cache_key(changed)] = (old, 0.0)  # type: ignore[assignment]
    conn._connections[target_cache_key(other)] = (kept, 0.0)  # type: ignore[assignment]

    await conn.invalidate_credentials(changed)  # type: ignore[arg-type]

    assert old.closed
    assert target_cache_key(changed) not in conn._connections
    assert not kept.closed
    assert target_cache_key(other) in conn._connections


# ---------------------------------------------------------------------------
# forget_target_in_connectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_target_asks_every_cached_connector_and_survives_a_failure() -> None:
    target = _Target()
    first, second = _RecordingConnector(), _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[type("A", (), {})] = first  # type: ignore[index]
    _CONNECTOR_INSTANCE_CACHE[type("B", (), {})] = _FailingConnector()  # type: ignore[index]
    _CONNECTOR_INSTANCE_CACHE[type("C", (), {})] = object()  # type: ignore[index]
    _CONNECTOR_INSTANCE_CACHE[type("D", (), {})] = second  # type: ignore[index]

    await forget_target_in_connectors(target)

    expected = [
        ("invalidate_session", str(target.id)),
        ("invalidate_credentials", str(target.id)),
    ]
    assert first.calls == expected
    assert second.calls == expected


# ---------------------------------------------------------------------------
# PATCH /api/v1/targets/{name}
# ---------------------------------------------------------------------------


def _patch(client: TestClient, name: str, body: dict[str, Any]) -> Any:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.patch(
            f"/api/v1/targets/{name}",
            json=body,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"secret_ref": _NARROW_REF},
        {"host": "api.github.com"},
        {"extras": {"owner": "example"}},
        {"verify_tls": True},
    ],
)
async def test_patch_of_a_connection_field_drops_the_kept_login(
    client: TestClient, body: dict[str, Any]
) -> None:
    row = await _insert_target(name="gh", product="github", secret_ref=_WIDE_REF)
    recorder = _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[GitHubRestConnector] = recorder  # type: ignore[assignment]

    response = _patch(client, "gh", body)

    assert response.status_code == 200
    assert ("invalidate_credentials", str(row.id)) in recorder.calls


@pytest.mark.asyncio
async def test_patch_of_notes_only_keeps_the_login(client: TestClient) -> None:
    await _insert_target(name="gh", product="github", secret_ref=_WIDE_REF)
    recorder = _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[GitHubRestConnector] = recorder  # type: ignore[assignment]

    response = _patch(client, "gh", {"notes": "just a note"})

    assert response.status_code == 200
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_the_drop_runs_again_after_the_commit() -> None:
    """A login kept between the change and the commit is dropped after the commit."""
    row = await _insert_target(name="gh", product="github", secret_ref=_WIDE_REF)
    conn = GitHubRestConnector()
    _CONNECTOR_INSTANCE_CACHE[GitHubRestConnector] = conn
    key = target_cache_key(row)

    async with get_sessionmaker()() as session:
        t = await session.get(TargetORM, row.id)
        assert t is not None
        t.secret_ref = _NARROW_REF
        await _drop_kept_logins_on_connection_change(session, t, {"secret_ref": _NARROW_REF})
        # A dispatch that read the old row before the commit keeps a login
        # made with the old secret.
        conn._pat_tokens[key] = "pat-wide-scope"
        await session.commit()

    await asyncio.gather(*_AFTER_COMMIT_DROP_TASKS)
    assert key not in conn._pat_tokens


@pytest.mark.asyncio
async def test_secret_change_to_a_narrower_credential_is_used_on_the_next_call(
    client: TestClient,
) -> None:
    """The case from #3879: switch to a narrower credential, next call uses it."""
    tokens_by_ref = {_WIDE_REF: "pat-wide-scope", _NARROW_REF: "pat-one-repo"}
    loads: list[str] = []

    async def _loader(target: Any, operator: Operator) -> GitHubPATCredentials:
        loads.append(target.secret_ref)
        return GitHubPATCredentials(token=tokens_by_ref[target.secret_ref])

    conn = GitHubRestConnector(credentials_loader=_loader)
    _CONNECTOR_INSTANCE_CACHE[GitHubRestConnector] = conn
    row = await _insert_target(name="gh", product="github", secret_ref=_WIDE_REF)
    before = _Target(name="gh", secret_ref=_WIDE_REF, id=row.id)
    first = await conn.auth_headers(before, _operator())
    assert first["Authorization"] == "Bearer pat-wide-scope"

    response = _patch(client, "gh", {"secret_ref": _NARROW_REF})
    assert response.status_code == 200

    after = _Target(name="gh", secret_ref=_NARROW_REF, id=row.id)
    second = await conn.auth_headers(after, _operator())
    assert second["Authorization"] == "Bearer pat-one-repo"
    assert loads == [_WIDE_REF, _NARROW_REF]
