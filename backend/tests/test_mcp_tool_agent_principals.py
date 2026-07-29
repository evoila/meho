# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Error-mapping tests for ``meho.agent_principals.register`` MCP tool.

The register handler (:mod:`meho_backplane.mcp.tools.agent_principals`)
mints the Keycloak client secret into Vault via
:func:`~meho_backplane.scheduler.vault_credentials.write_agent_secret`.
When ``VAULT_SCHEDULER_TOKEN`` is set but the write is denied (a
read-only token policy → Vault 403) or Vault is unreachable, the service
raises
:class:`~meho_backplane.scheduler.vault_credentials.SchedulerVaultBrokerError`.

Before #2000 the MCP handler caught
``AgentPrincipalExistsError`` / ``KeycloakAdmin*`` / ``ValueError`` but
**not** the broker error, so it bubbled to the dispatcher's catch-all and
surfaced as an opaque JSON-RPC ``-32603`` ``internal error`` — the
operator had no signal that the cause was a misscoped scheduler token.
The REST face (``POST /api/v1/agent-principals``) and the UI form handler
already mapped it to a ``502 scheduler_vault_write_error``; the MCP
handler regressed it (a backend-isolation review miss when #1489 bolted
the Vault write onto ``register``).

This file pins the contract at the **wire** level: a ``tools/call``
against ``meho.agent_principals.register`` whose Vault write raises
``SchedulerVaultBrokerError`` returns ``-32602`` (invalid params) with a
descriptive, secret-free message — mirroring
``test_api_v1_agent_principals.py``'s REST equivalent.

Since #2652 the same wire contract also carries the broker's
``token_invalid`` disposition: a Vault 403 whose ``lookup-self`` probe
also failed means the scheduler token is dead and the message must say
"invalid or expired … re-mint", while a live token denied by policy keeps
the pre-#2652 policy-scope wording verbatim.

Out of scope:

* Happy-path registration (service-layer suite covers it).
* RBAC gating (``test_mcp_tool_agent_grants.py`` pattern covers the
  list-filter + call-recheck gates generically).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.agent_principals import AgentPrincipalService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from meho_backplane.scheduler.vault_credentials import (
    SCHEDULER_VAULT_TOKEN_INVALID_DETAIL,
    SCHEDULER_VAULT_WRITE_DENIED_DETAIL,
    SchedulerVaultBrokerError,
)
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_REGISTER_TOOL = "meho.agent_principals.register"

#: A secret value the handler must never echo back in the error message.
#: write_agent_secret would receive this; if any leg leaked it into the
#: JSON-RPC error the assertion below would catch it.
_SENTINEL_SECRET = "super-secret-client-credential-value"


def _tools_call(name: str, arguments: dict[str, Any], call_id: int = 1) -> dict[str, Any]:
    """Build a JSON-RPC ``tools/call`` envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_register_vault_write_failure_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault-write failure → JSON-RPC ``-32602`` with a secret-free message.

    Monkeypatches ``AgentPrincipalService.register`` to raise
    ``SchedulerVaultBrokerError`` (the post-rollback state the real
    service reaches when the scheduler token can't write the minted
    secret), then asserts the dispatched ``tools/call`` returns
    INVALID_PARAMS (``-32602``) — **not** INTERNAL_ERROR (``-32603``) —
    with a message that names the ``VAULT_SCHEDULER_TOKEN`` remediation
    and leaks no secret.
    """
    client, _op = client_with_operator

    async def _raise_broker_error(*_args: Any, **_kwargs: Any) -> Any:
        raise SchedulerVaultBrokerError(
            "vault rejected agent-secret write at "
            "'secret/data/agents/VAULT_BOT/credentials': Forbidden"
        )

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_broker_error)

    resp = post_mcp(client, _tools_call(_REGISTER_TOOL, {"name": "vault-bot"}))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, body

    # The defect: this used to be INTERNAL_ERROR (-32603) because the
    # handler had no except for SchedulerVaultBrokerError.
    assert body["error"]["code"] == INVALID_PARAMS, body
    assert body["error"]["code"] != INTERNAL_ERROR, body

    message = body["error"]["message"]
    assert "VAULT_SCHEDULER_TOKEN" in message, message
    assert "vault write failed" in message.lower(), message
    # No secret leakage: neither the client secret nor a raw token value
    # may appear in the operator-facing error.
    assert _SENTINEL_SECRET not in message, message
    # A write denied while the token is live keeps the pre-#2652
    # policy-scope remediation, character for character.
    assert message == SCHEDULER_VAULT_WRITE_DENIED_DETAIL, message


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_register_dead_token_names_the_remint_not_the_policy(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead scheduler token gets the re-mint remediation, not the policy one.

    The broker's ``lookup-self`` probe (#2652) stamps ``token_invalid`` on
    the raised error; the MCP handler must translate that into a
    ``-32602`` naming the token re-mint. Before this the operator was
    told to widen a policy that was already correct.
    """
    client, _op = client_with_operator

    async def _raise_dead_token(*_args: Any, **_kwargs: Any) -> Any:
        raise SchedulerVaultBrokerError(
            "vault rejected agent-secret write at "
            "'secret/data/agents/VAULT_BOT/credentials': Forbidden",
            token_invalid=True,
        )

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_dead_token)

    resp = post_mcp(client, _tools_call(_REGISTER_TOOL, {"name": "vault-bot"}))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS, body

    message = body["error"]["message"]
    assert message == SCHEDULER_VAULT_TOKEN_INVALID_DETAIL, message
    assert "invalid or expired" in message, message
    assert "re-mint" in message.lower(), message
    # The misleading remediation the filing is about must be gone.
    assert "policy must grant" not in message, message
    assert _SENTINEL_SECRET not in message, message
