# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.redaction`` -- tiered connector-boundary redaction.

Initiative #805 (G11.4 Safety, C1). This package ships:

* The Tier-1 **schema + engine + named-pattern library** (#1070): a
  declarative YAML policy + deterministic regex engine targeting
  credential / infra-leak shapes.
* The connector-boundary **middleware** (#1071) that wires the engine
  into ``dispatcher._reduce_or_error`` with capture-raw +
  audit-storage.
* The **Tier-2 Microsoft Presidio adapter** for free-text NER
  (#1072), capability-flagged per policy. Policies with no ``tier2``
  block never load Presidio at runtime.

Public surface:

* :class:`RedactionPolicy` / :class:`RedactionRule` / :class:`RedactionScope`
  / :class:`Tier2Rule` -- declarative policy schema (Pydantic,
  frozen, ``extra='forbid'``).
* :class:`RedactionAction` -- ``Literal["redact", "mask", "hash"]``.
* :func:`parse_policy` / :func:`load_policy_yaml` -- YAML loaders.
* :func:`redact` -- the Tier-1 engine entry point; returns
  ``(redacted, manifest)``.
* :func:`apply_tier2` / :func:`get_engines` / :class:`Tier2NotAvailableError`
  -- the Tier-2 Presidio adapter entry points.
* :class:`RedactionResult` / :class:`RedactionManifestEntry` /
  :class:`Tier2Result` -- result shapes.
* :data:`PATTERN_NAMES` / :data:`PRESIDIO_SUPPORTED_ENTITIES` --
  the named-pattern + supported-entity catalogues.

The package is import-safe and side-effect-free; no global state, no
I/O at import time, no clocks. The Tier-2 adapter does its Presidio
import lazily on first call so importing this package costs nothing
beyond stdlib + pydantic + the C1-a regex tier.
"""

from meho_backplane.redaction.engine import (
    RedactionManifestEntry,
    RedactionResult,
    redact,
)
from meho_backplane.redaction.middleware import (
    RedactionMiddlewareResult,
    Tier2NotAvailableError,
    apply_connector_boundary_redaction,
    manifest_to_audit_payload,
    normalize_for_audit,
)
from meho_backplane.redaction.patterns import NAMED_PATTERNS, PATTERN_NAMES, get_pattern
from meho_backplane.redaction.policy import (
    PRESIDIO_DEFAULT_ENTITIES,
    PRESIDIO_SUPPORTED_ENTITIES,
    RedactionAction,
    RedactionMode,
    RedactionPolicy,
    RedactionPolicyError,
    RedactionRule,
    RedactionScope,
    Tier2Rule,
    load_policy_yaml,
    parse_policy,
)
from meho_backplane.redaction.presidio import (
    DEFAULT_SPACY_MODEL,
    Tier2EnginePair,
    Tier2Result,
    apply_tier2,
    clear_engine_cache,
    get_engines,
    policy_uses_tier2,
)
from meho_backplane.redaction.resolver import (
    DEFAULT_POLICY_PACKAGE,
    DEFAULT_POLICY_RESOURCE,
    clear_overrides,
    find_policy_by_id,
    get_default_policy,
    register_policy,
    resolve_policy,
)

__all__ = [
    "DEFAULT_POLICY_PACKAGE",
    "DEFAULT_POLICY_RESOURCE",
    "DEFAULT_SPACY_MODEL",
    "NAMED_PATTERNS",
    "PATTERN_NAMES",
    "PRESIDIO_DEFAULT_ENTITIES",
    "PRESIDIO_SUPPORTED_ENTITIES",
    "RedactionAction",
    "RedactionManifestEntry",
    "RedactionMiddlewareResult",
    "RedactionMode",
    "RedactionPolicy",
    "RedactionPolicyError",
    "RedactionResult",
    "RedactionRule",
    "RedactionScope",
    "Tier2EnginePair",
    "Tier2NotAvailableError",
    "Tier2Result",
    "Tier2Rule",
    "apply_connector_boundary_redaction",
    "apply_tier2",
    "clear_engine_cache",
    "clear_overrides",
    "find_policy_by_id",
    "get_default_policy",
    "get_engines",
    "get_pattern",
    "load_policy_yaml",
    "manifest_to_audit_payload",
    "normalize_for_audit",
    "parse_policy",
    "policy_uses_tier2",
    "redact",
    "register_policy",
    "resolve_policy",
]
