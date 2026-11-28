# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step operation with one step, for ``test_credential_step_always_waits.py``.

The step's op id and its declared risk level come from the call's params.
The step is sent through ``run_governed_subop`` without the approval flag.
Every request it sends is added to :data:`sent`. The dispatcher imports the
handler by its module path (``handler_ref``); closures are rejected.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.composite import run_governed_subop

CONNECTOR_ID = "vault-1.x"
COMPOSITE_OP_ID = "vault.composite.standin_one_step"
#: Declared ``credential_write`` by the test.
WRITE_STEP = "vault.standin.step.secret.write"
#: Declared ``credential_mint`` by the test.
MINT_STEP = "vault.standin.step.token.make"
#: Not declared: a plain step.
PLAIN_STEP = "vault.standin.step.config.write"

#: Every step request sent to the target, in order.
sent: list[str] = []


async def one_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Send the step ``params["step"]`` at level ``params["level"]``, without the approval flag."""
    op_id = params["step"]

    async def _send() -> dict[str, Any]:
        sent.append(op_id)
        return {"ok": True}

    step = await run_governed_subop(
        operator=operator,
        connector_id=CONNECTOR_ID,
        op_id=op_id,
        safety_level=params["level"],
        requires_approval=False,
        target=target,
        params={"path": "app"},
        call=_send,
    )
    if step.stopped is not None:
        return step.stopped
    return {"done": True}
