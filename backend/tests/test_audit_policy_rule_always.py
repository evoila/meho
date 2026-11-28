# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the audit row names the rule that decided the call.

Whole-path runs: a person in the web console (``POST /ui/operations/call``),
and a person's or an agent's token over ``POST /api/v1/operations/call``.
Each call goes through the real gate, and the test reads the audit row:

* the evaluator agrees with the gate: ``policy_rule`` is the evaluator's
  rule, with no ``(shadow)`` mark;
* the evaluator disagrees: ``policy_rule`` is ``gate:<verdict>``, and the
  payload key ``policy_rule_shadow`` holds the evaluator's rule with
  ``(shadow)``;
* the shadow fails: ``gate:<verdict>``, never an empty rule;
* with the switch off, the rows are as before: empty, or the evaluator's
  rule with ``(shadow)`` while ``POLICY_ENGINE_SHADOW`` is on.

Fixture values are made up.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from unittest.mock import patch

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, PermissionVerdict
from meho_backplane.main import app as main_app
from meho_backplane.op_classes import declare_op_classes
from meho_backplane.policy.evaluate import (
    Asked,
    Asker,
    Decision,
    Lookups,
    RuleRef,
    Verdict,
    evaluate,
)
from meho_backplane.policy.stamp import RuleStamp, deciding_rule, row_rule
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token, public_jwks
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as mock_jwks
from tests.test_ui_operations import (
    _TENANT_A,
    _bff_env,
    _client_with_role_and_csrf,
    _csrf_headers,
    _register_recording_gh_connector,
    _seed_destructive_op,
    _seed_gh_target,
    _seed_ingested_op,
    _seed_tenant,
    _wait_for_the_answer,
)

__all__ = ["_bff_env", "_wait_for_the_answer"]

_CONNECTOR = "gh-rest-3"
_TARGET = "gh-prod"
_CAUTION_OP = "POST:/repos/{owner}/{repo}/labels"
_CREDENTIAL_OP = "GET:/repos/{owner}/{repo}/deploy-token"
_DESTRUCTIVE_OP = "DELETE:/repos/{owner}/{repo}"
_PARAMS = {"owner": "example-org", "repo": "example-repo", "body": {}}
_EVALUATE = "meho_backplane.policy.shadow.evaluate_call"


def _switches(monkeypatch: pytest.MonkeyPatch, *, custom: bool, shadow: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if custom else "false")
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "true" if shadow else "false")
    get_settings.cache_clear()


def _seed(op_id: str = _CAUTION_OP, *, method: str = "POST", safety_level: str = "caution") -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(
        tenant_id=None,
        op_id=op_id,
        method=method,
        path=op_id.split(":", 1)[1],
        safety_level=safety_level,
    )
    _seed_gh_target(tenant_id=_TENANT_A)


def _rows() -> list[AuditLog]:
    async def _read() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            query = select(AuditLog).where(AuditLog.policy_decision.is_not(None))
            return list((await session.execute(query)).scalars().all())

    return asyncio.run(_read())


def _one_row() -> AuditLog:
    rows = _rows()
    assert len(rows) == 1, [(row.path, row.policy_rule) for row in rows]
    return rows[0]


def _request() -> ApprovalRequest:
    async def _read() -> list[ApprovalRequest]:
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(ApprovalRequest))).scalars().all())

    requests = asyncio.run(_read())
    assert len(requests) == 1
    return requests[0]


