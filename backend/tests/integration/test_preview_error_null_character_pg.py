# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: a refusal that quotes a null character is stored.

When a dry run fails, the stored approval request keeps the reason in
``proposed_effect.preview_error``. An admission check can quote a value the
caller sent, and that value can hold a null character. PostgreSQL refuses a
null character in a JSON column, so MEHO writes it as the two characters
``\\0``. SQLite (the unit-suite driver) stores the null character, so only a
real Postgres shows that the row is saved and read back.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations._preview import preview_failure_reason
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from tests._people_behind_helpers import give_known_person

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PARAMS: dict[str, Any] = {"name": "web-01"}


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _requester() -> Operator:
    return Operator(
        sub="agent:null-in-error",
        name="Null In Error Agent",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _store(preview_error: str) -> uuid.UUID:
    await give_known_person(_requester())
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_requester(),
            connector_id="rectest-1.x",
            op_id="rectest.null-in-error",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={
                "op_class": "write",
                "preview_unavailable": True,
                "preview_error": preview_error,
            },
            preview_hash="preview-hash",
        )
        await session.commit()
        return request.id


@_skip_no_docker
async def test_a_refusal_with_a_null_character_is_saved_and_read_back(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    refusal = RuntimeError('admission webhook denied the request: label "a\x00b" is not allowed')

    request_id = await _store(preview_failure_reason(refusal))

    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.proposed_effect["preview_error"] == (
            'RuntimeError: admission webhook denied the request: label "a\\0b" is not allowed'
        )


@_skip_no_docker
async def test_postgres_refuses_the_raw_null_character(
    pg_engine: None,  # noqa: F811 — fixture
) -> None:
    """The control: without the escape, PostgreSQL does not save the row."""
    with pytest.raises(Exception, match=r"0000|0x00"):
        await _store("RuntimeError: a\x00b")
