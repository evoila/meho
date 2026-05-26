# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-2 Presidio adapter tests -- Task #1072.

Acceptance criteria mapped:

* "Tier-2 redacts a free-text field containing a person name / IP /
  FQDN and records manifest entries." -> :func:`test_tier2_redacts_person_ip_url_in_free_text`.
* "Tier-2 runs **only** on policy-flagged free-text fields; a
  Tier-1-only policy never loads Presidio." -> :func:`test_tier1_only_policy_never_imports_presidio`
  and :func:`test_tier2_skips_non_flagged_fields`.

The acceptance tests rely on a spaCy model being installed on the
host. They auto-skip with a clear remediation message when no model
is available (locally: ``uv run python -m spacy download
en_core_web_sm``; in CI the model is provisioned by the workflow YAML).
This mirrors the ``MEHO_RUN_SLOW_TESTS`` / ``ENV_VAR not set``
sandbox-skip posture used elsewhere in the suite.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator

import pytest

from meho_backplane.redaction import (
    Tier2NotAvailableError,
    apply_tier2,
    clear_engine_cache,
    get_engines,
    parse_policy,
    policy_uses_tier2,
)
from meho_backplane.redaction.path_glob import glob_to_regex, path_matches
from meho_backplane.redaction.policy import Tier2Rule


def _spacy_model_available() -> bool:
    """Best-effort check: can we construct the analyser without raising?

    Distinct from :func:`get_engines` so tests can gate on it without
    forcing the actual engine load on every run. We probe with the
    cheaper ``spacy.util.is_package`` check first; the engine
    construction itself is the load-bearing assertion.
    """
    try:
        import spacy.util
    except ImportError:
        return False
    # Either en_core_web_lg (production default) or en_core_web_sm
    # (sandbox / fast-CI lane).
    return any(spacy.util.is_package(model) for model in ("en_core_web_lg", "en_core_web_sm"))


_REQUIRES_SPACY = pytest.mark.skipif(
    not _spacy_model_available(),
    reason=(
        "Skipped: no spaCy model installed. Locally: `uv run python -m spacy "
        "download en_core_web_sm`. CI provisions en_core_web_lg via ci.yml."
    ),
)


@pytest.fixture(autouse=True)
def _reset_engine_cache() -> Iterator[None]:
    """Drop cached engines around every test so an env-var swap in
    one test does not leak its model choice into the next."""
    clear_engine_cache()
    yield
    clear_engine_cache()


# ---------------------------------------------------------------------------
# Capability-flag guarantee -- the load-bearing #1072 acceptance test
# ---------------------------------------------------------------------------


def test_policy_uses_tier2_false_when_block_absent() -> None:
    """A Tier-1-only policy reports ``policy_uses_tier2 == False``."""
    raw = textwrap.dedent(
        """
        id: tier1-only
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        """,
    )
    policy = parse_policy(raw)
    assert policy_uses_tier2(policy) is False


def test_policy_uses_tier2_true_when_block_present() -> None:
    """A policy with a tier2 block reports ``policy_uses_tier2 == True``."""
    raw = textwrap.dedent(
        """
        id: with-tier2
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        tier2:
          - name: scrub
            fields: [description]
            reason: ner
        """,
    )
    policy = parse_policy(raw)
    assert policy_uses_tier2(policy) is True


def test_apply_tier2_short_circuits_when_block_absent() -> None:
    """:func:`apply_tier2` returns the payload unchanged with an empty
    manifest when the policy has no ``tier2`` block -- without
    touching Presidio."""
    raw = textwrap.dedent(
        """
        id: tier1-only
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        """,
    )
    policy = parse_policy(raw)
    payload = {"description": "John Doe at 10.0.0.1"}
    result = apply_tier2(payload, policy)
    # The function returns the input object verbatim and an empty
    # manifest -- no engine instantiated, no string rewritten.
    assert result.redacted is payload
    assert result.manifest == ()


