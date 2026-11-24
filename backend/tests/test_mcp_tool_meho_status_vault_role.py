# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_status`` runs the Vault check only for the ``operator`` role and up.

``GET /api/v1/health`` refuses a caller below ``operator``. The
``meho_status`` MCP tool is open to every role, so it applies the same
rule inside: a ``read_only`` caller gets the whole answer, but MEHO does
not log in to Vault for it and reads no secret. The ``vault`` block then
says ``checked=false`` and why.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

import meho_backplane.api.v1.health as health_module
from meho_backplane.auth.operator import Operator, TenantRole
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_NOT_CHECKED_DETAIL = "not checked: needs the operator role"


class _Recorder:
    """Stands in for a Vault step and records each call."""

    def __init__(self, result: health_module.VaultStatus | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        if self._result is None:
            raise AssertionError("the Vault check ran for a caller below operator")
        return self._result


def _patch_vault_steps(
    monkeypatch: pytest.MonkeyPatch, result: health_module.VaultStatus | None = None
) -> dict[str, _Recorder]:
    recorders = {
        "_probe_vault_federation": _Recorder(result),
        "_probe_backend_federation": _Recorder(result),
        "dispatch": _Recorder(None),
    }
    for name, recorder in recorders.items():
        monkeypatch.setattr(health_module, name, recorder)
    return recorders


def _call_meho_status(client: TestClient) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_status", "arguments": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


@pytest.mark.parametrize("client_with_operator", [TenantRole.READ_ONLY], indirect=True)
def test_read_only_caller_gets_status_without_vault_check(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — pytest fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, op = client_with_operator
    recorders = _patch_vault_steps(monkeypatch)

    payload = _call_meho_status(client)

    assert all(not r.calls for r in recorders.values())
    assert payload["vault"] == {
        "reachable": False,
        "read_ok": False,
        "checked": False,
        "detail": _NOT_CHECKED_DETAIL,
    }
    # Every part that needs no Vault is still in the answer.
    assert payload["operator"]["sub"] == op.sub
    assert payload["operator"]["tenant_role"] == "read_only"
    assert payload["db"]["migrated"] is True
    assert payload["readiness"] is not None
    assert payload["approvals"] is not None
    assert payload["server_time"] is not None
    assert "meho_status" in payload["surface_summary"]["listed_working_tools"]


@pytest.mark.parametrize(
    "client_with_operator", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN], indirect=True
)
def test_operator_caller_gets_vault_check(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — pytest fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, op = client_with_operator
    recorders = _patch_vault_steps(
        monkeypatch, health_module.VaultStatus(reachable=True, read_ok=True, detail="version=3")
    )

    payload = _call_meho_status(client)

    vault_calls = recorders["_probe_vault_federation"].calls
    assert len(vault_calls) == 1
    assert vault_calls[0][0].sub == op.sub
    assert payload["vault"] == {
        "reachable": True,
        "read_ok": True,
        "checked": True,
        "detail": "version=3",
    }


@pytest.mark.parametrize(
    ("role", "checked"),
    [
        (TenantRole.READ_ONLY, False),
        (TenantRole.OPERATOR, True),
        (TenantRole.TENANT_ADMIN, True),
    ],
)
async def test_role_rule_holds_on_a_non_vault_backend(
    monkeypatch: pytest.MonkeyPatch,
    role: TenantRole,
    checked: bool,
) -> None:
    """The rule holds for every credential backend, not only Vault."""
    recorders = _patch_vault_steps(
        monkeypatch, health_module.VaultStatus(reachable=True, read_ok=True)
    )
    monkeypatch.setattr(
        health_module,
        "get_settings",
        lambda: SimpleNamespace(credential_backend="gsm"),
    )

    vault = await health_module._federation_status_for(build_operator(role), structlog.get_logger())

    assert len(recorders["_probe_backend_federation"].calls) == (1 if checked else 0)
    assert not recorders["_probe_vault_federation"].calls
    assert not recorders["dispatch"].calls
    assert vault.checked is checked
    if not checked:
        assert vault.detail == _NOT_CHECKED_DETAIL


def test_vault_status_defaults_to_checked() -> None:
    """An answer without the field (an older server) reads as checked."""
    status = health_module.VaultStatus.model_validate({"reachable": True, "read_ok": True})

    assert status.checked is True
