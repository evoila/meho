# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vCenter spec-file resolver for the G0.7 canary acceptance test.

The vSphere OpenAPI specs (``vcenter.yaml``, ~7.5 MB; ``vi-json.yaml``,
~10 MB) are checked into the consumer's separate spec-shelf repo
rather than ``evoila/meho`` itself, so the public chassis repo stays
free of the vendor-licensed schema corpus. This helper resolves a
local path to each spec by checking, in priority order:

1. The explicit ``MEHO_VCENTER_OPENAPI_VCENTER`` / ``MEHO_VCENTER_OPENAPI_VI_JSON``
   env vars. CI sets these from the runner's checkout of the
   spec-shelf repo.
2. The legacy ``MEHO_VCENTER_OPENAPI`` env var pointing at a
   ``vcenter.yaml`` (or its directory). Compatibility with the
   pre-canary integration test (#393's
   ``tests/integration/test_operations_ingest_vcenter.py``).
3. The directory pointed at by ``MEHO_CONSUMER_DOCS_ROOT`` which
   is expected to contain ``vcenter-9.0/vcenter.yaml`` and
   ``vcenter-9.0/vi-json.yaml``. Local-dev convenience for the
   maintainer with the consumer repo cloned at a known sibling path.

When no source resolves, the test skips with a pointer to this
docstring rather than failing — the canary verifies a substrate that
operators run from their own deploys, and the acceptance criterion is
re-evaluated in CI where the env vars are wired up. CI green stays
the operator-visible signal.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "VCENTER_SPEC_REASON",
    "VI_JSON_PARAMETER_REF_LIMITATION",
    "resolve_vcenter_yaml",
    "resolve_vi_json_yaml",
]

#: Documented reason the test skips when no spec source is configured.
#: Embedded into the ``pytest.skip`` call so CI logs make the missing
#: env var traceable to this helper.
VCENTER_SPEC_REASON = (
    "vCenter OpenAPI spec not configured. Set MEHO_VCENTER_OPENAPI_VCENTER + "
    "MEHO_VCENTER_OPENAPI_VI_JSON to absolute paths, or set MEHO_CONSUMER_DOCS_ROOT "
    "to a directory containing vcenter-9.0/{vcenter.yaml,vi-json.yaml}. "
    "See tests/acceptance/_vcenter_spec.py for the resolver contract."
)

#: Reason ``vi-json.yaml`` is currently not ingested by the canary.
#: Surfaced in the test docstring + canary doc so operators following
#: the procedure understand why the second spec is gated on a
#: follow-up parser extension. Filed as a follow-up ticket from the
#: PR body for issue #408 — `vi-json.yaml` uses
#: `$ref: '#/components/parameters/moId'` on every operation, which
#: the T1 parser explicitly rejects (`refs.py` line ~93). The fix is
#: small but lives in T1's scope, not T8's acceptance work.
VI_JSON_PARAMETER_REF_LIMITATION = (
    "vi-json.yaml ingest is blocked on T1 parameter-ref resolver support "
    "(uses $ref to #/components/parameters/moId on every operation). "
    "Tracked as a follow-up ticket; the canary still proves end-to-end "
    "for the vcenter.yaml spec corpus (~1275 operations)."
)


def _expand_optional_path(value: str | None) -> Path | None:
    """Return a :class:`Path` for *value* iff it resolves to an existing file."""
    if value is None:
        return None
    candidate = Path(value).expanduser()
    return candidate if candidate.is_file() else None


def resolve_vcenter_yaml() -> Path | None:
    """Return the local path to ``vcenter.yaml``, or ``None`` if unconfigured.

    Checks the env vars described in the module docstring, in priority
    order. Returns ``None`` when nothing is configured so callers can
    convert that into a ``pytest.skip`` with :data:`VCENTER_SPEC_REASON`.
    """
    explicit = _expand_optional_path(os.getenv("MEHO_VCENTER_OPENAPI_VCENTER"))
    if explicit is not None:
        return explicit
    legacy = os.getenv("MEHO_VCENTER_OPENAPI")
    if legacy:
        legacy_path = Path(legacy).expanduser()
        if legacy_path.is_file():
            return legacy_path
        if legacy_path.is_dir() and (legacy_path / "vcenter.yaml").is_file():
            return legacy_path / "vcenter.yaml"
    consumer_root = os.getenv("MEHO_CONSUMER_DOCS_ROOT")
    if consumer_root:
        candidate = Path(consumer_root).expanduser() / "vcenter-9.0" / "vcenter.yaml"
        if candidate.is_file():
            return candidate
    return None


def resolve_vi_json_yaml() -> Path | None:
    """Return the local path to ``vi-json.yaml``, or ``None`` if unconfigured.

    Same resolver chain as :func:`resolve_vcenter_yaml` but for the
    Managed-Object JSON spec shelf. Currently unused by the canary
    (see :data:`VI_JSON_PARAMETER_REF_LIMITATION`); kept here so the
    follow-up parser-extension ticket can flip the test on with one
    code change.
    """
    explicit = _expand_optional_path(os.getenv("MEHO_VCENTER_OPENAPI_VI_JSON"))
    if explicit is not None:
        return explicit
    consumer_root = os.getenv("MEHO_CONSUMER_DOCS_ROOT")
    if consumer_root:
        candidate = Path(consumer_root).expanduser() / "vcenter-9.0" / "vi-json.yaml"
        if candidate.is_file():
            return candidate
    return None
