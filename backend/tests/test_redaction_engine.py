# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Redaction engine integration tests (Task #1070).

Acceptance criteria covered:

* "The engine returns ``(redacted, manifest)`` for a nested dict/list
  payload; manifest entries carry type/count/span/reason."
* The engine is pure: identical inputs produce identical outputs;
  no I/O; no globals mutated.

Each test names exactly which acceptance bit it pins; the file is
the load-bearing proof of the C1-a deliverable's behavioural contract.
"""

from __future__ import annotations

import textwrap
import time

import pytest

from meho_backplane.redaction import (
    RedactionManifestEntry,
    RedactionPolicy,
    RedactionResult,
    parse_policy,
    redact,
)


def _policy_with_rules(*rules_yaml: str) -> RedactionPolicy:
    """Build a one-off policy from a list of rule YAML fragments.

    Keeps test bodies focused on the assertion rather than
    boilerplate. The YAML preamble is fixed; rules are spliced in,
    each indented two spaces so they nest under the ``rules:`` key.
    """
    # textwrap.dedent unindents the f-string's leading whitespace; we
    # build the YAML with no leading indent, then attach rules already
    # indented two spaces for the list-under-mapping nesting.
    rules_block = "\n".join(
        textwrap.indent(textwrap.dedent(rule).strip("\n"), "  ") for rule in rules_yaml
    )
    raw = f"id: test\nversion: 1\nrules:\n{rules_block}\n"
    return parse_policy(raw)


# ---------------------------------------------------------------------------
# Return shape + manifest contract
# ---------------------------------------------------------------------------


def test_redact_returns_redaction_result_tuple_shape() -> None:
    """Engine yields a RedactionResult; the docstring's tuple framing
    is realised by the (redacted, manifest) field pair."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: r
              pattern: bearer_token
              action: redact
              reason: "test"
            """,
        ),
    )
    result = redact("Bearer abcdef0123456789", policy)
    assert isinstance(result, RedactionResult)
    # Field access mirroring the ``(redacted, manifest)`` framing.
    assert "[REDACTED:bearer_token]" in str(result.redacted)
    assert len(result.manifest) == 1


def test_no_matches_emits_empty_manifest_unchanged_payload() -> None:
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: r
              pattern: bearer_token
              action: redact
              reason: "test"
            """,
        ),
    )
    payload = {"status": "ok", "items": [1, 2, 3]}
    result = redact(payload, policy)
    assert result.redacted == payload
    assert result.manifest == ()


def test_manifest_entry_shape_per_match() -> None:
    """Each manifest entry carries type / count / span / reason."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-uuid
              pattern: uuid
              action: redact
              reason: "tenant correlator"
            """,
        ),
    )
    result = redact(
        "tenant=12345678-1234-1234-1234-123456789abc",
        policy,
    )
    assert len(result.manifest) == 1
    entry = result.manifest[0]
    assert isinstance(entry, RedactionManifestEntry)
    assert entry.rule == "strip-uuid"
    assert entry.pattern == "uuid"
    assert entry.action == "redact"
    assert entry.count == 1
    assert entry.reason == "tenant correlator"
    # Span is byte-indexed against the *pre-redaction* leaf; the UUID
    # starts after ``tenant=`` (7 chars) and is 36 chars long.
    start, end = entry.span
    assert start == len("tenant=")
    assert end == start + 36


def test_multiple_matches_collapse_to_one_manifest_entry_with_count() -> None:
    """Two UUIDs in one string => one manifest row, count=2."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-uuid
              pattern: uuid
              action: redact
              reason: "tenant correlator"
            """,
        ),
    )
    leaf = (
        "primary=12345678-1234-1234-1234-123456789abc "
        "secondary=abcdef00-aaaa-bbbb-cccc-aabbccddeeff"
    )
    result = redact(leaf, policy)
    assert len(result.manifest) == 1
    assert result.manifest[0].count == 2


# ---------------------------------------------------------------------------
# Nested payload walk + path tracking
# ---------------------------------------------------------------------------


def test_nested_dict_and_list_payload_walked() -> None:
    """The walker visits both Mapping and Sequence children."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-bearer
              pattern: bearer_token
              action: redact
              reason: "embedded token"
            """,
        ),
    )
    payload = {
        "request": {
            "headers": "Bearer eyJhbGciOiJIUzI1NiJ9",
            "method": "GET",
        },
        "items": [
            "Bearer abc123def456GHIJKL",
            {"deep": "no secret here"},
        ],
    }
    result = redact(payload, policy)
    # Structural identity preserved; mappings stay dicts, lists stay
    # lists, scalars untouched where no pattern fires.
    assert isinstance(result.redacted, dict)
    assert isinstance(result.redacted["items"], list)
    assert result.redacted["request"]["method"] == "GET"

    # Two leaves matched; manifest carries two entries with their paths.
    paths = sorted(entry.path for entry in result.manifest)
    assert paths == ["items.0", "request.headers"]


