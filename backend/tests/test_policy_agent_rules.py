# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``agents`` part of a tenant's policy file: the pure functions.

* The checks: the switch, the install's file, the shape of a rule, the
  callers of later versions, ``rights_changes`` only with ``connectors``,
  a repeated name, and a rule above the outer limit. Each mistake names its
  place.
* The stored form, the diff, and a stored part MEHO cannot read.
* How a rule is read: a rule that names the connector wins; between equal
  rules the stricter one wins; the lowest of rule, limit and role counts.
* The per-role list of the operations that start to run at once.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from meho_backplane.policy.agent_rules import (
    AGENTS_IN_INSTALL_FILE,
    AGENTS_NEED_CUSTOM_POLICY,
    AgentRule,
    RuleOp,
    agent_rules_mistakes,
    agents_diff,
    allowed_level,
    rule_for,
    rule_lets_run,
    rules_above_limit,
    rules_from_document,
    runs_at_once_by_role,
)
from meho_backplane.policy.document import canonical_sha256, document_of, parse_policy_file
from meho_backplane.settings import get_settings

_RULE = "  - {name: helper, caller: personal_agent, roles: [Admin], up_to: dangerous}\n"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _document(agents: str) -> dict[str, Any]:
    parsed = parse_policy_file(f"policy: v1\nagents:\n{agents}".encode())
    assert parsed.model is not None, parsed.mistakes
    return document_of(parsed.model)


def _mistakes(agents: str, *, limit: str = "dangerous", scope: Any = "tenant") -> list[str]:
    return agent_rules_mistakes(_document(agents), scope=scope, custom_policy=True, limit=limit)


def _rule(name: str = "r", **kwargs: Any) -> AgentRule:
    values: dict[str, Any] = {
        "roles": frozenset({"Admin"}),
        "up_to": "dangerous",
        "connectors": frozenset(),
        "rights_changes": False,
    }
    values.update(kwargs)
    return AgentRule(name=name, **values)


def _op(
    op_id: str = "x.op",
    level: str = "caution",
    connector_id: str = "c-1",
    *,
    changes_rights: bool = False,
    keeps_waiting: bool = False,
) -> RuleOp:
    return RuleOp(connector_id, op_id, level, changes_rights, keeps_waiting)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["{example-name: example-value-7}", "[example-value-7]", "example-value-7", "null"]
)
def test_with_the_switch_off_agents_is_one_sentence_whatever_its_value(value: str) -> None:
    parsed = parse_policy_file(f"policy: v1\nagents: {value}\n".encode())
    assert parsed.model is not None
    document = document_of(parsed.model)
    for scope in ("tenant", "install"):
        mistakes = agent_rules_mistakes(document, scope=scope, custom_policy=False, limit="safe")
        assert mistakes == [AGENTS_NEED_CUSTOM_POLICY]


def test_the_install_file_never_holds_agents() -> None:
    assert _mistakes(_RULE, scope="install") == [AGENTS_IN_INSTALL_FILE]


def test_a_valid_rule_has_no_mistake_and_is_stored_with_only_the_keys_set() -> None:
    document = _document(
        "  - name: helper\n    caller: personal_agent\n    roles: [Admin, Operator]\n"
        "    up_to: caution\n    connectors: [c-1]\n    rights_changes: allow\n"
    )
    assert _mistakes(_RULE) == []
    assert document["agents"] == [
        {
            "name": "helper",
            "caller": "personal_agent",
            "roles": ["Admin", "Operator"],
            "up_to": "caution",
            "connectors": ["c-1"],
            "rights_changes": "allow",
        }
    ]
    assert _document(_RULE)["agents"][0].keys() == {"name", "caller", "roles", "up_to"}


@pytest.mark.parametrize("caller", ["person", "service_agent"])
def test_a_caller_of_a_later_version_is_not_in_this_version(caller: str) -> None:
    (mistake,) = _mistakes(f"  - {{name: r, caller: {caller}, roles: [Admin], up_to: safe}}\n")
    assert mistake.startswith(f"`agents[0].caller` is `{caller}`, which is not in this version")


