# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Product-agnostic spec-shelf resolver + reconcile-assert harness (#2980).

Vendor API specs are pinned in the operator's private spec-shelf repo
(``evoila-bosnia/claude-rdc-hetzner-dc``, ``docs/<product>-<version>/``)
rather than this public chassis repo, so the vendor-licensed schema
corpus never lands in public history. CI provisions the shelf through a
secret-gated sparse checkout and points ``MEHO_CONSUMER_DOCS_ROOT`` at
it (see ``docs/decisions/vendor-spec-ci-provisioning.md``); local dev
sets the same env var at a full shelf clone.

This module generalises the vcenter-only resolver
(:mod:`tests.acceptance._vcenter_spec`, which now delegates its
shelf-root branch here) into the harness every per-vendor reconcile
lane builds on (#2981-#2993). A lane is three calls:

    spec_path = require_shelf_spec("nsx-9.0", "nsx-openapi.json")
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(declared, served, spec_label="nsx-9.0/nsx-openapi.json")

Skip semantics are uniform: when ``MEHO_CONSUMER_DOCS_ROOT`` is unset,
or the product directory / spec file is absent from the configured
shelf (e.g. CI's sparse checkout has not been widened to that product
yet), :func:`require_shelf_spec` skips with a reason naming the exact
missing file and this module — never fails. Wherever the shelf is
wired, the lane runs for real; a red lane there is the guard surfacing
a real finding, not harness noise (the #2944/#2970 protocol — see
``docs/decisions/spec-reconcile-guards-standard.md``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meho_backplane.operations.ingest import parse_openapi

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

__all__ = [
    "SHELF_ENV_VAR",
    "assert_op_ids_served",
    "openapi_served_op_ids",
    "require_shelf_spec",
    "resolve_shelf_file",
    "shelf_skip_reason",
]

#: The single env var every shelf-backed lane resolves through. CI sets
#: it unconditionally on both Python jobs; when the secret-gated shelf
#: checkout was skipped the directory does not exist and lanes skip.
SHELF_ENV_VAR = "MEHO_CONSUMER_DOCS_ROOT"

#: Hint cap per missing entry in :func:`assert_op_ids_served` failures.
_NEAR_MISS_LIMIT = 3


def shelf_skip_reason(product_dir: str, filename: str) -> str:
    """Uniform skip reason for an unconfigured / unprovisioned shelf spec."""
    return (
        f"{product_dir}/{filename} spec not configured. Set {SHELF_ENV_VAR} to a "
        f"spec-shelf docs directory containing {product_dir}/{filename}. "
        "See tests/_spec_shelf.py for the resolver contract."
    )


def resolve_shelf_file(product_dir: str, filename: str) -> Path | None:
    """Return the local path to ``<shelf>/<product_dir>/<filename>``, or ``None``.

    ``None`` covers every unprovisioned shape the same way: env var
    unset, shelf root missing, product directory absent (sparse
    checkout not widened), or the file itself absent. Callers convert
    ``None`` into a ``pytest.skip`` with :func:`shelf_skip_reason` —
    or use :func:`require_shelf_spec`, which does both.
    """
    shelf_root = os.getenv(SHELF_ENV_VAR)
    if not shelf_root:
        return None
    candidate = Path(shelf_root).expanduser() / product_dir / filename
    return candidate if candidate.is_file() else None


def require_shelf_spec(product_dir: str, filename: str) -> Path:
    """Resolve a shelf spec file or skip the calling test with the uniform reason."""
    resolved = resolve_shelf_file(product_dir, filename)
    if resolved is None:
        pytest.skip(shelf_skip_reason(product_dir, filename))
    return resolved


def openapi_served_op_ids(spec_path: Path, *, spec_source: str | None = None) -> set[str]:
    """Parse an OpenAPI spec file into the set of served ``METHOD:/path`` op_ids.

    Runs the file through the real ingest parser
    (:func:`~meho_backplane.operations.ingest.parse_openapi`), so the
    returned strings are byte-for-byte the ``endpoint_descriptor.op_id``
    values an operator's ingest of the same spec would produce — the
    exact strings dispatch pre-flight resolves against. ``content=``
    feeds the bytes verbatim (the https-only SSRF guard applies only to
    the URL-fetch path); the ``file://`` URI is just the audit label.
    """
    spec_text = spec_path.read_text(encoding="utf-8")
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source=spec_source or f"spec:{spec_path.name}",
        content=spec_text,
    )
    return {row.op_id for row in rows}


def _near_misses(op_id: str, served: Collection[str]) -> list[str]:
    """Served op_ids sharing the missing path's last literal segment.

    The #2970 repoint pattern: a hand-coded path is usually wrong in its
    prefix (renamed API family, wrong version root), while its resource
    name survives somewhere in the real spec — e.g. the unserved
    ``GET:/vcenter/network/distributed-switches`` had its resource under
    ``/vcenter/namespace-management/…/distributed-switches``. Surfacing
    those candidates turns a bare failure into a grounded repoint lead.
    """
    _, _, path = op_id.partition(":")
    literal_segments = [
        segment
        for segment in path.partition("?")[0].split("/")
        if segment and not (segment.startswith("{") and segment.endswith("}"))
    ]
    if not literal_segments:
        return []
    needle = literal_segments[-1]
    return sorted(candidate for candidate in served if needle in candidate)[:_NEAR_MISS_LIMIT]


def assert_op_ids_served(
    declared: Iterable[str],
    served: Collection[str],
    *,
    spec_label: str,
) -> None:
    """Fail with an actionable diff unless every declared op_id is served.

    *declared* is the lane's enumeration of hand-coded ``METHOD:/path``
    strings (introspected from the connector's live constants, never a
    hardcoded mirror); *served* is the pinned spec's op_id set, usually
    from :func:`openapi_served_op_ids` (union the sets when a product
    pins multiple specs). An empty *declared* fails rather than passing
    vacuously — a lane that enumerates nothing guards nothing.

    The failure message lists each unserved entry with up to
    3 near-miss candidates from the served set, plus the
    red-lane-is-a-finding protocol pointer. Never invent a repoint: fix
    paths only to targets the pinned spec actually serves.
    """
    declared_set = set(declared)
    if not declared_set:
        pytest.fail(
            f"reconcile lane for {spec_label} declared no op_ids — the enumeration "
            "wiring broke (a vacuously green lane guards nothing)."
        )
    missing = declared_set - set(served)
    if not missing:
        return

    lines = [
        f"{len(missing)} hand-coded op_id(s) are not served by the pinned {spec_label}:",
        "",
    ]
    for op_id in sorted(missing):
        lines.append(f"  {op_id}")
        candidates = _near_misses(op_id, served)
        if candidates:
            lines.extend(f"    near miss: {candidate}" for candidate in candidates)
        else:
            lines.append("    near miss: none — no served path shares its resource name")
    lines += [
        "",
        "A red lane here is the guard surfacing a real finding (a typo, a renamed "
        "endpoint, or a wrong-API-version path — the defect class fixture-based "
        "tests cannot catch). Repoint each op_id at a path the pinned spec "
        "actually serves; never invent one. Protocol: "
        "docs/decisions/spec-reconcile-guards-standard.md.",
    ]
    pytest.fail("\n".join(lines), pytrace=False)
