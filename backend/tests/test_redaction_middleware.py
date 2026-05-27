# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.redaction.middleware`.

Pins:

* :func:`apply_connector_boundary_redaction` returns the
  ``RedactionMiddlewareResult`` shape (raw, redacted, manifest,
  policy_id).
* The default-safe policy is what fires when no override matches.
* Normalisation: Pydantic models flatten to dicts; tuples / sets
  flatten to lists; bytes become hex; non-string keys coerce.
* :func:`manifest_to_audit_payload` produces JSON-encoder-safe dicts.
* Bearer-token-shaped strings round-trip raw -> redacted.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator

import pytest
from pydantic import BaseModel

from meho_backplane.redaction import (
    RedactionMiddlewareResult,
    apply_connector_boundary_redaction,
    clear_overrides,
    manifest_to_audit_payload,
    normalize_for_audit,
    parse_policy,
    register_policy,
)


@pytest.fixture(autouse=True)
def _isolate_overrides() -> Iterator[None]:
    """Reset resolver overrides around every test."""
    clear_overrides()
    yield
    clear_overrides()


def test_bearer_token_redacted_default_safe() -> None:
    """An un-configured call still strips a bearer token: the default
    policy is the conservative default-safe answer, not pass-through."""
    raw = {"authorization": "Bearer eyJabcdefghijklmnop1234"}
    result = apply_connector_boundary_redaction(
        raw,
        connector_id="some-connector",
        tenant=None,
        op="some.op",
    )

    assert isinstance(result, RedactionMiddlewareResult)
    assert "Bearer eyJ" not in str(result.redacted)
    assert "[REDACTED:" in str(result.redacted["authorization"])
    # raw preserves the original value so the audit row keeps the
    # pre-redaction view.
    assert "Bearer eyJabcdefghijklmnop1234" in str(result.raw["authorization"])
    assert result.policy_id == "connector-boundary-default"


def test_manifest_records_every_rule_firing() -> None:
    """One manifest entry per rule per leaf; the engine's contract is
    surfaced unchanged through the middleware."""
    raw = {
        "headers": {
            "authorization": "Bearer eyJabcdefghijklmnop1234",
            "x-api-key": "api_key=topsecretvalue1234",
        },
    }
    result = apply_connector_boundary_redaction(
        raw,
        connector_id="github",
        tenant=None,
        op="repos.get",
    )
    # At least the bearer + api_key firings; the default-safe policy
    # may produce more depending on the value shape (e.g. inner
    # ``Bearer`` plus authorization-header rule both fire).
    rule_names = {entry.rule for entry in result.manifest}
    assert {"strip-bearer-token", "strip-api-key"}.issubset(rule_names)


def test_override_policy_takes_effect() -> None:
    """A registered override on the call's connector_id is what the
    middleware applies, not the default."""
    minimal = parse_policy(
        textwrap.dedent(
            """
            id: minimal-uuid-only
            version: 1
            rules:
              - name: hash-uuid
                pattern: uuid
                action: hash
                reason: "test override"
            """
        ).strip()
    )
    register_policy(minimal, connector_id="github")

    raw = {
        "id": "deadbeef-1234-5678-90ab-cdef12345678",
        "authorization": "Bearer eyJabcdefghijklmnop1234",
    }
    result = apply_connector_boundary_redaction(
        raw,
        connector_id="github",
        tenant=None,
        op="any.op",
    )
    assert result.policy_id == "minimal-uuid-only"
    # The override has no bearer-token rule, so the bearer survives.
    assert "Bearer eyJ" in str(result.redacted["authorization"])
    # The UUID is hashed.
    assert "sha256:" in result.redacted["id"]


def test_normalize_pydantic_model_flattens_to_dict() -> None:
    """Handlers returning Pydantic models still reach the engine and
    audit row as JSON-shaped dicts."""

    class Resp(BaseModel):
        id: int
        token: str

    out = normalize_for_audit(Resp(id=42, token="Bearer eyJabcdefghijklmnop1234"))
    assert out == {"id": 42, "token": "Bearer eyJabcdefghijklmnop1234"}


def test_normalize_tuple_and_set_flatten_to_list() -> None:
    """Tuples and sets become lists; the audit JSON encoder can serialise
    them safely."""
    out = normalize_for_audit({"a": (1, 2, 3), "b": {"x", "y"}})
    assert isinstance(out["a"], list)
    assert out["a"] == [1, 2, 3]
    assert isinstance(out["b"], list)
    assert sorted(out["b"]) == ["x", "y"]


def test_normalize_non_string_key_is_stringified() -> None:
    """JSON cannot represent non-string keys; the audit row needs
    string keys."""
    out = normalize_for_audit({1: "a", 2: "b"})
    assert out == {"1": "a", "2": "b"}


def test_normalize_bytes_to_hex() -> None:
    """Binary payloads at the redaction boundary surface as hex so
    they are JSON-encoder-safe and don't trigger spurious base64
    matches in the named-pattern library."""
    out = normalize_for_audit({"blob": b"\xde\xad\xbe\xef"})
    assert out["blob"] == "deadbeef"


def test_manifest_to_audit_payload_returns_jsonable_list() -> None:
    """The manifest serialises to a list of plain dicts; the audit
    insert encoder accepts them without per-row Pydantic round-trip."""
    raw = {"v": "Bearer eyJabcdefghijklmnop1234"}
    result = apply_connector_boundary_redaction(
        raw,
        connector_id=None,
        tenant=None,
        op=None,
    )
    serialised = manifest_to_audit_payload(result.manifest)
    assert isinstance(serialised, list)
    assert all(isinstance(entry, dict) for entry in serialised)
    # Each dict has the expected keys.
    sample = serialised[0]
    assert {"rule", "pattern", "action", "count", "span", "reason", "path"} <= sample.keys()


def test_redaction_pipeline_is_idempotent() -> None:
    """Same input + same policy → identical output (engine determinism
    surfaced through the middleware)."""
    raw = {
        "token": "Bearer eyJabcdefghijklmnop1234",
        "id": "deadbeef-1234-5678-90ab-cdef12345678",
    }
    first = apply_connector_boundary_redaction(raw, connector_id="c", tenant=None, op="o")
    second = apply_connector_boundary_redaction(raw, connector_id="c", tenant=None, op="o")
    assert first.redacted == second.redacted
    assert first.policy_id == second.policy_id
    assert manifest_to_audit_payload(first.manifest) == manifest_to_audit_payload(second.manifest)


def test_str_payload_passes_through_engine() -> None:
    """A top-level string payload is a legal handler return shape;
    the engine treats it as one leaf."""
    raw = "Bearer eyJabcdefghijklmnop1234"
    result = apply_connector_boundary_redaction(raw, connector_id=None, tenant=None, op=None)
    assert "Bearer eyJ" not in str(result.redacted)
    assert "[REDACTED:bearer_token]" in str(result.redacted)


def test_none_payload_passes_through_unchanged() -> None:
    """A connector returning ``None`` produces an empty manifest and
    the redacted view stays ``None``."""
    result = apply_connector_boundary_redaction(None, connector_id=None, tenant=None, op=None)
    assert result.raw is None
    assert result.redacted is None
    assert result.manifest == ()