@pytest.fixture
def token_caller() -> Any:
    """Call an operation over REST with a signed token, as a real caller does."""
    key = make_rsa_keypair("kid-policy-rule-always")
    client = TestClient(main_app)

    def _call(op_id: str, *, kind: str | None = None) -> dict[str, Any]:
        claims = {"client_id": "example-agent-client"} if kind == "agent" else None
        token = mint_token(
            key,
            sub="example-agent" if kind == "agent" else "example-person",
            tenant_id=str(_TENANT_A),
            tenant_role=TenantRole.OPERATOR.value,
            principal_kind=kind,
            extra_claims=claims,
        )
        clear_jwks_cache()  # the console run of the same test signs with another key
        with respx.mock(assert_all_called=False) as mock:
            mock_jwks(mock, public_jwks(key))
            response = client.post(
                "/api/v1/operations/call",
                json={
                    "connector_id": _CONNECTOR,
                    "op_id": op_id,
                    "target": _TARGET,
                    "params": _PARAMS,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    return _call


def _console_run(op_id: str, *, preview: bool = False, body: bool = True) -> str:
    """Run *op_id* from the web console as an Operator; return the run status."""
    clear_jwks_cache()  # a token call of the same test signs with another key
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub="example-console-person", role=TenantRole.OPERATOR
    )
    form = {
        "connector_id": _CONNECTOR,
        "op_id": op_id,
        "target": _TARGET,
        "params": json.dumps(_PARAMS if body else {"owner": "example-org", "repo": "gone"}),
    }
    with mock:
        if preview:
            answer = client.post("/ui/operations/preview", headers=_csrf_headers(csrf), data=form)
            found = re.search(r'name="preview_hash" value="([0-9a-f]{64})"', answer.text)
            assert found is not None, answer.text
            form["preview_hash"] = found.group(1)
        response = client.post("/ui/operations/call", headers=_csrf_headers(csrf), data=form)
    assert response.status_code == 200, response.text
    status = re.search(r'data-run-status="([a-z_]+)"', response.text)
    assert status is not None, response.text
    return status.group(1)


# -------------------------------------------------- the switch on: rule decided


def test_a_person_in_the_web_console_runs_caution_by_the_default_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switches(monkeypatch, custom=True, shadow=False)
    _seed()

    assert _console_run(_CAUTION_OP) == "ok"

    row = _one_row()
    assert row.policy_decision == "auto-execute"
    assert row.policy_rule == "default:person.web.caution"
    assert "policy_rule_shadow" not in row.payload


def test_a_persons_token_caution_call_waits_by_the_token_floor(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=True, shadow=False)
    _seed()

    assert token_caller(_CAUTION_OP)["status"] == "awaiting_approval"

    row = _one_row()
    assert (row.path, row.policy_decision) == ("approval.request", "needs-approval")
    assert row.policy_rule == "floor:token_runs_only_safe"
    assert "policy_rule_shadow" not in row.payload
    stored = _request().policy_rule
    assert stored is not None
    assert (stored["source"], stored["rule_id"], stored["decided"]) == (
        "floor",
        "token_runs_only_safe",
        True,
    )


def test_a_destructive_call_waits_by_the_destructive_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switches(monkeypatch, custom=True, shadow=False)
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_destructive_op()
    _seed_gh_target(tenant_id=_TENANT_A)

    assert _console_run(_DESTRUCTIVE_OP, preview=True, body=False) == "awaiting_approval"

    row = _one_row()
    assert (row.path, row.policy_decision) == ("approval.request", "needs-approval")
    assert row.policy_rule == "floor:destructive_waits"


def test_an_agents_credential_read_waits_by_the_credential_floor(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=True, shadow=False)
    declare_op_classes(credential_read=[_CREDENTIAL_OP])
    _seed(_CREDENTIAL_OP, method="GET", safety_level="safe")
    # A safe read: only the credential floor makes it wait.
    assert token_caller(_CREDENTIAL_OP, kind="agent")["status"] == "awaiting_approval"

    row = _one_row()
    assert (row.path, row.policy_decision) == ("approval.request", "needs-approval")
    assert row.policy_rule == "floor:credential_waits"


# -------------------------------------------- the switch on: the gate decided


def _other_answer(**_kwargs: Any) -> Decision:
    rule = RuleRef(source="default", rule_id="person.token.caution", text="made up for the test")
    return Decision(verdict=Verdict.RUN, rule=rule)


def test_when_the_evaluator_disagrees_the_row_names_the_gate(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=True, shadow=False)
    _seed()

    with patch(_EVALUATE, side_effect=_other_answer):
        assert token_caller(_CAUTION_OP)["status"] == "awaiting_approval"

    row = _one_row()
    assert row.policy_rule == "gate:wait"
    assert row.payload["policy_rule_shadow"] == "default:person.token.caution (shadow)"
    stored = _request().policy_rule
    assert stored is not None
    assert (stored["source"], stored["rule_id"], stored["decided"]) == ("gate", "wait", True)


def test_when_the_shadow_fails_the_row_names_the_gate(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=True, shadow=False)
    _seed()

    with patch(_EVALUATE, side_effect=RuntimeError("made-up failure")):
        assert token_caller(_CAUTION_OP)["status"] == "awaiting_approval"

    row = _one_row()
    assert row.policy_rule == "gate:wait"
    assert "policy_rule_shadow" not in row.payload


# ---------------------------------------------------- the switch off: as before


def test_with_both_switches_off_no_row_names_a_rule(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=False, shadow=False)
    _seed()

    assert token_caller(_CAUTION_OP)["status"] == "awaiting_approval"
    assert _console_run(_CAUTION_OP) == "ok"

    rows = _rows()
    assert len(rows) == 2
    for row in rows:
        assert (row.policy_rule, row.policy_version) == (None, None)
        assert "policy_rule_shadow" not in row.payload
    assert _request().policy_rule is None


def test_with_only_the_shadow_switch_on_the_rule_is_marked_shadow(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=False, shadow=True)
    _seed()

    assert token_caller(_CAUTION_OP)["status"] == "awaiting_approval"
    assert _console_run(_CAUTION_OP) == "ok"

    by_decision = {row.policy_decision: row for row in _rows()}
    assert by_decision["auto-execute"].policy_rule == "default:person.web.caution (shadow)"
    assert by_decision["needs-approval"].policy_rule == "floor:token_runs_only_safe (shadow)"
    assert all("policy_rule_shadow" not in row.payload for row in by_decision.values())
    stored = _request().policy_rule
    assert stored is not None
    assert set(stored) == {"source", "rule_id", "text", "decided"}
    assert (stored["source"], stored["rule_id"], stored["decided"]) == (
        "floor",
        "token_runs_only_safe",
        False,
    )


def test_with_only_the_shadow_switch_on_the_token_floor_still_names_a_tie(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    """The floor order changes only with CUSTOM_POLICY on: the token floor names this wait."""
    _switches(monkeypatch, custom=False, shadow=True)
    declare_op_classes(credential_read=[_CREDENTIAL_OP])
    _seed(_CREDENTIAL_OP, method="GET", safety_level="safe")

    assert token_caller(_CREDENTIAL_OP, kind="agent")["status"] == "awaiting_approval"

    assert _one_row().policy_rule == "floor:token_runs_only_safe (shadow)"


def test_with_only_the_shadow_switch_on_a_failed_shadow_leaves_the_rule_empty(
    monkeypatch: pytest.MonkeyPatch, token_caller: Any
) -> None:
    _switches(monkeypatch, custom=False, shadow=True)
    _seed()

    with patch(_EVALUATE, side_effect=RuntimeError("made-up failure")):
        assert token_caller(_CAUTION_OP)["status"] == "awaiting_approval"

    assert _one_row().policy_rule is None


# ------------------------------------------------------------- the stamp alone


def _kept(verdict: str) -> RuleStamp:
    return RuleStamp(source="floor", rule_id="destructive_waits", text="t", verdict=verdict)


def test_deciding_rule_keeps_the_evaluators_rule_when_it_agrees() -> None:
    rule = deciding_rule(_kept("wait"), "wait")
    assert (rule.label(), rule.shadow) == ("floor:destructive_waits", None)


@pytest.mark.parametrize("kept", [_kept("run"), None])
def test_deciding_rule_names_the_gate_otherwise(kept: RuleStamp | None) -> None:
    rule = deciding_rule(kept, "deny")
    assert (rule.label(), rule.shadow) == ("gate:deny", kept)


@pytest.mark.parametrize(
    ("custom", "names"),
    [(False, "token_runs_only_safe"), (True, "credential_waits")],
)
def test_the_floor_order_names_a_tie_by_the_switch(
    monkeypatch: pytest.MonkeyPatch, custom: bool, names: str
) -> None:
    _switches(monkeypatch, custom=custom, shadow=False)
    roles = frozenset({TenantRole.OPERATOR.value})
    asker = Asker(kind="agent", sub="example-agent", tenant_id=_TENANT_A, roles=roles)
    asked = Asked(
        connector_id=_CONNECTOR,
        op_id=_CREDENTIAL_OP,
        risk="caution",
        op_class=frozenset({"credential_waits"}),
    )
    decision = evaluate(asker, asked, Lookups())
    assert (decision.verdict, decision.rule.rule_id) == (Verdict.WAIT, names)


def test_row_rule_leaves_the_kept_rule_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switches(monkeypatch, custom=False, shadow=True)
    kept = _kept("run")
    assert row_rule(kept, PermissionVerdict.NEEDS_APPROVAL) is kept
    assert row_rule(None, PermissionVerdict.NEEDS_APPROVAL) is None
