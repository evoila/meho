# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of ``harbor.robot.create`` shows the access the robot gets.

``harbor.robot.create`` is a ``credential_mint`` op, so MEHO hides its plain
parameter echo on the approval card. Its own preview puts the robot, the
project, the lifetime and the effective ``access`` on the card instead:

* a call without ``access`` shows the default, push and pull;
* a pull-only call shows only pull;
* the card in the console lists the access in its field table.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

import meho_backplane.connectors.harbor  # noqa: F401 -- registers the preview at import
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    has_preview_builder,
)
from tests.test_ui_approvals import (
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _render_modal,
)

_OP_ID = "harbor.robot.create"
_CONNECTOR_ID = "harbor-rest-2.x"


def _ctx(params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=Operator(
            sub="op-harbor-preview",
            name="Harbor Preview Operator",
            email=None,
            raw_jwt="op.harbor.preview.jwt",
            tenant_id=UUID("00000000-0000-0000-0000-00000000a0a4"),
            tenant_role=TenantRole.OPERATOR,
        ),
        target=None,
        params=params,
        connector_id=_CONNECTOR_ID,
    )


def test_robot_create_has_its_own_preview() -> None:
    assert has_preview_builder(_OP_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access", "expected"),
    [(None, ["push", "pull"]), (["pull"], ["pull"]), (["pull", "push"], ["push", "pull"])],
)
async def test_card_preview_shows_the_effective_access(
    access: list[str] | None, expected: list[str]
) -> None:
    params: dict[str, Any] = {"name": "puller", "project": "alpha", "duration": 90}
    if access is not None:
        params["access"] = access
    effect = await build_proposed_effect(_ctx(params))

    assert effect == {
        "op_class": "credential_mint",
        "preview": {
            "robot": "puller",
            "project": "alpha",
            "level": "project",
            "duration": 90,
            "access": expected,
        },
    }


@pytest.mark.asyncio
async def test_card_preview_marks_bad_access_as_unavailable() -> None:
    effect = await build_proposed_effect(
        _ctx({"name": "x", "project": "alpha", "duration": 90, "access": ["delete"]})
    )
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "preview" not in effect


@pytest.mark.parametrize(
    ("access", "shown", "hidden"),
    [(None, ["push", "pull"], []), (["pull"], ["pull"], ["push"])],
)
def test_approval_card_lists_the_access(
    access: list[str] | None, shown: list[str], hidden: list[str]
) -> None:
    params: dict[str, Any] = {"name": "puller", "project": "alpha", "duration": -1}
    if access is not None:
        params["access"] = access
    # The console renders through its own event loop, so this test is sync.
    effect = asyncio.run(build_proposed_effect(_ctx(params)))
    assert effect is not None

    body = _render_modal(
        {
            "op_id": _OP_ID,
            "connector_id": _CONNECTOR_ID,
            **effect,
            "preview_populated": True,
            "safety_level": "caution",
        }
    )

    table = body.split('aria-label="Proposed effect fields"')[1].split("</table>")[0]
    access_row = table.split(">access<")[1].split("</tr>")[0]
    for action in shown:
        assert f'"{action}"' in access_row
    for action in hidden:
        assert f'"{action}"' not in access_row
    assert "alpha" in table
    assert "puller" in table
