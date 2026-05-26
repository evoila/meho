# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Redaction policy schema tests (Task #1070).

Acceptance criterion: "The Pydantic policy model parses a YAML
fixture with named-pattern rules + scope; an invalid policy raises a
typed error." This file covers:

* the shipped example.yaml round-trips through
  :func:`load_policy_yaml`;
* :func:`parse_policy` accepts a multi-rule policy with scope;
* invalid policies (unknown pattern, duplicate rule names, malformed
  YAML, missing fields, extra keys) raise :class:`RedactionPolicyError`
  with the offending field in the message.
"""

from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from meho_backplane.redaction import (
    PATTERN_NAMES,
    RedactionPolicy,
    RedactionPolicyError,
    RedactionRule,
    RedactionScope,
    load_policy_yaml,
    parse_policy,
)

# ---------------------------------------------------------------------------
# Loading + parsing happy paths
# ---------------------------------------------------------------------------


def test_shipped_example_policy_loads() -> None:
    """The packaged example.yaml parses cleanly and looks well-formed."""
    policy = load_policy_yaml("meho_backplane.redaction.policies", "example.yaml")
    assert isinstance(policy, RedactionPolicy)
    assert policy.id == "default-tier1"
    assert policy.version == 1
    # The example covers every Tier-1 named pattern in the catalogue
    # at least once -- if a pattern is added without a rule, the
    # mismatch surfaces here.
    used_patterns = {rule.pattern for rule in policy.rules}
    assert used_patterns == set(PATTERN_NAMES), (
        f"example.yaml pattern coverage drifted; missing={set(PATTERN_NAMES) - used_patterns} "
        f"extra={used_patterns - set(PATTERN_NAMES)}"
    )


def test_parse_policy_accepts_minimal_policy() -> None:
    """A one-rule policy with no scope and no description is valid."""
    raw = textwrap.dedent(
        """
        id: minimal
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "smallest valid policy"
        """,
    )
    policy = parse_policy(raw)
    assert policy.id == "minimal"
    assert policy.version == 1
    assert len(policy.rules) == 1
    rule = policy.rules[0]
    assert isinstance(rule, RedactionRule)
    assert rule.scope == RedactionScope()


def test_parse_policy_accepts_scope_fields() -> None:
    """Scope fields parse into the typed model."""
    raw = textwrap.dedent(
        """
        id: scoped
        version: 1
        rules:
          - name: github-only-uuids
            pattern: uuid
            action: mask
            reason: "GitHub correlator UUIDs only"
            scope:
              connector_id: github
              op: issues.list
        """,
    )
    policy = parse_policy(raw)
    rule = policy.rules[0]
    assert rule.scope.connector_id == "github"
    assert rule.scope.op == "issues.list"
    assert rule.scope.tenant is None


# ---------------------------------------------------------------------------
# Scope.matches predicate
# ---------------------------------------------------------------------------


def test_empty_scope_matches_anything() -> None:
    scope = RedactionScope()
    assert scope.matches(connector_id="github", tenant="t1", op="any")
    assert scope.matches(connector_id=None, tenant=None, op=None)


def test_scope_with_connector_id_filters() -> None:
    scope = RedactionScope(connector_id="github")
    assert scope.matches(connector_id="github", tenant=None, op=None)
    assert not scope.matches(connector_id="kubernetes", tenant=None, op=None)
    assert not scope.matches(connector_id=None, tenant=None, op=None)


def test_scope_with_all_fields_is_and() -> None:
    scope = RedactionScope(connector_id="github", tenant="t1", op="issues.list")
    assert scope.matches(connector_id="github", tenant="t1", op="issues.list")
    assert not scope.matches(connector_id="github", tenant="t2", op="issues.list")
    assert not scope.matches(connector_id="github", tenant="t1", op="issues.create")


# ---------------------------------------------------------------------------
# Validation failure modes (all raise RedactionPolicyError)
# ---------------------------------------------------------------------------


def test_unknown_pattern_rejected_with_known_set() -> None:
    raw = textwrap.dedent(
        """
        id: bad
        version: 1
        rules:
          - name: nope
            pattern: not_a_pattern
            action: redact
            reason: "should fail at parse"
        """,
    )
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    msg = str(excinfo.value)
    assert "unknown pattern" in msg or "not_a_pattern" in msg
    # The known-set hint is in the validator output -- confirm at
    # least one canonical pattern name surfaces in the error string.
    assert "bearer_token" in msg


def test_duplicate_rule_names_rejected() -> None:
    raw = textwrap.dedent(
        """
        id: dup
        version: 1
        rules:
          - name: same-name
            pattern: bearer_token
            action: redact
            reason: "first"
          - name: same-name
            pattern: jwt
            action: redact
            reason: "second"
        """,
    )
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    assert "duplicate rule name" in str(excinfo.value)


def test_empty_rules_rejected() -> None:
    raw = textwrap.dedent(
        """
        id: empty
        version: 1
        rules: []
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_extra_top_level_key_rejected() -> None:
    """``extra='forbid'`` catches typo'd top-level keys."""
    raw = textwrap.dedent(
        """
        id: x
        version: 1
        descriptionn: "typo'd description key"
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "typo target"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_extra_rule_field_rejected() -> None:
    """``extra='forbid'`` applies inside rules too."""
    raw = textwrap.dedent(
        """
        id: x
        version: 1
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "ok"
            severity: high
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_invalid_action_rejected() -> None:
    raw = textwrap.dedent(
        """
        id: x
        version: 1
        rules:
          - name: only
            pattern: bearer_token
            action: encrypt
            reason: "encrypt is not a Tier-1 action"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_blank_reason_rejected() -> None:
    raw = textwrap.dedent(
        """
        id: x
        version: 1
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "   "
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_malformed_yaml_rejected() -> None:
    """Broken YAML surfaces as RedactionPolicyError, not yaml.YAMLError."""
    raw = "id: [\n unclosed:"
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    assert "not valid YAML" in str(excinfo.value)


def test_non_mapping_top_level_rejected() -> None:
    """A YAML list at the top level is not a policy."""
    raw = "- not a mapping"
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    assert "mapping" in str(excinfo.value)


def test_policy_is_frozen() -> None:
    """Policies are immutable; accidental mutation raises a typed error."""
    raw = textwrap.dedent(
        """
        id: frozen
        version: 1
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "ok"
        """,
    )
    policy = parse_policy(raw)
    # Frozen Pydantic models raise ValidationError (v2 surfaces it for
    # assignment to a frozen field; the wider Exception catch would mask
    # an accidental regression to a mutable model).
    with pytest.raises(ValidationError):
        policy.rules[0].name = "rebound"  # type: ignore[misc]
