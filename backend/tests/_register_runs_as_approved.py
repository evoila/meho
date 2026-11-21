# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test fixture: a person's ``targets.register`` call runs as an approved call.

Every ``targets.register`` call waits for a person's approval
(``test_targets_register_always_waits.py`` pins that rule for each kind of
caller). A suite that checks what an executed registration stores, or how
the handler refuses bad input, uses this fixture to skip the wait for a
person's call. Agents and service logins still wait.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.targets.ops import TARGETS_REGISTER_OP_ID


@pytest.fixture
def person_register_runs_as_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.operations import _validate

    original = _validate._non_agent_park_reason

    def _reason(
        operator: Any, descriptor: Any, *, is_service: bool, decide_only: bool = False
    ) -> str | None:
        if descriptor.op_id == TARGETS_REGISTER_OP_ID and not is_service:
            return None
        return original(operator, descriptor, is_service=is_service, decide_only=decide_only)

    monkeypatch.setattr(_validate, "_non_agent_park_reason", _reason)
