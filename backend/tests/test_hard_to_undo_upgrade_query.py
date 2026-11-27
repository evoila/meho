# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The query of the upgrade row lists exactly the grants the switch stops.

The query in ``docs/upgrade-rows/v0.49.0/hard-to-undo-no-standing-grant.md``
lists a standing grant when it is live (not revoked, not expired), its
connector id names the stored row the way a call reads it, and the row a
call of the grant's tenant uses has ``hard_to_undo``. Each case also makes
the direct call of the grant over REST, with ``CUSTOM_POLICY`` off and on.
Both directions hold: every grant whose call runs with the switch off and
waits with it on is listed; a listed grant never lets the call run with the
switch on; a grant that is not listed gives the same answer with the switch
off and on. A listed grant can also let no call run with the switch off,
when its operation is turned off for the tenant or is ``destructive``.

The connectors, the operations and the target are made up.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import inspect, select, text

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    ServicePrincipalGrant,
    TenantOperationSetting,
)
from tests._hard_to_undo_helpers import (
    RUNS,
    SERVICE,
    TENANT,
    add_connector,
    call,
    custom_policy,
    finish,
    register_op,
    seed_target,
    service_client,
    start,
)
from tests._sandbox_mode_helpers import grant_use_rows, seed_grant_row

_UPGRADE_ROW = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "upgrade-rows"
    / "v0.49.0"
    / "hard-to-undo-no-standing-grant.md"
)
_COWAPI = "cowapi-1.x"
_COWLITE = "cowlite"
_TARGET = "cow-box-01"
_LITE_TARGET = "cow-lite-01"
_HALT = "cowapi.box.halt"
_RESET = "cowapi.box.reset"
_LITE_HALT = "cowlite.box.halt"
_COWOFF = "cowoff-1.x"
_OFF_HALT = "cowoff.box.halt"
_OFF_TARGET = "cow-off-01"
_COWTOP = "cowtop-1.x"
_TOP_HALT = "cowtop.box.halt"
_TOP_TARGET = "cow-top-01"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    add_connector("cowapi", "1.x", "cowapi")
    add_connector("cowlite", "", "")
    add_connector("cowoff", "1.x", "cowoff")
    add_connector("cowtop", "1.x", "cowtop")
    yield
    finish()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with service_client() as c:
        yield c


def _query() -> str:
    action = _UPGRADE_ROW.read_text(encoding="utf-8").split("## Action", 1)[1]
    match = re.search(r"`(SELECT [^`]+)`", action)
    assert match is not None, "the upgrade row has no query"
    return match.group(1).replace("\\|", "|")


async def _listed() -> list[tuple[str, str]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(text(_query()))).all()
    return [(row.connector_id, row.op_id) for row in rows]


async def _grant(
    connector_id: str,
    op_id: str,
    *,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> uuid.UUID:
    return await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=op_id,
        connector_id=connector_id,
        target_product=connector_id.split("-", 1)[0],
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


async def _tenant_copy(op_id: str, hard_to_undo: str | None) -> None:
    """Give the tenant its own copy of the built-in row, with its own ``hard_to_undo``."""
    async with get_sessionmaker()() as session:
        built_in = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == op_id, EndpointDescriptor.tenant_id.is_(None)
                )
            )
        ).scalar_one()
        values = {
            attr.key: getattr(built_in, attr.key)
            for attr in inspect(EndpointDescriptor).column_attrs
        }
        values.update(id=uuid.uuid4(), tenant_id=TENANT, hard_to_undo=hard_to_undo)
        session.add(EndpointDescriptor(**values))
        await session.commit()


async def _turn_off_for_tenant(op_id: str, *, product: str, version: str, impl_id: str) -> None:
    """The tenant turns the built-in operation off for itself."""
    async with get_sessionmaker()() as session:
        session.add(
            TenantOperationSetting(
                tenant_id=TENANT,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                enabled=False,
                set_by_sub="tenant-admin",
                set_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _calls(
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[httpx.AsyncClient, Any],
    *,
    grant_id: uuid.UUID,
    connector_id: str,
    op_id: str,
    target: str,
) -> tuple[str, str]:
    """Call the operation with the switch off, then on: did it run, wait or get refused?"""
    answers: list[str] = []
    for on in (False, True):
        custom_policy(monkeypatch, on=on)
        runs_before = len(RUNS)
        uses_before = len(await grant_use_rows(grant_id))
        result = await call(client, connector_id=connector_id, op_id=op_id, target=target)
        if result["status"] == "ok":
            assert len(RUNS) == runs_before + 1
            assert len(await grant_use_rows(grant_id)) == uses_before + 1
            answers.append("runs")
        else:
            assert len(RUNS) == runs_before, result
            assert len(await grant_use_rows(grant_id)) == uses_before
            if result["status"] == "awaiting_approval":
                answers.append("waits")
            else:
                code = (result.get("extras") or {}).get("error_code")
                answers.append(f"{result['status']}:{code}")
    return answers[0], answers[1]


async def test_case_7_a_live_grant_of_a_hard_to_undo_op_is_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_HALT, "stops", product="cowapi", version="1.x", impl_id="cowapi")
    await seed_target(_TARGET, product="cowapi", version="1.x")
    grant_id = await _grant(_COWAPI, _HALT)

    assert await _listed() == [(_COWAPI, _HALT)]
    off, on = await _calls(
        monkeypatch, client, grant_id=grant_id, connector_id=_COWAPI, op_id=_HALT, target=_TARGET
    )
    assert (off, on) == ("runs", "waits")


async def test_case_8_a_grant_on_a_connector_without_a_version_is_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_LITE_HALT, "stops", product="cowlite", version="", impl_id="")
    await seed_target(_LITE_TARGET, product="cowlite", version="")
    grant_id = await _grant(_COWLITE, _LITE_HALT)

    assert await _listed() == [(_COWLITE, _LITE_HALT)]
    off, on = await _calls(
        monkeypatch,
        client,
        grant_id=grant_id,
        connector_id=_COWLITE,
        op_id=_LITE_HALT,
        target=_LITE_TARGET,
    )
    assert (off, on) == ("runs", "waits")