@pytest.mark.parametrize(
    ("rule", "mistake"),
    [
        (
            "{name: r, caller: robot, roles: [Admin], up_to: safe}",
            "`agents[0].caller` is not a kind of caller; use personal_agent.",
        ),
        (
            "{name: r, caller: personal_agent, roles: [Owner], up_to: safe}",
            "`agents[0].roles`: names a role that does not exist; use Reader, Operator, "
            "Admin (a platform admin counts as Admin).",
        ),
        (
            "{name: r, caller: personal_agent, roles: [Admin, Admin], up_to: safe}",
            "`agents[0].roles`: names a role twice.",
        ),
        (
            "{name: r, caller: personal_agent, roles: [Admin], up_to: destructive}",
            "`agents[0].up_to` is not a level a rule can allow; use safe, caution or "
            "dangerous. A destructive operation always waits.",
        ),
        (
            "{name: r, caller: personal_agent, roles: [Admin], up_to: safe, rights_changes: allow}",
            "`agents[0]`: `rights_changes` needs `connectors`: a rule without connectors "
            "never allows changes of rights.",
        ),
        (
            "{name: r, caller: personal_agent, roles: [Admin], up_to: safe, "
            "connectors: [c-1], rights_changes: yes-please}",
            "`agents[0].rights_changes` is not a value it can have; use allow, or leave "
            "the key out.",
        ),
        (
            "{name: Not A Name, caller: personal_agent, roles: [Admin], up_to: safe}",
            "`agents[0].name` is not a rule name: use 1 to 63 lower case letters, digits, "
            "- and _, starting with a letter.",
        ),
        (
            "{name: r, caller: personal_agent, roles: [Admin], up_to: safe, colour: red}",
            "`agents[0].colour` is not a known key.",
        ),
    ],
)
def test_a_wrong_rule_is_one_sentence_with_its_place(rule: str, mistake: str) -> None:
    assert _mistakes(f"  - {rule}\n") == [mistake]


def test_a_part_that_is_not_a_list_and_a_repeated_name_are_mistakes() -> None:
    parsed = parse_policy_file(b"policy: v1\nagents: {name: r}\n")
    assert parsed.model is not None
    document = document_of(parsed.model)
    assert agent_rules_mistakes(document, scope="tenant", custom_policy=True, limit="safe") == [
        "`agents` must be a list of rules."
    ]
    assert _mistakes(_RULE + _RULE) == [
        "`agents[1].name` repeats `agents[0]`; give each rule its own name."
    ]


@pytest.mark.parametrize("form", ["agents: []", "agents: null", "agents:"])
def test_an_empty_part_keeps_the_key_and_means_no_rule(form: str) -> None:
    parsed = parse_policy_file(f"policy: v1\n{form}\n".encode())
    assert parsed.model is not None
    document = document_of(parsed.model)
    assert document["agents"] == []
    assert agent_rules_mistakes(document, scope="tenant", custom_policy=True, limit="safe") == []
    assert canonical_sha256(document) == canonical_sha256({"classifications": []})


@pytest.mark.parametrize(
    ("limit", "count"), [("safe", 2), ("caution", 1), ("dangerous", 0)], ids=str
)
def test_a_rule_above_the_outer_limit_is_refused_with_its_place(limit: str, count: int) -> None:
    agents = (
        "  - {name: low, caller: personal_agent, roles: [Reader], up_to: safe}\n"
        "  - {name: mid, caller: personal_agent, roles: [Operator], up_to: caution}\n"
        "  - {name: high, caller: personal_agent, roles: [Admin], up_to: dangerous}\n"
    )
    mistakes = _mistakes(agents, limit=limit)
    assert len(mistakes) == count
    if count:
        assert mistakes[-1] == (
            "`agents[2].up_to` is above the outer limit for personal agents: the rule `high` "
            f"allows dangerous, the limit is {limit}. Only a platform admin raises the limit, "
            "in the install's policy file."
        )


def test_the_order_of_the_rules_does_not_count() -> None:
    two = "  - {name: b, caller: personal_agent, roles: [Reader], up_to: safe}\n"
    assert canonical_sha256(_document(_RULE + two)) == canonical_sha256(_document(two + _RULE))


# ---------------------------------------------------------------------------
# Stored rules, the diff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "example-value-7",
        ["example-value-7"],
        [{"name": "r", "caller": "person", "roles": ["Admin"], "up_to": "safe"}],
        [{"name": "r", "caller": "personal_agent", "roles": ["Admin"], "up_to": "destructive"}],
    ],
)
def test_a_stored_part_that_cannot_be_read_gives_no_rule(stored: Any) -> None:
    with structlog.testing.capture_logs() as logs:
        rules = rules_from_document({"agents": stored})
    assert rules == []
    (warning,) = [e for e in logs if e["event"] == "policy_agent_rules_not_readable"]
    assert warning["place"] == "tenant:agents"
    assert "example-value-7" not in str(logs)
    assert rules_above_limit({"agents": stored}, "safe", place="tenant t:agents") == [
        "The stored rules at tenant t:agents are not valid, so no rule counts."
    ]


def test_no_stored_part_gives_no_rule_without_a_warning() -> None:
    with structlog.testing.capture_logs() as logs:
        assert rules_from_document({"classifications": []}) == []
        assert rules_from_document(None) == []
    assert logs == []


def test_the_diff_shows_added_changed_and_removed_rules_by_name() -> None:
    old = {"agents": [{"name": "a", "up_to": "safe"}, {"name": "b", "up_to": "safe"}]}
    new = {"agents": [{"name": "a", "up_to": "caution"}, {"name": "c", "up_to": "safe"}]}
    assert agents_diff(old, new) == {
        "agents": {
            "added": [{"entry": {"name": "c", "up_to": "safe"}}],
            "changed": [{"before": {"name": "a", "up_to": "safe"}, "after": new["agents"][0]}],
            "removed": [{"entry": {"name": "b", "up_to": "safe"}}],
        }
    }
    assert agents_diff({}, {"agents": []}) == {}
    assert agents_diff(None, {}) == {}


