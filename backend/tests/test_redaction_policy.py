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
    PRESIDIO_DEFAULT_ENTITIES,
    PRESIDIO_SUPPORTED_ENTITIES,
    RedactionPolicy,
    RedactionPolicyError,
    RedactionRule,
    RedactionScope,
    Tier2Rule,
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


# ---------------------------------------------------------------------------
# Mode field (G11.4-T4 #1073) -- shadow / detection-only support
# ---------------------------------------------------------------------------


def test_mode_defaults_to_enforce_when_omitted() -> None:
    """A policy YAML without a ``mode:`` key parses as ``enforce``.

    Backward-compatibility contract for pre-#1073 policies; the
    full behavioural coverage lives in ``test_redaction_shadow_mode.py``.
    Captured here too because the policy schema is the canonical
    source of truth for default values.
    """
    raw = textwrap.dedent(
        """
        id: default-mode
        version: 1
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "default"
        """,
    )
    policy = parse_policy(raw)
    assert policy.mode == "enforce"


def test_mode_shadow_round_trips() -> None:
    """``mode: shadow`` parses and survives the Pydantic round trip."""
    raw = textwrap.dedent(
        """
        id: shadow-mode
        version: 1
        mode: shadow
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "shadow"
        """,
    )
    policy = parse_policy(raw)
    assert policy.mode == "shadow"


def test_unknown_mode_value_rejected() -> None:
    """An unsupported ``mode:`` value is rejected at parse time.

    The Literal union on :data:`RedactionMode` is the schema-layer
    enforcement -- a typo or stale value (e.g. ``mode: monitor``)
    must fail policy load rather than slipping through as ambient
    behaviour. The wrapping :class:`RedactionPolicyError` carries
    the field path so an operator pasting a malformed YAML sees
    which key needs fixing.
    """
    raw = textwrap.dedent(
        """
        id: bad-mode
        version: 1
        mode: monitor
        rules:
          - name: only
            pattern: bearer_token
            action: redact
            reason: "bad mode"
        """,
    )
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    assert "mode" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tier-2 (Presidio) schema -- Task #1072
# ---------------------------------------------------------------------------


def test_policy_without_tier2_has_empty_tuple() -> None:
    """A policy that omits ``tier2`` parses cleanly with an empty tuple."""
    raw = textwrap.dedent(
        """
        id: tier1-only
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "no tier2 here"
        """,
    )
    policy = parse_policy(raw)
    assert policy.tier2 == ()


def test_policy_with_minimal_tier2_rule_parses() -> None:
    """A single Tier-2 rule with default entities/threshold round-trips."""
    raw = textwrap.dedent(
        """
        id: tier2-minimal
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1 baseline"
        tier2:
          - name: scrub-error
            fields:
              - error.message
            reason: "free-text NER on error messages"
        """,
    )
    policy = parse_policy(raw)
    assert len(policy.tier2) == 1
    rule = policy.tier2[0]
    assert isinstance(rule, Tier2Rule)
    assert rule.name == "scrub-error"
    assert rule.fields == ("error.message",)
    # Default entity list comes from PRESIDIO_DEFAULT_ENTITIES.
    assert rule.entities == PRESIDIO_DEFAULT_ENTITIES
    assert rule.action == "redact"
    assert rule.threshold == 0.5
    assert rule.language == "en"


def test_policy_with_full_tier2_rule_parses() -> None:
    """Every Tier-2 field is parseable end-to-end."""
    raw = textwrap.dedent(
        """
        id: tier2-full
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1 baseline"
        tier2:
          - name: scrub-descriptions
            fields:
              - description
              - items.*.message
              - "**.error.body"
            entities:
              - PERSON
              - IP_ADDRESS
              - URL
              - EMAIL_ADDRESS
            action: mask
            threshold: 0.65
            language: en
            scope:
              connector_id: github
              op: issues.list
            reason: "scrub user-facing prose in github lists"
        """,
    )
    policy = parse_policy(raw)
    assert len(policy.tier2) == 1
    rule = policy.tier2[0]
    assert rule.fields == ("description", "items.*.message", "**.error.body")
    assert rule.entities == ("PERSON", "IP_ADDRESS", "URL", "EMAIL_ADDRESS")
    assert rule.action == "mask"
    assert rule.threshold == 0.65
    assert rule.scope.connector_id == "github"
    assert rule.scope.op == "issues.list"


def test_tier2_unknown_entity_rejected() -> None:
    """A typo'd Presidio entity name fails at parse time."""
    raw = textwrap.dedent(
        """
        id: tier2-bad-entity
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - description
            entities:
              - PERSON_NAME  # typo'd; real label is PERSON
            reason: "should fail"
        """,
    )
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    msg = str(excinfo.value)
    assert "unknown presidio entity" in msg or "PERSON_NAME" in msg
    # The known-set hint mentions at least one canonical entity.
    assert "PERSON" in msg


def test_tier2_empty_fields_rejected() -> None:
    """A Tier-2 rule with no fields would silently match nothing -- reject."""
    raw = textwrap.dedent(
        """
        id: tier2-no-fields
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields: []
            reason: "would skip everything"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_tier2_empty_entities_rejected() -> None:
    """A Tier-2 rule with no entities would do no NER -- reject."""
    raw = textwrap.dedent(
        """
        id: tier2-no-entities
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - description
            entities: []
            reason: "would do nothing"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_tier2_duplicate_entities_rejected() -> None:
    """Duplicate entities within a rule are operator error."""
    raw = textwrap.dedent(
        """
        id: tier2-dup-entity
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - description
            entities:
              - PERSON
              - PERSON
            reason: "duplicate"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_tier2_duplicate_names_rejected() -> None:
    """Two Tier-2 rules with the same name make audit entries ambiguous."""
    raw = textwrap.dedent(
        """
        id: tier2-dup-name
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - description
            reason: "first"
          - name: scrub
            fields:
              - notes
            reason: "second"
        """,
    )
    with pytest.raises(RedactionPolicyError) as excinfo:
        parse_policy(raw)
    assert "duplicate tier2 rule name" in str(excinfo.value)


def test_tier2_threshold_out_of_range_rejected() -> None:
    """Threshold must live in ``[0.0, 1.0]``."""
    raw = textwrap.dedent(
        """
        id: tier2-bad-threshold
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - description
            threshold: 1.5
            reason: "100x typo"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_tier2_extra_field_rejected() -> None:
    """``extra='forbid'`` applies to Tier-2 rules too."""
    raw = textwrap.dedent(
        """
        id: tier2-extra-field
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - description
            reason: "ok"
            severity: high  # not a tier2 field
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_tier2_blank_field_path_rejected() -> None:
    """A blank field path is operator error."""
    raw = textwrap.dedent(
        """
        id: tier2-blank-field
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: "tier1"
        tier2:
          - name: scrub
            fields:
              - "   "
            reason: "blank"
        """,
    )
    with pytest.raises(RedactionPolicyError):
        parse_policy(raw)


def test_presidio_supported_entities_includes_named_set() -> None:
    """The named set called out in the issue body (PERSON/IP/URL) is supported."""
    for entity in ("PERSON", "IP_ADDRESS", "URL"):
        assert entity in PRESIDIO_SUPPORTED_ENTITIES
