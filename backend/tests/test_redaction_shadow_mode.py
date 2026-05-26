# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shadow / detection-only mode tests -- G11.4-T4 (#1073).

Acceptance criterion (#1073):

    Shadow mode emits a manifest without mutating the payload;
    covered by a test.

The mode is a policy-level flag (``mode: shadow`` in the YAML),
gated inside :func:`meho_backplane.redaction.engine.redact`. Tests
cover three layers:

1. **Default-enforce**: a policy without ``mode:`` defaults to
   ``enforce`` and the existing redact-and-mutate behaviour holds.
2. **Engine-level shadow**: a policy with ``mode: shadow`` returns
   the input payload unchanged in :attr:`RedactionResult.redacted`
   but the manifest still carries every detection.
3. **Middleware-level shadow**: the same flag propagates through
   :func:`apply_connector_boundary_redaction` so the dispatcher's
   audit row records a populated manifest while the caller-visible
   payload is the raw view.

The middleware test pins that the policy-level flag is the *only*
threading needed -- no per-call ``mode=`` arg threaded everywhere,
no separate "shadow middleware" wrapper. That's the architectural
contract spelled out in the task's hard requirement ("clean
policy-level flag, not a runtime arg threaded everywhere").
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meho_backplane.redaction import (
    RedactionPolicy,
    apply_connector_boundary_redaction,
    clear_overrides,
    parse_policy,
    redact,
    register_policy,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_overrides() -> Iterator[None]:
    """Reset registered policy overrides around every test.

    The resolver is process-global mutable state; a leaked
    registration would surface as a flake in the next test. Mirrors
    the autouse pattern in ``test_redaction_resolver.py``.
    """
    clear_overrides()
    yield
    clear_overrides()


def _bearer_policy(*, mode: str | None = None) -> RedactionPolicy:
    """Return a one-rule policy that strips bearer tokens.

    *mode* threads through to the YAML. ``None`` (the default)
    omits the key entirely so the test exercises the schema's
    default-fill behaviour ("policy without mode -> enforce").
    """
    # Assemble the YAML without relying on f-string interpolation
    # inside a triple-quoted dedented block -- the splice column
    # for the optional ``mode:`` line breaks the YAML otherwise.
    lines = [
        "id: shadow-test-policy",
        "version: 1",
    ]
    if mode is not None:
        lines.append(f"mode: {mode}")
    lines.extend(
        [
            "rules:",
            "  - name: strip-bearer",
            "    pattern: bearer_token",
            "    action: redact",
            '    reason: "shadow-mode test policy"',
        ],
    )
    return parse_policy("\n".join(lines))


# ---------------------------------------------------------------------------
# Defaults: an absent ``mode`` key parses as enforce
# ---------------------------------------------------------------------------


def test_policy_without_mode_defaults_to_enforce() -> None:
    """A policy with no ``mode:`` key is parsed as ``enforce``.

    Backward-compatibility contract: every policy YAML written
    before #1073 (and every policy authored without explicit
    awareness of shadow mode) must continue to mutate payloads.
    A regression here would silently turn an entire fleet into
    shadow mode on the next deploy.
    """
    policy = _bearer_policy()
    assert policy.mode == "enforce"


def test_enforce_mode_mutates_payload_as_before() -> None:
    """Enforce mode is the unchanged pre-#1073 behaviour.

    Mirrors the engine integration tests' shape: the bearer token
    in the payload is replaced with ``[REDACTED:bearer_token]``
    and the manifest carries one entry. Acts as the baseline the
    shadow test below diverges from.
    """
    policy = _bearer_policy()  # implicit enforce
    payload = {"log": "Bearer abc123def456ghi789"}

    result = redact(payload, policy)

    assert result.redacted == {"log": "[REDACTED:bearer_token]"}
    assert len(result.manifest) == 1


# ---------------------------------------------------------------------------
# Shadow mode at the engine layer
# ---------------------------------------------------------------------------


def test_shadow_mode_returns_payload_unmodified() -> None:
    """Shadow mode does not mutate the payload.

    Engine-level acceptance: the same payload + same rule that
    enforce-mode rewrites is returned untouched under
    ``mode: shadow``. Strings are compared via ``==`` so a
    sneaky whitespace change would also be caught.
    """
    policy = _bearer_policy(mode="shadow")
    payload = {"log": "Bearer abc123def456ghi789"}

    result = redact(payload, policy)

    assert result.redacted == payload, (
        "shadow mode must not mutate the payload; "
        "got a rewritten value where the input was expected"
    )


def test_shadow_mode_still_emits_manifest() -> None:
    """Shadow mode still produces the detection manifest.

    The whole point of the mode is to preserve detection (visible
    in audit + dashboards) without mutating. A test that only
    asserts "payload unchanged" could pass even if the engine
    skipped the rule entirely; this test pins that the manifest
    entry is identical to the enforce-mode manifest entry.
    """
    shadow_policy = _bearer_policy(mode="shadow")
    enforce_policy = _bearer_policy()
    payload = {"log": "Bearer abc123def456ghi789"}

    shadow_result = redact(payload, shadow_policy)
    enforce_result = redact(payload, enforce_policy)

    assert len(shadow_result.manifest) == 1

    # Compare projected manifests: rule / pattern / action / count /
    # path. ``span`` is excluded because the engine docstring marks it
    # diagnostic-only; ``reason`` is identical here by construction.
    def _project(entry: object) -> tuple[object, ...]:
        return (entry.rule, entry.pattern, entry.action, entry.count, entry.path)  # type: ignore[attr-defined]

    assert [_project(e) for e in shadow_result.manifest] == [
        _project(e) for e in enforce_result.manifest
    ], "shadow-mode manifest must match enforce-mode manifest projection"


def test_shadow_mode_with_no_matches_emits_empty_manifest_and_unchanged_payload() -> None:
    """A payload with no matching strings still round-trips cleanly.

    Shadow mode degenerates to "no-op" when the rules don't fire,
    which must not crash, must not invent manifest entries, and
    must hand back the input verbatim.
    """
    policy = _bearer_policy(mode="shadow")
    payload = {"log": "no token here", "neighbour": 42}

    result = redact(payload, policy)

    assert result.redacted == payload
    assert result.manifest == ()


# ---------------------------------------------------------------------------
# Shadow mode through the middleware (policy-level flag is sufficient)
# ---------------------------------------------------------------------------


def test_shadow_mode_propagates_through_connector_boundary_middleware() -> None:
    """The middleware honours ``mode: shadow`` without per-call args.

    Hard requirement: shadow mode is a clean policy-level flag, not
    a runtime arg threaded everywhere. This test pins that contract:
    we register a shadow-mode policy via the resolver and call
    :func:`apply_connector_boundary_redaction` with the standard
    (raw, connector_id, tenant, op) interface -- no new kwargs.
    The middleware returns the un-mutated raw view in ``redacted``
    while the manifest carries the detection.
    """
    shadow_policy = _bearer_policy(mode="shadow")
    # Register on the connector_id label that the call below carries.
    # The resolver ladder picks ``(connector_id, None, None)`` at
    # level 4, so a per-connector override is the cheapest way to
    # exercise the path without leaning on the (None, None, None)
    # registration shape (which is reachable via the default-policy
    # fallback rather than the ladder).
    register_policy(shadow_policy, connector_id="github")

    raw = {"log": "Bearer abc123def456ghi789"}

    result = apply_connector_boundary_redaction(
        raw,
        connector_id="github",
        tenant="t1",
        op="repos.list",
    )

    # Caller-visible payload: un-mutated raw.
    assert result.redacted == raw, (
        "middleware must surface the un-mutated payload to the caller "
        "when the resolved policy is in shadow mode"
    )
    # Manifest still populated -- the audit row gets full detection signal.
    assert len(result.manifest) == 1
    assert result.manifest[0].pattern == "bearer_token"
    # Policy id stable -- the audit row's policy attribution still
    # records which shadow policy fired so an operator can correlate
    # detection counts to a YAML file.
    assert result.policy_id == "shadow-test-policy"


def test_shadow_mode_policy_yaml_round_trips_through_parse_policy() -> None:
    """``mode: shadow`` survives YAML -> Pydantic round trip.

    A regression where ``parse_policy`` silently drops the ``mode``
    field on the way through (e.g. a future ``extra='ignore'``
    change) would let a shadow policy revert to enforce mid-deploy.
    This test fails immediately if that happens.
    """
    policy = _bearer_policy(mode="shadow")
    assert policy.mode == "shadow"
