# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A call that is a change because of its params, at the console call form.

The same rule and the same two stand-in operations as
``test_reader_change_by_params.py``: a ``safe`` read whose connector marks a
call with the key ``secret`` in its params as a credential call, and a
write at ``caution`` with no approval flag. The ``params`` field of the form
is JSON text. Text that is not JSON, a value that is not a JSON object and
JSON nested too deep for the parser are params the role check cannot read,
so only what does not depend on the params decides. MEHO stores nothing
and runs nothing for a Reader. An Operator keeps its answers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane import op_classes
from meho_backplane.auth.operator import TenantRole
from meho_backplane.op_classes import declare_credential_check
from tests.test_reader_change_by_params import PARAMS_READ, PARAMS_WRITE, secret_in_params
from tests.test_reader_change_refused_console import (
    _BODY,
    _PLAIN,
    _approval_requests,
    _nested,
    reader,  # noqa: F401 - pytest fixture
)
from tests.test_ui_operations import (
    _TENANT_A,
    _bff_env,
    _client_with_role_and_csrf,
    _csrf_headers,
    _patch_call_operation,
    _seed_group_and_op,
    _seed_tenant,
    _wait_for_the_answer,
)

__all__ = ["_bff_env", "_wait_for_the_answer"]

_SECRET = json.dumps({"path": "x", "secret": "made-up"})


@pytest.fixture
def stand_ins() -> Iterator[None]:
    """The two stand-in operations, looked up by the call form."""
    declare_credential_check(PARAMS_READ, secret_in_params)
    for op_id, level in ((PARAMS_READ, "safe"), (PARAMS_WRITE, "caution")):
        _seed_group_and_op(
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            group_key=f"group-{op_id}",
            group_name=f"Group {op_id}",
            op_id=op_id,
            safety_level=level,
            requires_approval=False,
        )
    yield
    op_classes._CREDENTIAL_CHECKS.pop(PARAMS_READ, None)


def _form(op_id: str, params: str) -> dict[str, str]:
    return {"connector_id": "vault-1.x", "op_id": op_id, "target": "", "params": params}


#: (op_id, ``params`` text) -> True when a Reader gets the code and the
#: sentence, False when the Reader gets the plain ``operator_required``.
_READER_CASES: dict[str, tuple[str, str, bool]] = {
    "read with secret": (PARAMS_READ, _SECRET, True),
    "read with no params": (PARAMS_READ, "{}", False),
    "read with a path": (PARAMS_READ, '{"path": "x"}', False),
    "read with broken JSON": (PARAMS_READ, "{", False),
    "read with a list": (PARAMS_READ, '["secret"]', False),
    "read with a string": (PARAMS_READ, '"secret"', False),
    "read with a number": (PARAMS_READ, "7", False),
    "read nested 100,000 levels": (PARAMS_READ, _nested(100_000), False),
    "write with broken JSON": (PARAMS_WRITE, "{", True),
    "write with a list": (PARAMS_WRITE, '["secret"]', True),
    "write with a string": (PARAMS_WRITE, '"secret"', True),
    "write with a number": (PARAMS_WRITE, "7", True),
    "write nested 100,000 levels": (PARAMS_WRITE, _nested(100_000), True),
}


@pytest.mark.parametrize("case", list(_READER_CASES))
@pytest.mark.usefixtures("stand_ins")
def test_a_reader_console_call_is_a_change_by_its_params_as_at_the_gate(
    monkeypatch: pytest.MonkeyPatch,
    reader: tuple[TestClient, str],  # noqa: F811
    case: str,
) -> None:
    client, csrf = reader
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})
    op_id, params, change = _READER_CASES[case]

    response = client.post(
        "/ui/operations/call", headers=_csrf_headers(csrf), data=_form(op_id, params)
    )

    assert (response.status_code, response.json()) == (403, _BODY if change else _PLAIN)
    assert received == []
    assert _approval_requests() == 0


#: (op_id, ``params`` text) -> (status, the answer holds "Cannot run",
#: calls that reached the stand-in). An Operator's answers.
_OPERATOR_CASES: dict[str, tuple[str, str, tuple[int, bool, int]]] = {
    "write with no params": (PARAMS_WRITE, "{}", (200, False, 1)),
    "write with secret": (PARAMS_WRITE, _SECRET, (200, False, 1)),
    "read with secret": (PARAMS_READ, _SECRET, (200, False, 1)),
    "read with broken JSON": (PARAMS_READ, "{", (400, True, 0)),
    "write with a list": (PARAMS_WRITE, '["secret"]', (400, True, 0)),
    "write with a string": (PARAMS_WRITE, '"secret"', (400, True, 0)),
    "write with a number": (PARAMS_WRITE, "7", (400, True, 0)),
    "write nested 100,000 levels": (PARAMS_WRITE, _nested(100_000), (422, False, 0)),
}


@pytest.mark.parametrize("case", list(_OPERATOR_CASES))
@pytest.mark.usefixtures("_wait_for_the_answer", "stand_ins")
def test_an_operator_console_call_with_the_same_params_keeps_its_answer(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})
    op_id, params, expected = _OPERATOR_CASES[case]
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub="operator-person", role=TenantRole.OPERATOR
    )

    with mock:
        response = client.post(
            "/ui/operations/call", headers=_csrf_headers(csrf), data=_form(op_id, params)
        )

    answer = (response.status_code, "Cannot run" in response.text, len(received))
    assert answer == expected, response.text[:300]
    assert _approval_requests() == 0
