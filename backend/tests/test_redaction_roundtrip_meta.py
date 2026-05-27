# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Meta-tests for the round-trip CI gate -- G11.4-T4 (#1073).

The round-trip fixture suite in ``test_redaction_roundtrip_fixtures.py``
is the actual CI gate. This file proves the gate *would* fail if an
operator submitted a regressing fixture pair -- the two cases #1073's
acceptance criteria call out by name:

* an injected leak (raw secret survives into ``expected``) makes the
  gate fail;
* an injected over-redaction (``expected`` redacts something the
  policy did not target) makes the gate fail.

The tests inject the mutation in-process by building an in-memory
fixture (policy + raw + tampered expected) and asserting the same
equality the harness uses fires. We deliberately do NOT create
tampered files on disk and re-invoke pytest -- the gate's
correctness is verified by exercising the comparator, not by
spawning a subprocess. (A nested pytest invocation is brittle on
xdist + the heavy ARC runner.)

A test that just calls ``assert raw != expected`` would also catch
both, but the spirit of #1073 is "prove the gate's CI behaviour";
this file's two ``with pytest.raises(AssertionError)`` blocks are
the structural proof.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.redaction import parse_policy, redact

# A minimal policy that redacts bearer tokens. Shared across both
# meta-tests so the only thing that varies between them is the
# tampered expected payload -- the comparator behaviour is the
# variable under test, not the policy.
_MINIMAL_POLICY_YAML = """
id: meta-gate-policy
version: 1
rules:
  - name: strip-bearer
    pattern: bearer_token
    action: redact
    reason: "meta-gate proof: bearer token must be redacted"
"""

# A canonical raw payload: one bearer token in a nested dict, plus an
# untouched neighbour. Drives both injected-leak and injected-over-
# redaction cases below.
_RAW_PAYLOAD: dict[str, Any] = {
    "log": "GET /foo with Bearer abc123def456ghi789 -> 200 OK",
    "neighbour": "harmless free text",
}

# What the policy actually produces (the ground truth used by the
# gate). Bearer token redacted; neighbour untouched.
_CORRECT_REDACTED: dict[str, Any] = {
    "log": "GET /foo with [REDACTED:bearer_token] -> 200 OK",
    "neighbour": "harmless free text",
}


def _run_engine(raw: Any) -> Any:
    """Helper: parse the shared policy and run the engine on *raw*.

    Each test re-runs the engine fresh; the policy is parsed each
    call so a future test that mutates the YAML can do so without
    leaking state across tests via a cached policy object.
    """
    policy = parse_policy(_MINIMAL_POLICY_YAML)
    return redact(raw, policy).redacted


def test_correct_pair_passes_the_gate() -> None:
    """Sanity baseline: an honest fixture pair passes equality.

    Without this test, a broken comparator would silently pass the
    two failure-injection tests below ("the AssertionError fired,
    so we're good!") even though the gate would in fact be
    rubber-stamping bad fixtures. This test is the positive
    pole.
    """
    actual = _run_engine(_RAW_PAYLOAD)
    assert actual == _CORRECT_REDACTED


def test_injected_leak_fails_the_gate() -> None:
    """A leak (un-redacted secret in ``expected``) must fail the gate.

    Acceptance criterion (#1073 issue body):

        "an injected leak (un-redacted secret in `expected`)
        fails CI"

    Concretely: an operator submits a fixture where ``expected``
    keeps the bare ``Bearer abc123...`` string instead of the
    ``[REDACTED:bearer_token]`` marker. The harness compares the
    engine's output (which DID redact) to the tampered expected
    (which did NOT redact) and the equality fails -- exactly the
    behaviour CI needs to see for a leak to block merge.
    """
    # Tampered expected: the operator forgot to redact -- the
    # captured raw string is mirrored verbatim.
    tampered_expected_leak: dict[str, Any] = {
        "log": "GET /foo with Bearer abc123def456ghi789 -> 200 OK",  # leaked!
        "neighbour": "harmless free text",
    }
    actual = _run_engine(_RAW_PAYLOAD)

    # The harness's assertion is ``result.redacted == expected``.
    # We perform the same comparison here in a way that surfaces
    # AssertionError when it diverges, mirroring the gate's
    # observable behaviour to the CI runner.
    with pytest.raises(AssertionError):
        assert actual == tampered_expected_leak


def test_injected_over_redaction_fails_the_gate() -> None:
    """Over-redaction must also fail the gate.

    Acceptance criterion (#1073 issue body):

        "an injected over-redaction fails CI"

    Concretely: an operator submits a fixture where ``expected``
    redacts the ``neighbour`` field (an untargeted leaf) on top of
    the bearer token. The engine's actual output only redacts the
    bearer token; the tampered expected expects *both* to be
    blanked. The equality fails -- exactly the behaviour CI needs
    to see for over-redaction (a usability failure as load-bearing
    as a leak per Initiative #805 DoD) to block merge.
    """
    tampered_expected_over: dict[str, Any] = {
        "log": "GET /foo with [REDACTED:bearer_token] -> 200 OK",
        "neighbour": "[REDACTED:bearer_token]",  # over-redacted!
    }
    actual = _run_engine(_RAW_PAYLOAD)

    with pytest.raises(AssertionError):
        assert actual == tampered_expected_over