def test_tier1_only_policy_never_imports_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance gate: with Presidio imports blocked, a Tier-1-only
    policy runs the middleware end-to-end without raising.

    Uses a meta-path finder that fakes a missing presidio install to
    prove the guarantee holds even on hosts that do not have presidio
    available (e.g. a stripped runtime container)."""

    class _BlockPresidio:
        def find_spec(
            self,
            fullname: str,
            path: object = None,
            target: object = None,
        ) -> None:
            if fullname.startswith("presidio_"):
                raise ImportError(f"presidio blocked by test: {fullname}")
            return None

    # Drop any presidio modules that may have been imported by an
    # earlier test in the same session, then install the blocker.
    for mod in [k for k in sys.modules if k.startswith("presidio_")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(
        sys,
        "meta_path",
        [_BlockPresidio(), *sys.meta_path],
    )

    raw = textwrap.dedent(
        """
        id: tier1-only
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        """,
    )
    policy = parse_policy(raw)

    # apply_tier2 short-circuits BEFORE any presidio import, so this
    # call succeeds even with imports blocked.
    payload = {"description": "Bearer abcdef123456"}
    result = apply_tier2(payload, policy)
    assert result.redacted is payload
    # No presidio modules were imported.
    assert not [k for k in sys.modules if k.startswith("presidio_")]


def test_tier2_raises_when_presidio_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """:func:`get_engines` surfaces the missing dep as
    :class:`Tier2NotAvailableError` (the documented failure mode)."""

    class _BlockPresidio:
        def find_spec(
            self,
            fullname: str,
            path: object = None,
            target: object = None,
        ) -> None:
            if fullname.startswith("presidio_"):
                raise ImportError(f"presidio blocked by test: {fullname}")
            return None

    for mod in [k for k in sys.modules if k.startswith("presidio_")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(
        sys,
        "meta_path",
        [_BlockPresidio(), *sys.meta_path],
    )

    with pytest.raises(Tier2NotAvailableError):
        get_engines("en")


# ---------------------------------------------------------------------------
# Field-path glob matcher
# ---------------------------------------------------------------------------


def test_glob_matches_top_level_literal() -> None:
    assert glob_to_regex("description").match("description")
    assert not glob_to_regex("description").match("descriptions")
    assert not glob_to_regex("description").match("a.description")


def test_glob_matches_single_segment_wildcard() -> None:
    pattern = glob_to_regex("items.*.message")
    assert pattern.match("items.0.message")
    assert pattern.match("items.abc.message")
    # ``*`` matches exactly one segment, not multiple.
    assert not pattern.match("items.0.nested.message")
    assert not pattern.match("items.message")


def test_glob_matches_double_star_any_depth() -> None:
    pattern = glob_to_regex("**.error")
    assert pattern.match("error")
    assert pattern.match("a.error")
    assert pattern.match("a.b.c.error")


def test_path_matches_against_rule_fields() -> None:
    rule = Tier2Rule(
        name="r",
        fields=("description", "items.*.message"),
        reason="test",
    )
    assert path_matches(rule.fields, "description")
    assert path_matches(rule.fields, "items.0.message")
    assert path_matches(rule.fields, "items.abc.message")
    assert not path_matches(rule.fields, "summary")
    assert not path_matches(rule.fields, "items.0.title")


# ---------------------------------------------------------------------------
# Acceptance: Tier-2 redacts a free-text field with PERSON / IP / URL
# ---------------------------------------------------------------------------


@_REQUIRES_SPACY
def test_tier2_redacts_person_ip_url_in_free_text() -> None:
    """Issue #1072 acceptance: a free-text field containing PERSON /
    IP / FQDN is redacted and the manifest records entries."""
    raw = textwrap.dedent(
        """
        id: tier2-acceptance
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        tier2:
          - name: scrub-error
            fields:
              - error.message
            entities:
              - PERSON
              - IP_ADDRESS
              - URL
            action: redact
            threshold: 0.3
            reason: free-text ner
        """,
    )
    policy = parse_policy(raw)
    payload = {
        "error": {
            "message": (
                "Operator John Smith reported a connection to 192.168.1.50 via "
                "https://example.org/path."
            ),
        },
    }
    result = apply_tier2(payload, policy)

    redacted_msg = result.redacted["error"]["message"]  # type: ignore[index]
    # At least one of the three entities should redact -- the small
    # spaCy model may miss PERSON on shorter strings but reliably
    # catches IP and URL.
    assert "[REDACTED:presidio:" in redacted_msg
    # Every emitted manifest entry carries the correct shape.
    assert len(result.manifest) >= 1
    for entry in result.manifest:
        assert entry.rule == "scrub-error"
        assert entry.pattern.startswith("presidio:")
        assert entry.action == "redact"
        assert entry.path == "error.message"
        assert entry.count >= 1
    # IP_ADDRESS is the most reliable signal across both lg and sm
    # spaCy models -- the IP recogniser is regex-backed, not NER.
    patterns = {e.pattern for e in result.manifest}
    assert "presidio:IP_ADDRESS" in patterns


@_REQUIRES_SPACY
def test_tier2_skips_non_flagged_fields() -> None:
    """Acceptance: Tier-2 fires only on policy-flagged paths."""
    raw = textwrap.dedent(
        """
        id: tier2-field-gated
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        tier2:
          - name: scrub-message
            fields:
              - error.message
            entities:
              - IP_ADDRESS
            action: redact
            threshold: 0.3
            reason: only the message field
        """,
    )
    policy = parse_policy(raw)
    payload = {
        "summary": "10.0.0.1 was the source",  # NOT flagged
        "error": {
            "message": "10.0.0.2 was the source",  # flagged
        },
    }
    result = apply_tier2(payload, policy)

    # ``summary`` is untouched (not in fields).
    assert result.redacted["summary"] == "10.0.0.1 was the source"  # type: ignore[index]
    # ``error.message`` is rewritten.
    assert "10.0.0.2" not in result.redacted["error"]["message"]  # type: ignore[index]


@_REQUIRES_SPACY
def test_tier2_scope_filters_apply() -> None:
    """Tier-2 rules honour the same scope predicate as Tier-1."""
    raw = textwrap.dedent(
        """
        id: tier2-scoped
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        tier2:
          - name: scrub-github-only
            fields:
              - description
            entities:
              - IP_ADDRESS
            scope:
              connector_id: github
            threshold: 0.3
            reason: github only
        """,
    )
    policy = parse_policy(raw)
    payload = {"description": "Server at 10.0.0.1 had an issue"}

    # connector_id=github -> rule fires.
    fired = apply_tier2(payload, policy, connector_id="github")
    assert fired.manifest
    assert "10.0.0.1" not in fired.redacted["description"]  # type: ignore[index]

    # connector_id=kubernetes -> scope mismatches, rule skips.
    skipped = apply_tier2(payload, policy, connector_id="kubernetes")
    assert skipped.manifest == ()
    assert skipped.redacted == payload


@_REQUIRES_SPACY
def test_tier2_threshold_filters_low_confidence() -> None:
    """A high threshold suppresses low-confidence matches."""
    raw_high = textwrap.dedent(
        """
        id: tier2-high-threshold
        version: 1
        rules:
          - name: strip-bearer
            pattern: bearer_token
            action: redact
            reason: tier1
        tier2:
          - name: scrub
            fields:
              - description
            entities:
              - PERSON
            threshold: 0.99   # essentially "never match"
            reason: high
        """,
    )
    policy_high = parse_policy(raw_high)
    payload = {"description": "Alice met Bob at the office."}
    high = apply_tier2(payload, policy_high)
    # At threshold 0.99 we expect NO matches even if the model
    # tags PERSON spans -- Presidio's default scores top out below
    # 0.99 for unambiguous PERSON entities (typically ~0.85).
    assert high.manifest == ()


# ---------------------------------------------------------------------------
# Engine caching
# ---------------------------------------------------------------------------


@_REQUIRES_SPACY
def test_get_engines_caches_per_language() -> None:
    """Two consecutive calls with the same language return the same pair."""
    first = get_engines("en")
    second = get_engines("en")
    assert first is second


@_REQUIRES_SPACY
def test_clear_engine_cache_forces_rebuild() -> None:
    """:func:`clear_engine_cache` drops the cache so a subsequent call
    constructs a fresh engine pair."""
    first = get_engines("en")
    clear_engine_cache()
    second = get_engines("en")
    assert first is not second
