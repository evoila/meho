# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.redaction`` -- Tier-1 connector-boundary redaction.

Initiative #805 (G11.4 Safety, C1), Task #1070 (T1). This package
ships the **schema + engine + named-pattern library** half of the
sanitization deliverable; the middleware that wires the engine into
``dispatcher._reduce_or_error`` lives in #1071 (C1-b), and the Tier-2
Microsoft Presidio adapter for free-text NER lives in #1072 (C1-c).

Public surface:

* :class:`RedactionPolicy` / :class:`RedactionRule` / :class:`RedactionScope`
  -- declarative policy schema (Pydantic, frozen, ``extra='forbid'``).
* :class:`RedactionAction` -- ``Literal["redact", "mask", "hash"]``.
* :func:`parse_policy` / :func:`load_policy_yaml` -- YAML loaders.
* :func:`redact` -- the engine entry point; returns ``(redacted, manifest)``.
* :class:`RedactionResult` / :class:`RedactionManifestEntry` -- result shapes.
* :data:`PATTERN_NAMES` -- the named-pattern catalogue.

The package is import-safe and side-effect-free; no global state, no
I/O at import time, no clocks. Callers may import it from anywhere
without lifecycle concerns.
"""

from meho_backplane.redaction.engine import (
    RedactionManifestEntry,
    RedactionResult,
    redact,
)
from meho_backplane.redaction.patterns import NAMED_PATTERNS, PATTERN_NAMES, get_pattern
from meho_backplane.redaction.policy import (
    RedactionAction,
    RedactionPolicy,
    RedactionPolicyError,
    RedactionRule,
    RedactionScope,
    load_policy_yaml,
    parse_policy,
)

__all__ = [
    "NAMED_PATTERNS",
    "PATTERN_NAMES",
    "RedactionAction",
    "RedactionManifestEntry",
    "RedactionPolicy",
    "RedactionPolicyError",
    "RedactionResult",
    "RedactionRule",
    "RedactionScope",
    "get_pattern",
    "load_policy_yaml",
    "parse_policy",
    "redact",
]
