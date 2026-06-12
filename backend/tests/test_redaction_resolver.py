# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.redaction.resolver`.

Pins:

* Default-safe: an un-configured call returns the packaged default
  policy (not pass-through; the policy has rules).
* Specificity ladder: ``(connector_id, tenant, op)`` beats
  ``(connector_id, op)`` beats ``connector_id`` beats ``tenant`` beats
  the built-in default.
* ``clear_overrides()`` actually clears -- subsequent resolves fall
  back to the default.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator

import pytest

from meho_backplane.redaction import (
    RedactionPolicy,
    clear_overrides,
    get_default_policy,
    parse_policy,
    register_policy,
    resolve_policy,
)


@pytest.fixture(autouse=True)
def _isolate_overrides() -> Iterator[None]:
    """Reset registered overrides around every test in this file.

    The resolver is process-global state. A leaked registration would
    surface as a flake in the next test that calls :func:`resolve_policy`
    against the same labels. The autouse teardown is the cheapest way
    to keep the file deterministic under pytest-xdist parallelism
    (each worker has its own interpreter, so the lock isn't sufficient
    -- the registry would still leak within one worker).
    """
    clear_overrides()
    yield
    clear_overrides()


def _policy(policy_id: str) -> RedactionPolicy:
    """Build a one-rule policy with the given id for matching."""
    return parse_policy(
        textwrap.dedent(
            f"""
            id: {policy_id}
            version: 1
            rules:
              - name: r-{policy_id}
                pattern: bearer_token
                action: redact
                reason: "test policy {policy_id}"
            """
        ).strip()
    )


def test_default_policy_returned_when_no_overrides_registered() -> None:
    """Default-safe contract: an un-configured call gets the packaged
    default policy, which still applies the named-pattern library."""
    resolved = resolve_policy(connector_id="anything", tenant="t1", op="op.x")
    assert resolved is get_default_policy()
    # The default-safe policy is not empty -- proving "not pass-through".
    assert len(resolved.rules) >= 1
    # Default-safe targets credential-shaped patterns.
    pattern_names = {rule.pattern for rule in resolved.rules}
    assert "authorization_header" in pattern_names
    assert "bearer_token" in pattern_names


def test_default_policy_load_is_cached() -> None:
    """The packaged YAML is loaded once and reused; two calls return
    the identical reference."""
    first = get_default_policy()
    second = get_default_policy()
    assert first is second


def test_register_connector_id_override_beats_default() -> None:
    """A ``(connector_id, None, None)`` override fires when the call's
    connector_id matches, regardless of tenant or op labels."""
    custom = _policy("custom-github")
    register_policy(custom, connector_id="github")

    resolved = resolve_policy(connector_id="github", tenant="t1", op="op.x")
    assert resolved is custom


def test_more_specific_override_beats_less_specific() -> None:
    """The ``(connector_id, tenant, op)`` triple is the most specific
    key; a less-specific override registered for the same connector_id
    must not win."""
    broad = _policy("broad-github")
    narrow = _policy("narrow-github-tenant-op")
    register_policy(broad, connector_id="github")
    register_policy(narrow, connector_id="github", tenant="t1", op="op.x")

    resolved = resolve_policy(connector_id="github", tenant="t1", op="op.x")
    assert resolved is narrow


def test_connector_op_override_beats_connector_only() -> None:
    """``(connector_id, None, op)`` beats ``(connector_id, None, None)``
    when the op label matches."""
    broad = _policy("broad-github")
    op_scoped = _policy("github-op-x")
    register_policy(broad, connector_id="github")
    register_policy(op_scoped, connector_id="github", op="op.x")

    resolved = resolve_policy(connector_id="github", tenant="t1", op="op.x")
    assert resolved is op_scoped


def test_tenant_wide_override_falls_back_to_default_for_other_tenants() -> None:
    """A tenant-wide override on (None, t1, None) applies to t1 but
    not to other tenants."""
    t1_policy = _policy("tenant-t1")
    register_policy(t1_policy, tenant="t1")

    resolved_t1 = resolve_policy(connector_id="github", tenant="t1", op="op.x")
    resolved_t2 = resolve_policy(connector_id="github", tenant="t2", op="op.x")
    assert resolved_t1 is t1_policy
    assert resolved_t2 is get_default_policy()


def test_none_tenant_call_does_not_match_tenant_scoped_override() -> None:
    """A ``None`` tenant label only matches an override whose tenant
    is also ``None``; tenant-scoped overrides are skipped."""
    t1_policy = _policy("tenant-t1")
    register_policy(t1_policy, tenant="t1")

    resolved = resolve_policy(connector_id="github", tenant=None, op="op.x")
    assert resolved is get_default_policy()


def test_clear_overrides_resets_resolution_to_default() -> None:
    """After :func:`clear_overrides`, the next resolve falls back to
    the built-in default."""
    custom = _policy("scoped")
    register_policy(custom, connector_id="github")
    assert resolve_policy(connector_id="github", tenant=None, op=None) is custom

    clear_overrides()
    assert resolve_policy(connector_id="github", tenant=None, op=None) is get_default_policy()


def test_re_register_overwrites_previous_policy() -> None:
    """Registering twice on the same key uses the latest registration."""
    first = _policy("first")
    second = _policy("second")
    register_policy(first, connector_id="github")
    register_policy(second, connector_id="github")

    resolved = resolve_policy(connector_id="github", tenant=None, op=None)
    assert resolved is second


def test_wildcard_register_overrides_default_for_any_call() -> None:
    """``register_policy(policy)`` with no scope kwargs shadows the
    packaged default for every ``resolve_policy`` call (#1189).

    The wildcard slot is the documented "tenant-wide, connector-wide,
    op-wide" override (see ``register_policy`` docstring). Pre-fix,
    the resolver ladder did not include ``(None, None, None)``, so the
    registration was stored but never consulted -- ``resolve_policy``
    silently fell through to ``get_default_policy()`` regardless. The
    assertion targets every label shape (fully-labelled, partially-
    labelled, fully-``None``) because the wildcard slot's value
    proposition is "applies to every call when no more-specific
    override hits."
    """
    custom = _policy("global-wildcard")
    register_policy(custom)

    for connector_id, tenant, op in (
        ("github", "t1", "op.x"),
        ("gitlab", None, "op.y"),
        (None, "t2", None),
        (None, None, None),
    ):
        resolved = resolve_policy(connector_id=connector_id, tenant=tenant, op=op)
        assert resolved is custom, (
            f"wildcard register should win for "
            f"(connector_id={connector_id!r}, tenant={tenant!r}, op={op!r}); "
            f"got {resolved.id!r}"
        )


def test_connector_specific_override_beats_wildcard_register() -> None:
    """A more-specific override out-ranks the wildcard register (#1189).

    Specificity is the resolver's whole contract; the wildcard slot is
    the *lowest*-specificity override (one rung above the packaged
    default). A connector-anchored registration must still win for
    calls labelled with that connector. Calls without a matching
    more-specific override fall through to the wildcard.
    """
    global_policy = _policy("global-wildcard")
    github_policy = _policy("github-specific")
    register_policy(global_policy)
    register_policy(github_policy, connector_id="github")

    assert resolve_policy(connector_id="github", tenant="t1", op="op.x") is github_policy
    assert resolve_policy(connector_id="gitlab", tenant="t1", op="op.x") is global_policy