# ---------------------------------------------------------------------------
# How a rule is read
# ---------------------------------------------------------------------------


def test_a_rule_that_names_the_connector_wins() -> None:
    wide = _rule("wide", up_to="safe")
    named = _rule("named", up_to="dangerous", connectors=frozenset({"c-1"}))
    assert rule_for([wide, named], "Admin", "c-1") == named
    assert rule_for([wide, named], "Admin", "c-2") == wide
    assert rule_for([named], "Admin", "c-2") is None
    assert rule_for([wide], "Reader", "c-1") is None


def test_between_equal_rules_the_stricter_one_wins() -> None:
    low = _rule("low", up_to="caution", connectors=frozenset({"c-1"}))
    high = _rule("high", up_to="dangerous", connectors=frozenset({"c-1"}))
    rights = _rule("rights", up_to="caution", connectors=frozenset({"c-1"}), rights_changes=True)
    assert rule_for([high, low], "Admin", "c-1") == low
    assert rule_for([rights, low], "Admin", "c-1") == low
    assert rule_for([rights, high], "Admin", "c-1") == rights


@pytest.mark.parametrize(
    ("role", "up_to", "limit", "expected"),
    [
        ("Admin", "dangerous", "dangerous", "dangerous"),
        ("Operator", "dangerous", "dangerous", "caution"),
        ("Reader", "dangerous", "dangerous", "safe"),
        ("Admin", "dangerous", "caution", "caution"),
        ("Admin", "safe", "dangerous", "safe"),
    ],
)
def test_the_lowest_of_rule_limit_and_role_counts(
    role: str, up_to: str, limit: str, expected: str
) -> None:
    rule = _rule(roles=frozenset({role}), up_to=up_to)
    assert allowed_level(rule, role, limit) == expected


def test_a_change_of_rights_runs_only_where_the_rule_names_its_connector() -> None:
    plain = _rule("plain", connectors=frozenset({"c-1"}))
    allowing = _rule("allowing", connectors=frozenset({"c-1"}), rights_changes=True)
    op = _op(level="caution", changes_rights=True)
    assert not rule_lets_run(plain, "Admin", "dangerous", op)
    assert rule_lets_run(allowing, "Admin", "dangerous", op)
    assert not rule_lets_run(
        allowing, "Admin", "dangerous", _op(connector_id="c-2", changes_rights=True)
    )
    assert not rule_lets_run(allowing, "Admin", "dangerous", _op(keeps_waiting=True))
    assert not rule_lets_run(allowing, "Admin", "dangerous", _op(level="destructive"))


# ---------------------------------------------------------------------------
# The per-role list
# ---------------------------------------------------------------------------


def test_the_list_names_for_each_role_the_operations_that_start_to_run_at_once() -> None:
    rules = [_rule("all", roles=frozenset({"Reader", "Operator", "Admin"}), up_to="dangerous")]
    ops = [
        _op("x.read", "safe"),
        _op("x.restart", "caution"),
        _op("x.resize", "dangerous"),
        _op("x.erase", "dangerous", keeps_waiting=True),
        _op("x.erase-all", "destructive"),
    ]
    listed = runs_at_once_by_role(rules, limit="dangerous", ops=ops)
    ids = {role: [item["op_id"] for item in items] for role, items in listed.items()}
    assert ids == {"Reader": [], "Operator": ["x.restart"], "Admin": ["x.resize", "x.restart"]}
    assert listed["Operator"] == [
        {"connector_id": "c-1", "op_id": "x.restart", "risk": "caution", "rule": "tenant_rule:all"}
    ]
    admin_only = RuleOp("c-1", "x.register", "caution", False, False, lowest_role="Admin")
    only = runs_at_once_by_role(rules, limit="dangerous", ops=[admin_only])
    assert {role: len(items) for role, items in only.items()} == {
        "Reader": 0,
        "Operator": 0,
        "Admin": 1,
    }
    capped = runs_at_once_by_role(rules, limit="safe", ops=ops)
    assert capped == {"Reader": [], "Operator": [], "Admin": []}


def test_the_plan_reads_the_role_floor_of_an_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.auth.operator import TenantRole
    from meho_backplane.operations import role_floors
    from meho_backplane.policy.agent_rules_plan import _lowest_role

    monkeypatch.setitem(role_floors._FLOORS, "example.register", TenantRole.TENANT_ADMIN)
    monkeypatch.setitem(role_floors._FLOORS, "example.restart", TenantRole.OPERATOR)
    assert _lowest_role("example.register") == "Admin"
    assert _lowest_role("example.restart") == "Operator"
    assert _lowest_role("example.list") == "Reader"