async def test_case_9_an_expired_grant_is_not_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_HALT, "stops", product="cowapi", version="1.x", impl_id="cowapi")
    await seed_target(_TARGET, product="cowapi", version="1.x")
    grant_id = await _grant(_COWAPI, _HALT, expires_at=datetime.now(UTC) - timedelta(days=1))

    assert await _listed() == []
    off, on = await _calls(
        monkeypatch, client, grant_id=grant_id, connector_id=_COWAPI, op_id=_HALT, target=_TARGET
    )
    assert (off, on) == ("waits", "waits")


async def test_case_10_a_revoked_grant_is_not_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_HALT, "stops", product="cowapi", version="1.x", impl_id="cowapi")
    await seed_target(_TARGET, product="cowapi", version="1.x")
    grant_id = await _grant(_COWAPI, _HALT, revoked_at=datetime.now(UTC))

    assert await _listed() == []
    off, on = await _calls(
        monkeypatch, client, grant_id=grant_id, connector_id=_COWAPI, op_id=_HALT, target=_TARGET
    )
    assert (off, on) == ("waits", "waits")


async def test_case_11_a_tenant_copy_without_the_field_is_not_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_RESET, "overwrites", product="cowapi", version="1.x", impl_id="cowapi")
    await _tenant_copy(_RESET, None)
    await seed_target(_TARGET, product="cowapi", version="1.x")
    grant_id = await _grant(_COWAPI, _RESET)

    assert await _listed() == []
    off, on = await _calls(
        monkeypatch, client, grant_id=grant_id, connector_id=_COWAPI, op_id=_RESET, target=_TARGET
    )
    assert (off, on) == ("runs", "runs")


async def test_case_12_a_tenant_copy_with_the_field_is_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_RESET, None, product="cowapi", version="1.x", impl_id="cowapi")
    await _tenant_copy(_RESET, "deletes")
    await seed_target(_TARGET, product="cowapi", version="1.x")
    grant_id = await _grant(_COWAPI, _RESET)

    assert await _listed() == [(_COWAPI, _RESET)]
    off, on = await _calls(
        monkeypatch, client, grant_id=grant_id, connector_id=_COWAPI, op_id=_RESET, target=_TARGET
    )
    assert (off, on) == ("runs", "waits")


async def test_case_18_a_grant_of_an_op_turned_off_for_the_tenant_is_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(_OFF_HALT, "stops", product="cowoff", version="1.x", impl_id="cowoff")
    await _turn_off_for_tenant(_OFF_HALT, product="cowoff", version="1.x", impl_id="cowoff")
    await seed_target(_OFF_TARGET, product="cowoff", version="1.x")
    grant_id = await _grant(_COWOFF, _OFF_HALT)

    assert await _listed() == [(_COWOFF, _OFF_HALT)]
    off, on = await _calls(
        monkeypatch,
        client,
        grant_id=grant_id,
        connector_id=_COWOFF,
        op_id=_OFF_HALT,
        target=_OFF_TARGET,
    )
    assert (off, on) == ("error:unknown_op", "error:unknown_op")


async def test_case_19_a_grant_of_a_destructive_op_is_listed(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await register_op(
        _TOP_HALT,
        "stops",
        product="cowtop",
        version="1.x",
        impl_id="cowtop",
        safety_level="destructive",
    )
    await seed_target(_TOP_TARGET, product="cowtop", version="1.x")
    grant_id = await _grant(_COWTOP, _TOP_HALT)

    assert await _listed() == [(_COWTOP, _TOP_HALT)]
    off, on = await _calls(
        monkeypatch,
        client,
        grant_id=grant_id,
        connector_id=_COWTOP,
        op_id=_TOP_HALT,
        target=_TOP_TARGET,
    )
    # No grant covers a destructive call, so it never runs: without a preview first it is refused.
    assert (off, on) == ("denied:preview_binding_required", "denied:preview_binding_required")