def test_string_rows_are_matched_as_one_text() -> None:
    """A secret that spans rows is found whole; every row keeps its place."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-key
              pattern: pem_private_key
              action: redact
              reason: "key split over rows"
            """,
        ),
    )
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    rows = ["start", begin, "keybodyline", end, "finish"]
    result = redact({"rows": rows, "total": 5}, policy)
    hidden = "[REDACTED:pem_private_key]"
    assert result.redacted == {"rows": ["start", hidden, hidden, hidden, "finish"], "total": 5}
    assert [entry.path for entry in result.manifest] == ["rows.1", "rows.2", "rows.3"]
    assert [entry.count for entry in result.manifest] == [1, 1, 1]
    # Offsets are the row's own, as for a string leaf.
    assert result.manifest[0].span == (0, len(begin))
    assert result.manifest[1].span == (0, len("keybodyline"))


def test_string_rows_keep_the_text_around_a_match_in_one_row() -> None:
    """A match inside one row changes only that part of that row."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-bearer
              pattern: bearer_token
              action: redact
              reason: "token in a log row"
            """,
        ),
    )
    rows = ["plain row", "got Bearer abc123def456GHIJKL from client", "plain row"]
    result = redact(rows, policy)
    assert result.redacted == ["plain row", "got [REDACTED:bearer_token] from client", "plain row"]
    assert len(result.manifest) == 1
    assert result.manifest[0].path == "1"
    assert result.manifest[0].span == (4, 4 + len("Bearer abc123def456GHIJKL"))


def test_string_rows_with_many_matches_are_redacted_in_linear_time() -> None:
    """A long list of rows with a secret in every row does not stall.

    This is the shape of a large ``log.tail`` result at its supported
    limit. Every row is rewritten and gets its own manifest entry, and
    the whole list is done well inside the time an operator waits for a
    response. Rows and matches are walked together once, so the time
    grows with the number of rows, not with rows times matches.
    """
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-bearer
              pattern: bearer_token
              action: redact
              reason: "token in every log row"
            """,
        ),
    )
    rows = [
        f"row {index}: Authorization: Bearer abc123def456GHIJKL{index:05d}"
        for index in range(10_000)
    ]
    started = time.perf_counter()
    result = redact({"rows": rows, "total": len(rows)}, policy)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"redaction of 10000 rows took {elapsed:.1f}s"
    assert len(result.redacted["rows"]) == len(rows)
    assert all("Bearer" not in row for row in result.redacted["rows"])
    assert result.redacted["rows"][0] == "row 0: Authorization: [REDACTED:bearer_token]"
    assert len(result.manifest) == len(rows)
    assert result.manifest[-1].path == "rows.9999"


def test_string_rows_keep_a_match_that_covers_an_empty_row() -> None:
    """An empty row inside a multi-row match is still inside the match."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-key
              pattern: pem_private_key
              action: redact
              reason: "key with a blank line"
            """,
        ),
    )
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    rows = ["start", begin, "", "keybodyline", end, "finish"]
    result = redact(rows, policy)
    hidden = "[REDACTED:pem_private_key]"
    assert result.redacted == ["start", hidden, hidden, hidden, hidden, "finish"]
    assert [entry.path for entry in result.manifest] == ["1", "2", "3", "4"]


def test_non_string_scalars_passed_through() -> None:
    """Numbers / booleans / None survive the walk verbatim."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: strip-uuid
              pattern: uuid
              action: redact
              reason: "should not affect ints"
            """,
        ),
    )
    payload = {"count": 42, "active": True, "missing": None, "ratio": 0.5}
    result = redact(payload, policy)
    assert result.redacted == payload
    assert result.manifest == ()


# ---------------------------------------------------------------------------
# Action semantics
# ---------------------------------------------------------------------------


def test_redact_action_substitutes_pattern_marker() -> None:
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: r
              pattern: bearer_token
              action: redact
              reason: "redact action"
            """,
        ),
    )
    result = redact("Bearer abcdefghij1234567890", policy)
    assert result.redacted == "[REDACTED:bearer_token]"


