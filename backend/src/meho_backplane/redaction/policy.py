# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Declarative redaction policy schema -- Initiative #805, Task #1070.

Pydantic models that parse a YAML policy file into a validated,
immutable :class:`RedactionPolicy`. The schema is one half of the
C1 deliverable; the engine in :mod:`.engine` consumes it.

Shape (mirrors the YAML):

.. code-block:: yaml

    id: default-connector-redaction
    version: 1
    description: |
      Tier-1 hot-path redaction for every connector response.
    rules:
      - name: strip-authorization-headers
        pattern: authorization_header
        action: redact
        reason: "RFC 7235 secret in header"
      - name: mask-uuids-in-github
        pattern: uuid
        action: mask
        scope:
          connector_id: github
        reason: "GitHub correlator UUIDs are tenant-stable"

**Action semantics** (consumed by :func:`engine.redact`):

* ``redact`` -- replace the matched span with a fixed marker
  (``"[REDACTED:<pattern>]"``). The default; appropriate for raw
  secrets where partial reveal is itself a leak.
* ``mask`` -- replace the matched span with a length-preserving
  asterisk run plus a short suffix (``"********a1b2"``). Useful when
  downstream consumers correlate by identifier shape but cannot see
  the value.
* ``hash`` -- replace the matched span with the prefix
  ``"sha256:<12-hex>"`` of the SHA-256 of the match. Stable across
  identical inputs so audit replay can compare hashed views without
  reissuing the secret.

**Why version-controlled and testable.** The parent initiative (#805
DoD) treats redaction policies as code: every rule has a unit-test
fixture, every policy round-trips raw -> redacted in CI (C1-d).
Pydantic's ``extra='forbid'`` + the field validators below catch the
typos and stale pattern names that would otherwise silently neuter a
rule.

