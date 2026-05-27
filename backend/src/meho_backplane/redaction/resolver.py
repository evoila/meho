# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-call redaction policy selection -- Initiative #805 (C1-b, #1071).

The connector-boundary middleware needs to answer "which
:class:`~meho_backplane.redaction.policy.RedactionPolicy` applies to
*this* call?" before it can run the engine. The resolution dimensions
are, in order of specificity:

1. ``(connector_id, tenant, op)`` -- the most specific override.
2. ``(connector_id, op)`` -- per-connector, per-op (tenant wildcard).
3. ``(connector_id, tenant)`` -- per-connector, per-tenant.
4. ``connector_id`` -- per-connector default.
5. ``tenant`` -- tenant-wide default across every connector.
6. ``(None, None, None)`` -- the wildcard global override registered
   via ``register_policy(policy)`` with no scope kwargs.

When no override at any of those levels matches, the resolver falls
through to the conservative default-safe policy packaged at
:mod:`meho_backplane.redaction.policies` (:data:`DEFAULT_POLICY_RESOURCE`).
**Pass-through is never the answer** -- the parent goal (#800) treats
the API surface as the trust boundary, so an un-configured
operator-facing connector still gets credentials stripped out of its
responses.

Registration is process-global state, deliberately mutable so the
middleware can be exercised in tests without monkeypatching:
:func:`register_policy` adds an override at one of the specificity
levels above; :func:`clear_overrides` resets to the built-in default
only. Tests must call :func:`clear_overrides` in a fixture teardown
to keep registrations from leaking across the file. Production runs
without any overrides today -- the per-tenant policy authoring path
is a follow-on Initiative; this module ships the resolution shape so
the policy column can land on the audit row and middleware against
it can be unit-tested.

The module owns one lazy global -- :func:`get_default_policy` --
which loads the packaged YAML on first access and caches it. The
load is side-effect-free at import time so this module stays as cheap
to import as :mod:`meho_backplane.redaction.policy` itself.
"""

from __future__ import annotations

import threading
from typing import Final

from meho_backplane.redaction.policy import RedactionPolicy, load_policy_yaml

__all__ = [
    "DEFAULT_POLICY_PACKAGE",
    "DEFAULT_POLICY_RESOURCE",
    "clear_overrides",
    "find_policy_by_id",
    "get_default_policy",
    "register_policy",
    "resolve_policy",
]


#: Dotted package the default policy YAML lives in. Mirrors the
#: ``meho_backplane.operations.ingest`` precedent: policy files travel
#: with the wheel via ``importlib.resources``.
DEFAULT_POLICY_PACKAGE: Final[str] = "meho_backplane.redaction.policies"

#: File name within :data:`DEFAULT_POLICY_PACKAGE` for the default-safe
#: policy. The conservative defaults are documented in the YAML header.
DEFAULT_POLICY_RESOURCE: Final[str] = "default.yaml"


# A tuple key encodes the override specificity:
# ``(connector_id, tenant, op)``. ``None`` means "wildcard at this
# dimension". The resolver walks from most-specific to least-specific
# (six lookups) and returns the first hit. This is a tiny table in
# practice -- per-tenant operator authoring is the only realistic
# population path -- so a flat dict is the right shape.
_OverrideKey = tuple[str | None, str | None, str | None]


_overrides: dict[_OverrideKey, RedactionPolicy] = {}
_overrides_lock: threading.Lock = threading.Lock()
_default_policy: RedactionPolicy | None = None
_default_lock: threading.Lock = threading.Lock()


def get_default_policy() -> RedactionPolicy:
    """Return the packaged default-safe :class:`RedactionPolicy`.

    Loaded lazily on first access and cached for the lifetime of the
    process. The YAML lives in
    :data:`DEFAULT_POLICY_PACKAGE`/:data:`DEFAULT_POLICY_RESOURCE` and
    is resolved via :func:`importlib.resources.files`, so a packaged
    wheel finds it regardless of cwd. The lock protects against two
    threads racing the first load -- :func:`load_policy_yaml` is pure
    but the file read is not free, and the cached :class:`RedactionPolicy`
    is frozen so post-load there is no reason to take the lock again.
    """
    global _default_policy
    if _default_policy is not None:
        return _default_policy
    with _default_lock:
        if _default_policy is None:
            _default_policy = load_policy_yaml(
                DEFAULT_POLICY_PACKAGE,
                DEFAULT_POLICY_RESOURCE,
            )
    return _default_policy


def register_policy(
    policy: RedactionPolicy,
    *,
    connector_id: str | None = None,
    tenant: str | None = None,
    op: str | None = None,
) -> None:
    """Register *policy* as an override for the given specificity tuple.

    A call to :func:`resolve_policy` whose labels match *all three* of
    the non-``None`` parameters (treating ``None`` as a wildcard at
    this dimension) returns *policy* before the resolver falls through
    to a less-specific override or the built-in default. All three
    parameters defaulting to ``None`` is the "tenant-wide, connector-
    wide, op-wide" override -- effectively replacing the built-in
    default.

    Re-registration on the same key overwrites the previous entry
    without warning; tests that swap policies between cases should do
    so inside their own fixture teardown so the mutation is visible
    in the fixture scope, not as cross-test ambient state.
    """
    key: _OverrideKey = (connector_id, tenant, op)
    with _overrides_lock:
        _overrides[key] = policy


def clear_overrides() -> None:
    """Drop every registered override; the next :func:`resolve_policy`
    call falls through to the built-in default for every input.

    Test-only API. Production callers do not run with mid-process
    overrides today; the per-tenant policy authoring path will land
    as part of a follow-on Initiative and likely register at app
    startup rather than mid-request.
    """
    with _overrides_lock:
        _overrides.clear()


def find_policy_by_id(policy_id: str) -> RedactionPolicy | None:
    """Return the registered :class:`RedactionPolicy` whose ``id`` matches.

    Walks the registered overrides + the packaged default and returns
    the first policy whose ``id`` equals *policy_id*. Returns ``None``
    when no policy with that id is registered (typically because the
    YAML file containing it has been retired since the audit row was
    written, or the row was written by a different deployment).

    The policy-replay sense (G11.4-T5 #1074) consumes this: an audit
    row records :attr:`~meho_backplane.db.models.AuditLog.payload`'s
    ``redaction_policy_id`` field; the replayer looks the policy up by
    that id and re-runs :func:`~meho_backplane.redaction.engine.redact`
    against the row's ``raw_payload``, verifying the engine still
    produces the recorded manifest. A policy id that no longer
    resolves is a structurally explicit "cannot replay" signal --
    distinct from "the engine produced a different manifest" -- so
    the consumer can render an actionable diagnosis rather than a
    silent pass.

    The lookup acquires the overrides lock briefly to snapshot the
    table values; the default policy is cached on first access. The
    returned policy is immutable (Pydantic ``frozen=True``) so the
    caller can hold the reference past the lock without further
    synchronisation. O(n) over the override table is fine: the table
    has at most a handful of overrides in v0.2 (the per-tenant
    authoring path is a follow-on Initiative; production today runs
    on the packaged default).
    """
    with _overrides_lock:
        for policy in _overrides.values():
            if policy.id == policy_id:
                return policy
    default = get_default_policy()
    if default.id == policy_id:
        return default
    return None


def resolve_policy(
    *,
    connector_id: str | None,
    tenant: str | None,
    op: str | None,
) -> RedactionPolicy:
    """Return the :class:`RedactionPolicy` that applies to this call.

    Walks the specificity ladder documented on the module: the most
    specific override that matches the call's labels wins. ``None``
    on a label is treated as "no value at this dimension" -- a
    tenant-less call (uncommon outside tests) only matches overrides
    whose ``tenant`` is also ``None``. Falls through to
    :func:`get_default_policy` when no override matches.

    The resolver is read-only and acquires the overrides lock only
    briefly to snapshot the relevant keys. The returned policy is
    immutable (Pydantic ``frozen=True``) so the caller can hold the
    reference past the lock without further synchronisation.
    """
    # Order matters -- most specific first. Each tuple corresponds
    # to one specificity level documented on the module docstring.
    # The wildcards are encoded as ``None`` so the resolver loop stays
    # a straight dict lookup; no per-call regex or per-call walk over
    # the table is needed. The final ``(None, None, None)`` step is the
    # wildcard-global override slot: ``register_policy(policy)`` with no
    # scope kwargs stores under that key, and a hit there shadows the
    # packaged default (#1189; the entry was missing pre-fix, so a
    # wildcard registration was stored but never looked up).
    ladder: tuple[_OverrideKey, ...] = (
        (connector_id, tenant, op),
        (connector_id, None, op),
        (connector_id, tenant, None),
        (connector_id, None, None),
        (None, tenant, None),
        (None, None, None),
    )
    with _overrides_lock:
        for key in ladder:
            hit = _overrides.get(key)
            if hit is not None:
                return hit
    return get_default_policy()
