# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.script.run`` refuses a secret in ``env`` before the call waits.

The approval card shows every ``env`` value, so a secret belongs in
``secret_env``. A call whose ``env`` name holds a secret word, or whose
``env`` value has a secret shape, is refused, and no approval request is
stored. A plain value still waits for approval.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.operations.dispatcher import dispatch
from tests.test_connectors_linux_write import (
    _CONNECTOR_ID,
    _OPERATOR,
    _TARGET_NAME,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    linux_write_registered,  # noqa: F401 -- fixture
)

_SCRIPT = "#!/bin/bash\nset -eu\ntar -cf /tmp/out.tar /etc/app\n"

# Made-up values, built in parts so a secret scanner does not read them as real.
_JWT = "eyJ" + "h" * 30 + "." + "p" * 30 + "." + "s" * 30
_PEM_BODY = "madeupkeybody" * 4
_PEM = "-----BEGIN " + "PRIVATE KEY-----\n" + _PEM_BODY + "\n-----END " + "PRIVATE KEY-----"


async def _call(env: dict[str, str]) -> dict[str, Any]:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import resolve_target

    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id="linux.script.run",
        target=target,
        params={"script": _SCRIPT, "env": env},
        _approved=False,
    )
    return result.model_dump(mode="json")


async def _stored_requests() -> int:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest

    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.op_id == "linux.script.run")
        )
    return int(count or 0)


async def _assert_refused(env: dict[str, str], *, name: str, secret: str | None) -> None:
    before = await _stored_requests()
    dumped = await _call(env)
    assert dumped["status"] == "denied", dumped
    assert dumped["extras"]["error_code"] == "refused_before_approval"
    reason = dumped["extras"]["reason"]
    assert name in reason
    assert "secret_env" in reason
    if secret is not None:
        assert secret not in repr(dumped)
    assert await _stored_requests() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "DB_PASSWORD",
        "MYSQL_PASSWD",
        "GITHUB_TOKEN",
        "APP_SECRET",
        "API_KEY",
        "CLOUD_CREDENTIAL",
        "PROXY_AUTH",
    ],
)
async def test_env_name_with_a_secret_word_is_refused(
    linux_write_registered: None,  # noqa: F811 -- fixture
    name: str,
) -> None:
    await _assert_refused({name: "plain-value"}, name=name, secret=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("https://deploy:s3cr3t-pw@repo.example.com/app.git", "s3cr3t-pw"),
        (_JWT, _JWT),
        ("Bearer abcdefghijklmnop0123", "abcdefghijklmnop0123"),
        (_PEM, _PEM_BODY),
        ("ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2", "ghp_A1b2C3d4E5f6G7h8I9j0K1l2"),
    ],
    ids=["url-password", "jwt", "bearer", "private-key", "vendor-token"],
)
async def test_env_value_with_a_secret_shape_is_refused(
    linux_write_registered: None,  # noqa: F811 -- fixture
    value: str,
    secret: str,
) -> None:
    await _assert_refused({"APP_SOURCE": value}, name="APP_SOURCE", secret=secret)


@pytest.mark.asyncio
async def test_plain_env_value_still_waits_for_approval(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    before = await _stored_requests()
    dumped = await _call({"TAR_OPTIONS": "x"})
    assert dumped["status"] == "awaiting_approval", dumped
    assert await _stored_requests() == before + 1
