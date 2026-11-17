# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval card preview for ``harbor.robot.create``.

``harbor.robot.create`` makes a new robot account with a new secret, so it is
a ``credential_mint`` op. For such an op MEHO hides the call's parameters on
the approval card, unless the op has its own preview. This module is that
preview. It shows the person who approves:

* the robot name, the project and the level (always ``project``),
* how long the robot lives (``duration``, ``-1`` means it never expires),
* ``access``: the repository actions the robot gets. This is the value the
  handler sends to Harbor, so a call without ``access`` shows the default,
  push and pull.
* ``secret_goes_to``: where the new secret goes. With ``vault_target`` it
  names the Vault mount, path and field, and says that an existing value in
  that field is replaced. Without it, it says the secret is returned in the
  answer.

The preview uses only the call's parameters. It makes no Harbor call and
holds no secret: the secret exists only in Harbor's answer after the approval.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.harbor.connector import robot_access_actions
from meho_backplane.connectors.harbor.ops_robot_vault import secret_destination
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

_CREATE_OP_ID = "harbor.robot.create"


async def _harbor_robot_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the robot, its project, its lifetime, its access and where its secret goes."""
    return {
        "robot": ctx.params.get("name"),
        "project": ctx.params.get("project"),
        "level": "project",
        "duration": ctx.params.get("duration"),
        "access": robot_access_actions(ctx.params),
        "secret_goes_to": secret_destination(ctx.params),
    }


register_preview_builder(_CREATE_OP_ID, _harbor_robot_create_preview)