def test_mask_action_preserves_suffix() -> None:
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: r
              pattern: uuid
              action: mask
              reason: "mask action"
            """,
        ),
    )
    leaf = "tenant=12345678-1234-1234-1234-123456789abc"
    result = redact(leaf, policy)
    # Last four characters of the matched UUID are preserved; the
    # rest become asterisks. Total length unchanged.
    uuid_str = "12345678-1234-1234-1234-123456789abc"
    expected_mask = "*" * (len(uuid_str) - 4) + uuid_str[-4:]
    assert result.redacted == f"tenant={expected_mask}"


def test_value_group_limits_the_replacement_to_the_secret() -> None:
    """A pattern with a ``value`` group keeps the rest of the match.

    ``bind_key_secret`` matches the keyword, any comments, and the
    quoted value. Only the value is masked, and the manifest span
    points at the value.
    """
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: mask-bind
              pattern: bind_key_secret
              action: mask
              reason: "keep the keyword"
            """,
        ),
    )
    leaf = 'secret /* note */ "' + "abcdefgh" + 'ijkl";'
    result = redact(leaf, policy)
    assert result.redacted == 'secret /* note */ **********jkl";'
    start, end = result.manifest[0].span
    assert leaf[start:end] == '"abcdefghijkl"'


def test_hash_action_returns_stable_sha_prefix() -> None:
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: r
              pattern: fqdn
              action: hash
              reason: "hash action"
            """,
        ),
    )
    leaf = "endpoint=vcenter.lab.example.com"
    first = redact(leaf, policy)
    second = redact(leaf, policy)
    # Same input => same hashed string. The redacted suffix is
    # ``sha256:<12-hex>``; the prefix proves the format.
    assert first.redacted == second.redacted
    redacted_str = str(first.redacted)
    assert "sha256:" in redacted_str
    # The hex suffix is 12 lowercase hex characters.
    after_marker = redacted_str.split("sha256:", 1)[1][:12]
    assert all(c in "0123456789abcdef" for c in after_marker)


# ---------------------------------------------------------------------------
# Scope predicate
# ---------------------------------------------------------------------------


def test_rule_with_scope_skipped_when_labels_mismatch() -> None:
    """A scoped rule does nothing when the call labels don't match."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: github-only
              pattern: bearer_token
              action: redact
              reason: "github only"
              scope:
                connector_id: github
            """,
        ),
    )
    payload = "Bearer abcdef1234567890"
    no_match = redact(payload, policy, connector_id="kubernetes")
    assert no_match.redacted == payload
    assert no_match.manifest == ()

    match = redact(payload, policy, connector_id="github")
    assert "[REDACTED:bearer_token]" in str(match.redacted)
    assert len(match.manifest) == 1


# ---------------------------------------------------------------------------
# Determinism (no I/O / no clocks)
# ---------------------------------------------------------------------------


def test_redact_is_deterministic_for_identical_inputs() -> None:
    """Two back-to-back calls on the same input produce identical
    outputs and manifests (pure-function contract; required for
    C1-d's round-trip CI gate)."""
    policy = _policy_with_rules(
        textwrap.dedent(
            """
            - name: r1
              pattern: bearer_token
              action: redact
              reason: "first rule"
            - name: r2
              pattern: uuid
              action: mask
              reason: "second rule"
            """,
        ),
    )
    payload = {
        "auth": "Bearer abc123def456GHIJKL",
        "items": [
            "id=12345678-1234-1234-1234-123456789abc",
            "id=abcdef00-aaaa-bbbb-cccc-aabbccddeeff",
        ],
    }
    first = redact(payload, policy)
    second = redact(payload, policy)
    assert first.redacted == second.redacted
    assert first.manifest == second.manifest


# ---------------------------------------------------------------------------
# Defensive: get_pattern KeyError surfacing for an unknown name
# ---------------------------------------------------------------------------


def test_engine_raises_on_unknown_pattern_name_in_rule() -> None:
    """The schema rejects unknown names, but an engine-internal lookup
    must still raise cleanly if a rule is constructed via
    ``RedactionRule.model_construct`` (test-only path that skips
    validators)."""
    from meho_backplane.redaction.policy import RedactionRule, RedactionScope

    bogus = RedactionRule.model_construct(
        name="bogus",
        pattern="not_in_catalogue",
        action="redact",
        scope=RedactionScope(),
        reason="forced via model_construct",
    )
    policy = RedactionPolicy.model_construct(
        id="bogus",
        version=1,
        description="",
        rules=(bogus,),
    )
    with pytest.raises(KeyError):
        redact("anything", policy)