**Boundary purity.** The model is frozen and side-effect-free.
:func:`load_policy_yaml` is the only I/O entry point. Callers that
need to compose policies in tests (or generate one from a tenant
record at runtime) use :meth:`RedactionPolicy.model_validate` against
a plain dict.
"""

from __future__ import annotations

from importlib import resources
from typing import Annotated, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from meho_backplane.redaction.patterns import PATTERN_NAMES

__all__ = [
    "RedactionAction",
    "RedactionMode",
    "RedactionPolicy",
    "RedactionPolicyError",
    "RedactionRule",
    "RedactionScope",
    "load_policy_yaml",
    "parse_policy",
]


#: Marker for the supported pattern actions; :class:`RedactionRule`
#: pins the literal so an unsupported value surfaces as a typed
#: ValidationError, and downstream pattern-matches over the union
#: stay exhaustive under ``mypy --strict``.
RedactionAction = Literal["redact", "mask", "hash"]

#: Whole-policy execution mode (G11.4-T4 #1073). ``enforce`` is the
#: default and the only mode that mutates the payload; ``shadow``
#: still runs the engine's detection walk and emits the manifest but
#: returns the original payload unmodified. Shadow / detection-only
#: mode exists so a new rule can be deployed and exercised against
#: real traffic (manifest counts visible in audit + dashboards)
#: before flipping it to ``enforce`` and risking an
#: over-redaction-driven incident. Implemented as a policy-level
#: flag rather than a per-call runtime argument so the choice
#: travels with the policy YAML: the C1-d round-trip fixture suite
#: picks the mode up from the policy under test, the resolver
#: returns a policy carrying its mode, and the middleware does not
#: need new threading.
RedactionMode = Literal["enforce", "shadow"]

#: Max length of a rule / policy identifier. Long enough for
#: human-readable slugs (``strip-authorization-headers``) plus a
#: tenant suffix; short enough that the audit-manifest entries the
#: engine emits stay compact.
_NAME_MAX_LENGTH: Final[int] = 96

#: Max length of a free-text ``reason`` field. The audit manifest
#: carries this verbatim into the C1-b audit row; capping prevents an
#: adversarial / pasted-novel policy from bloating audit storage.
_REASON_MAX_LENGTH: Final[int] = 512


class RedactionPolicyError(RuntimeError):
    """Raised when a redaction policy is malformed or incoherent.

    Mirrors the :class:`~meho_backplane.operations.ingest.catalog.CatalogError`
    posture: callers get one remediation-bearing exception type rather
    than handling :class:`yaml.YAMLError` and
    :class:`pydantic.ValidationError` separately.
    """


class RedactionScope(BaseModel):
    """Optional rule scope -- limits which calls a rule applies to.

    All three fields are optional and combined with **AND** semantics
    when present: a rule with ``connector_id='github'`` and
    ``op='issues.create'`` fires only when both labels match. Empty
    scope (the default) means "apply on every call".

    The engine receives the (connector_id, tenant, op) labels from the
    middleware (C1-b, #1071); this module just stores the predicate.
    Tenant is the OIDC ``sub`` / tenant id string, matching the type
    used in :mod:`meho_backplane.tenancy`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: Annotated[str | None, Field(max_length=_NAME_MAX_LENGTH)] = None
    tenant: Annotated[str | None, Field(max_length=_NAME_MAX_LENGTH)] = None
    op: Annotated[str | None, Field(max_length=_NAME_MAX_LENGTH)] = None

    def matches(
        self,
        *,
        connector_id: str | None,
        tenant: str | None,
        op: str | None,
    ) -> bool:
        """Return ``True`` when the scope predicate fires for these labels.

        Unset scope fields are wildcards. The engine calls this once per
        rule per payload; it is pure and allocation-free in the common
        case (all scope fields ``None`` -> short-circuit to ``True``).
        """
        if self.connector_id is not None and self.connector_id != connector_id:
            return False
        if self.tenant is not None and self.tenant != tenant:
            return False
        if self.op is not None and self.op != op:  # noqa: SIM103 -- explicit reads clearer than `not (... and ...)`
            return False
        return True


class RedactionRule(BaseModel):
    """One ``named pattern -> action`` binding inside a policy.

    The rule's ``name`` is the operator-facing handle (shows up in
    audit manifests; what a sibling task / runtime config flag toggles).
    The ``pattern`` MUST be one of :data:`patterns.PATTERN_NAMES`; the
    field validator rejects unknown names at parse time so a misspelled
    pattern fails policy load, not silently at first match.

    ``reason`` is a one-line operator-facing rationale, emitted verbatim
    into the audit manifest by the engine and consumed by C1-b for the
    audit row (#1071). Required because every redaction the auditor sees
    has to answer "why did this fire" without crawling git blame.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=_NAME_MAX_LENGTH)]
    pattern: Annotated[str, Field(min_length=1, max_length=_NAME_MAX_LENGTH)]
    action: RedactionAction = "redact"
    scope: RedactionScope = Field(default_factory=RedactionScope)
    reason: Annotated[str, Field(min_length=1, max_length=_REASON_MAX_LENGTH)]

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, value: str) -> str:
        """Strip + assert non-empty after strip.

        We do not enforce a strict slug shape (``[a-z0-9-]+``) here --
        the audit-manifest consumer surfaces names verbatim and an
        operator who wants ``StripAuth/2025`` should be able to keep
        it. The cap on length protects the audit row.
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("rule name must not be blank or whitespace-only")
        return normalized

    @field_validator("pattern")
    @classmethod
    def _pattern_is_known(cls, value: str) -> str:
        """Reject unknown pattern names with the known-set in the error.

        :data:`PATTERN_NAMES` is the source of truth; a stale rule
        referencing a removed/renamed pattern fails policy load with a
        list of valid replacements. Tier-2 (#1072) Presidio recognizers
        will register their names here too when that lands.
        """
        normalized = value.strip()
        if normalized not in PATTERN_NAMES:
            raise ValueError(
                f"unknown pattern {normalized!r}; known patterns: {', '.join(PATTERN_NAMES)}",
            )
        return normalized

    @field_validator("reason")
    @classmethod
    def _reason_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rule reason must not be blank or whitespace-only")
        return normalized


class RedactionPolicy(BaseModel):
    """A named, versioned bundle of redaction rules.

    ``id`` is the policy slug (per-tenant or system-wide); ``version``
    is a monotonically-increasing integer the C1-d round-trip CI gate
    pins to. A policy with no rules is rejected -- an empty policy is
    almost certainly a mistake and would silently let raw payloads
    through; the explicit error forces the author to either delete the
    file or write at least one rule.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=_NAME_MAX_LENGTH)]
    version: Annotated[int, Field(ge=1)]
    description: Annotated[str, Field(default="", max_length=2048)]
    rules: Annotated[tuple[RedactionRule, ...], Field(min_length=1)]
    mode: RedactionMode = "enforce"

    @field_validator("id")
    @classmethod
    def _id_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("policy id must not be blank or whitespace-only")
        return normalized

    @field_validator("rules")
    @classmethod
    def _rule_names_unique(cls, rules: tuple[RedactionRule, ...]) -> tuple[RedactionRule, ...]:
        """Reject duplicate ``rule.name`` within one policy.

        Duplicate names would make audit-manifest entries ambiguous
        (which rule fired?) and would let an operator silently shadow
        an earlier rule by re-defining the same name later in the
        file. Enforced here so the YAML diff is self-explanatory.
        """
        seen: set[str] = set()
        for rule in rules:
            if rule.name in seen:
                raise ValueError(f"duplicate rule name {rule.name!r} within policy")
            seen.add(rule.name)
        return rules


def parse_policy(raw: str) -> RedactionPolicy:
    """Parse + schema-validate a YAML policy string.

    Raises :class:`RedactionPolicyError` (never a bare ``yaml`` /
    ``pydantic`` error) so callers get one remediation-bearing exception
    type. The error message embeds the underlying validator output --
    pydantic's tree-form error already lists every offending field --
    so an operator pasting a malformed policy sees the path
    (``rules.2.pattern``) and the reason in one place.
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RedactionPolicyError(f"redaction policy is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RedactionPolicyError(
            "redaction policy must be a YAML mapping at the top level",
        )
    try:
        return RedactionPolicy.model_validate(data)
    except ValidationError as exc:
        raise RedactionPolicyError(
            f"redaction policy failed schema validation: {exc}",
        ) from exc


def load_policy_yaml(package: str, resource: str) -> RedactionPolicy:
    """Load + validate a packaged YAML policy via ``importlib.resources``.

    Mirrors the connector-spec catalog precedent
    (:func:`~meho_backplane.operations.ingest.catalog.load_catalog`):
    policies ship as package data colocated with their loader so a
    deployed wheel resolves them through
    :func:`importlib.resources.files` regardless of cwd.

    Parameters
    ----------
    package
        Dotted package name where the policy lives (e.g.
        ``"meho_backplane.redaction.policies"``).
    resource
        File name within *package* (e.g. ``"example.yaml"``).

    This is the only I/O entry point in the module; tests round-trip
    raw YAML strings through :func:`parse_policy` directly and never
    touch the filesystem.
    """
    raw = resources.files(package).joinpath(resource).read_text(encoding="utf-8")
    return parse_policy(raw)
