# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op-level params check the dispatcher runs after the schema check.

A connector registers a check for an op (``register_params_check``) when
its JSON schema cannot say a rule, for example "a secret field must be a
locker reference". The check runs on every call, before the policy gate,
so a refused call never waits for approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.operations._validate import (
    _PARAMS_CHECKS,
    params_check_errors,
    params_problem,
    register_params_check,
)

_OP = "test.params.check"


@pytest.fixture(autouse=True)
def _forget_the_test_check() -> Iterator[None]:
    yield
    _PARAMS_CHECKS.pop(_OP, None)


def _no_clear_token(params: dict[str, Any]) -> dict[str, Any] | None:
    if str(params.get("token", "")).startswith("ref:"):
        return None
    return params_problem("token", "a reference (ref:...)", "Give the reference, not the value.")


def test_an_op_without_a_check_passes() -> None:
    assert params_check_errors("test.no.check", {"anything": 1}) == []


def test_a_registered_check_refuses_with_one_entry() -> None:
    register_params_check(_OP, _no_clear_token)
    assert params_check_errors(_OP, {"token": "ref:abc"}) == []
    errors = params_check_errors(_OP, {"token": "plain-value"})
    assert errors == [
        {
            "path": "token",
            "validator": "params_check",
            "expected": "a reference (ref:...)",
            "remediation": "Give the reference, not the value.",
        }
    ]


def test_the_problem_entry_keeps_the_path_of_the_field() -> None:
    entry = params_problem("spec.infrastructure.properties.defaultPassword", "a reference", "fix")
    assert entry["path"] == "spec.infrastructure.properties.defaultPassword"
    assert entry["validator"] == "params_check"
