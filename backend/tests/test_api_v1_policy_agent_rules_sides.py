# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The import plan reads the stored rules with the stored classifications.

``runs_at_once`` lists, for each role, each operation that waits under the
tenant's stored policy (its rules and its classifications) and runs at
once under the file (its rules and its classifications). So an import
that drops an "always wait" lists the operation, also when the rules stay
the same. An edit that changes neither the rules nor what waits lists
none.

Each case asks for the plan with a tenant admin's token
(``POST /api/v1/policy/import?preview=true``), then asks for the import,
and reads the approval card in the web console
(``GET /ui/approvals/{request_id}``). ``CUSTOM_POLICY`` is on and the
install's outer limit is ``dangerous``. ``inslim.vm.restart`` is
``caution`` and ``inslim.vm.list`` is ``safe``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.policy.document import document_of, parse_policy_file

from . import _agent_rules_helpers as a
from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    switch_on_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_KEYPAIR = make_rsa_keypair("kid-agents-sides")

_WAITS = "classifications:\n  - {match: {op: inslim.vm.restart}, always_wait: true}\n"
_NO_ENTRY = "classifications: []\n"


def _file(classifications: str, up_to: str, description: str = "") -> str:
    head = f"policy: v1\n{description}"
    rule = (
        "agents:\n  - name: helper\n    caller: personal_agent\n"
        f"    roles: [Admin]\n    up_to: {up_to}\n"
    )
    return head + classifications + rule


async def _store_tenant(content: str) -> None:
    parsed = parse_policy_file(content.encode())
    assert parsed.model is not None, parsed.mistakes
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TenantPolicy(tenant_id=h.TENANT, document=document_of(parsed.model), set_by_sub="fx")
        )


_CASES = [
    pytest.param(_file(_WAITS, "caution"), _file(_NO_ENTRY, "dangerous"), True, id="1-wider-rule"),
    pytest.param(_file(_WAITS, "caution"), _file(_NO_ENTRY, "caution"), True, id="2-same-rule"),
    pytest.param(_file(_NO_ENTRY, "caution"), _file(_WAITS, "caution"), False, id="3-adds-wait"),
    pytest.param(
        _file(_NO_ENTRY, "caution"),
        _file(_NO_ENTRY, "caution", "description: another text\n"),
        False,
        id="4-description",
    ),
]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(("stored", "content", "listed"), _CASES)
async def test_the_plan_lists_what_waits_under_the_stored_policy_and_runs_under_the_file(
    client: TestClient, embedding: AsyncMock, stored: str, content: str, listed: bool
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _store_tenant(stored)
    bearer = a.tenant_admin(_KEYPAIR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        preview = h.post(client, "import", bearer, content, preview="true")
        asked = h.post(client, "import", bearer, content)
        assert asked.status_code == 202, asked.text
        request_id = asked.json()["approval_request_id"]
        page = await a.console_get(client, _KEYPAIR, f"/ui/approvals/{request_id}", sub="rev-b")

    assert preview.status_code == 200, preview.text
    lists: dict[str, Any] = preview.json()["diff"]["runs_at_once"]
    expected = ["inslim.vm.restart"] if listed else []
    assert {role: a.inslim(items) for role, items in lists.items()} == {
        "Reader": [],
        "Operator": [],
        "Admin": expected,
    }
    (request,) = await h.pending_requests()
    assert request.proposed_effect["preview"]["diff"]["runs_at_once"] == lists
    assert page.status_code == 200, page.text
    assert ("tenant_rule:helper" in page.text) is listed
