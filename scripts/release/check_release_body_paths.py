#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Assert every ``/api/v*`` path cited in a release body resolves in OpenAPI.

Sister gate to ``cli-api-snapshot-freshness`` (#928) — but at
release-time rather than PR-time. The PR-time gate catches "the
OpenAPI snapshot lags the route table"; this one catches "the
release body advertises a path the route table doesn't actually
expose".

Background
----------

Three consecutive releases shipped with paths in the prose that
didn't match the routes the operators got:

* v0.5.0: CHANGELOG ``[Unreleased]`` → ``[0.5.0]`` roll skipped
  entirely, so the GitHub Release fell back to a hand-curated body
  that wasn't audited against shipped routes.
* v0.5.1: release body said the connector raw-REST on-ramp answered
  v0.3.0's "only 13 vmware ops?" — but the path it gave operators
  was the read-only catalog (``/api/v1/connectors/catalog``,
  introduced by #743), not live typed-connector dispatch.
* v0.6.0: release body cited ``GET /api/v1/audit/replay`` (actual
  shipped path:
  ``GET /api/v1/audit/sessions/{session_id}/replay``, introduced by
  #1012) and described 6 ``tenant_conventions`` routes under a
  ``tenant-`` prefix that doesn't exist (actual: 3 routes under
  ``/api/v1/conventions``, no ``tenant-`` prefix).

The pattern is cross-cycle and stable, so a release-time CI-style
gate is the right answer. It runs once per release, against the
proposed release body and the published OpenAPI snapshot, and
refuses to let the release proceed when any cited path doesn't
exist in the snapshot.

Mechanism
---------

1. Read the proposed release body (a markdown file passed via
   ``--release-body``).
2. Extract every token shaped like ``/api/v<N>/<segment>...`` from
   the prose. A token ends at the first character that wouldn't be
   legal in a URL path (whitespace, backtick, paren, etc.).
3. For each extracted token, derive its **template form** — the
   path with literal segments preserved and concrete IDs (UUIDs,
   integers, slugs in the form ``{name}``) replaced by the OpenAPI
   path-template placeholders.
4. For each template form, assert it exists in the OpenAPI snapshot
   passed via ``--openapi-snapshot``. The snapshot is the same
   ``cli/api/openapi.json`` the #928 freshness gate produces.
5. Exit 0 on full match; exit 1 with a diagnostic listing every
   path that didn't resolve.

The template-derivation step is what makes the gate useful: when a
release body says

    ``GET /api/v1/audit/sessions/abc-123/replay``

we want to match it against the OpenAPI path

    ``/api/v1/audit/sessions/{session_id}/replay``

A strict literal-match gate would have flagged the legitimate
citation as broken; a template-aware gate flags only the actually-
drifted citations.

Whitelist
---------

The ``--allow-path`` flag accepts a path pattern (literal or
templatised) that the gate should treat as resolved even when it
isn't in the OpenAPI snapshot. Use sparingly — examples include
paths that ship via a different surface than the FastAPI app
(e.g. a planned ``/api/v2/...`` path mentioned for forward-
compatibility, or a path served by a sibling service).

Exit codes
----------

* ``0`` — every cited path resolves (or matches a whitelist entry).
* ``1`` — at least one cited path failed to resolve; report on
  stderr lists the cited token, the derived template, and the
  closest OpenAPI prefix match (when one exists).
* ``2`` — argument / file / JSON parse error.

Usage
-----

::

    python3 scripts/release/check_release_body_paths.py \\
        --release-body /tmp/v0.6.0-release-body.md \\
        --openapi-snapshot cli/api/openapi.json

    # whitelist a path that ships via a different surface:
    python3 scripts/release/check_release_body_paths.py \\
        --release-body /tmp/v0.7.0-release-body.md \\
        --openapi-snapshot cli/api/openapi.json \\
        --allow-path /api/v2/projected-future-path

The script is invoked by ``/release`` skill step 2.5 (between
CHANGELOG roll and tag) — see
``docs/codebase/release-body-freshness.md`` for the integration
point.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Final

#: Regex that matches the broadest legal-looking ``/api/v<N>/...``
#: token in markdown prose. The character class deliberately stops
#: at whitespace, common markdown delimiters (`` ` ``, ``)``, ``]``,
#: ``"``, ``'``, ``>``, ``<``, ``,``), and end-of-string. Trailing
#: punctuation (``.``, ``;``, ``:``) is stripped post-match to
#: tolerate "the route ``/api/v1/foo``." with a sentence-final dot
#: that isn't part of the path.
_PATH_TOKEN_RE: Final = re.compile(r"/api/v[0-9]+/[A-Za-z0-9_/{}\-.]*")

#: Trailing characters that aren't part of any real path but commonly
#: appear in prose after a path citation. Stripped post-extraction.
#:
#: Notably absent: ``}`` (legitimate trailing char for an OpenAPI path
#: template like ``/api/v1/foo/{slug}``) and ``/`` (kept so a trailing
#: slash matches a snapshot with or without one). Including ``\\``
#: defends against double-escaped citations in JSON code-block prose;
#: ``>`` defends against ``GET /api/v1/foo>`` markdown blockquote
#: trailing.
_TRAILING_PUNCT: Final = ".,;:!?\\)]>'\""

#: A 36-char UUID at any segment position is replaced by ``{id}``.
_UUID_LIKE_RE: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: A pure-digit segment (length ≥ 1) is replaced by ``{id}``.
_DIGITS_RE: Final = re.compile(r"^[0-9]+$")


def _strip_trailing_punct(token: str) -> str:
    """Drop sentence-final punctuation that markdown leaves attached."""
    while token and token[-1] in _TRAILING_PUNCT:
        token = token[:-1]
    return token


def _is_uuid_like(seg: str) -> bool:
    """Match the canonical 36-char UUID shape with dashes."""
    if _UUID_LIKE_RE.match(seg):
        # Belt-and-braces: ``uuid.UUID`` will raise on near-misses
        # (e.g. wrong nibble count in a section) the regex permits.
        try:
            uuid.UUID(seg)
        except ValueError:
            return False
        return True
    return False


def _templatise(path: str, openapi_templates: set[str]) -> set[str]:
    """Derive the set of template forms a citation could resolve to.

    Returns a set of candidate templates because the same concrete
    citation can collide with multiple OpenAPI templates (e.g.
    ``/api/v1/foo/bar`` could match a literal ``/api/v1/foo/bar`` or
    a templated ``/api/v1/foo/{name}``).

    The strategy:

    1. The original path string is always a candidate (so a literal
       OpenAPI path matches without surgery).
    2. UUID-shaped segments are replaced by ``{<param>}`` using the
       OpenAPI snapshot's parameter naming for that position when
       available.
    3. Pure-digit segments are replaced by ``{<param>}`` likewise.
    4. Segments wrapped in ``{...}`` (already a template form) are
       kept verbatim.

    For (2) and (3) we don't know which OpenAPI parameter name to
    use (the same shape could be ``{id}``, ``{audit_id}``,
    ``{target_id}``...), so we generate every form that the OpenAPI
    snapshot's existing templates suggest at that segment position.
    """
    candidates: set[str] = {path}

    parts = path.split("/")
    # Per-position parameter-name pool: at index ``i`` (1-based,
    # since ``parts[0]`` is the empty string before the leading
    # ``/``), what parameter names does the OpenAPI snapshot use at
    # that exact position?  Build the pool once.
    param_names_by_position: dict[int, set[str]] = {}
    for tmpl in openapi_templates:
        tmpl_parts = tmpl.split("/")
        for i, seg in enumerate(tmpl_parts):
            if seg.startswith("{") and seg.endswith("}"):
                param_names_by_position.setdefault(i, set()).add(seg)

    # Build a list of (index, segment-replacement-options) tuples
    # for every position that needs templating.
    replacements: list[tuple[int, list[str]]] = []
    for i, seg in enumerate(parts):
        if seg.startswith("{") and seg.endswith("}"):
            # Already templated by the author — preserve.
            replacements.append((i, [seg]))
            continue
        if _is_uuid_like(seg) or _DIGITS_RE.match(seg):
            # ID-like — pool the snapshot's parameter names at this
            # position, and fall back to ``{id}`` if the snapshot
            # has no template at this position (so a citation
            # against a path the snapshot doesn't know about can
            # still expand). The literal segment is intentionally
            # NOT preserved as an option here — IDs in a release
            # body are concrete instances, not literal route bits.
            pool = sorted(param_names_by_position.get(i, set()) or {"{id}"})
            replacements.append((i, pool))

    if not replacements:
        return candidates

    # Cartesian product of replacement options. Cap at 100 to keep
    # the candidate set bounded — a single citation should never
    # produce more than a few combinations in practice; the cap is
    # just a guard against a pathological release body that puts
    # many ID-shaped segments in one line.
    expansions: list[list[str]] = [list(parts)]
    for idx, options in replacements:
        new_expansions: list[list[str]] = []
        for prev in expansions:
            for opt in options:
                replaced = list(prev)
                replaced[idx] = opt
                new_expansions.append(replaced)
                if len(new_expansions) >= 100:
                    break
            if len(new_expansions) >= 100:
                break
        expansions = new_expansions
        if len(expansions) >= 100:
            break

    for exp in expansions:
        candidates.add("/".join(exp))

    return candidates


def extract_paths(release_body: str) -> list[str]:
    """Return every ``/api/v*`` token found in ``release_body``, deduped.

    Order-preserving (first occurrence wins) so the diagnostic
    output reads in document order.
    """
    seen: dict[str, None] = {}
    for raw in _PATH_TOKEN_RE.findall(release_body):
        cleaned = _strip_trailing_punct(raw)
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen.keys())


def load_openapi_paths(snapshot_path: Path) -> set[str]:
    """Load the ``paths`` keys from an OpenAPI snapshot JSON file.

    Raises:
        FileNotFoundError: if ``snapshot_path`` doesn't exist.
        ValueError: if the JSON is malformed or has no ``paths``
            top-level key.
    """
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - simple wrap
        raise ValueError(f"{snapshot_path}: invalid JSON ({exc})") from exc

    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"{snapshot_path}: missing or non-object 'paths' key")

    return set(paths.keys())


def _closest_template(citation: str, openapi_templates: set[str]) -> str | None:
    """Return the most-likely-relevant OpenAPI template for ``citation``.

    Heuristic: rank by (longest shared prefix, last-segment match)
    so a drifted citation like ``/api/v1/audit/replay`` against a
    snapshot that contains both
    ``/api/v1/audit/sessions/{session_id}/replay`` and
    ``/api/v1/audit/who-touched/{target}`` surfaces the
    ``...replay`` path (matching verb) rather than the verb-mismatched
    same-prefix sibling.

    Used purely for the human-readable diagnostic; no logic depends
    on the value. Returns ``None`` when no template shares the
    ``/api/v<N>/<surface>`` prefix.
    """

    def _shared_prefix_len(a: str, b: str) -> int:
        a_parts, b_parts = a.split("/"), b.split("/")
        n = 0
        for x, y in zip(a_parts, b_parts, strict=False):
            if x == y:
                n += 1
            else:
                break
        return n

    candidates = [t for t in openapi_templates if t.startswith("/api/v")]
    if not candidates:
        return None

    citation_last = citation.rstrip("/").split("/")[-1] if "/" in citation else ""

    def _rank(template: str) -> tuple[int, int]:
        """Higher tuple wins; lexicographic ordering by Python's tuple cmp.

        Component 1: shared-prefix length (primary).
        Component 2: 1 if the citation's last segment matches the
        template's last segment (verb match), else 0 (tiebreaker).
        """
        prefix = _shared_prefix_len(citation, template)
        tmpl_last = template.rstrip("/").split("/")[-1] if "/" in template else ""
        verb_match = 1 if citation_last and citation_last == tmpl_last else 0
        return (prefix, verb_match)

    best = max(candidates, key=_rank)
    # Require at least 3 shared segments (``/api/v1/<surface>``) to
    # avoid noise from any-other-surface paths the snapshot lists.
    if _shared_prefix_len(citation, best) < 3:
        return None
    return best


def check_release_body(
    release_body: str,
    openapi_templates: set[str],
    allow_paths: set[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Return the list of unresolved citations.

    Args:
        release_body: full markdown text of the proposed release.
        openapi_templates: the set of templated paths from the
            OpenAPI snapshot (e.g.
            ``/api/v1/audit/sessions/{session_id}/replay``).
        allow_paths: path patterns that should be treated as
            resolved even when absent from ``openapi_templates``.

    Returns:
        A list of ``(citation, closest_match_or_None)`` pairs for
        every citation that didn't resolve. Empty list = all good.
    """
    allow = allow_paths or set()
    unresolved: list[tuple[str, str | None]] = []

    for citation in extract_paths(release_body):
        if citation in allow:
            continue
        candidates = _templatise(citation, openapi_templates)
        if candidates & openapi_templates:
            continue
        if candidates & allow:
            continue
        unresolved.append((citation, _closest_template(citation, openapi_templates)))

    return unresolved


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assert every /api/v* path cited in a release body resolves "
            "in the OpenAPI snapshot. Sister gate to #928 "
            "(cli-api-snapshot-freshness)."
        ),
    )
    parser.add_argument(
        "--release-body",
        type=Path,
        required=True,
        help="Path to the proposed release-body markdown file.",
    )
    parser.add_argument(
        "--openapi-snapshot",
        type=Path,
        required=True,
        help="Path to the published OpenAPI snapshot JSON (cli/api/openapi.json).",
    )
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        help=(
            "Path (literal or templatised) to treat as resolved even when "
            "absent from the OpenAPI snapshot. May be passed multiple times."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        body = args.release_body.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read release body: {exc}", file=sys.stderr)
        return 2

    try:
        openapi_templates = load_openapi_paths(args.openapi_snapshot)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load OpenAPI snapshot: {exc}", file=sys.stderr)
        return 2

    unresolved = check_release_body(body, openapi_templates, set(args.allow_path))

    if not unresolved:
        cited = len(extract_paths(body))
        print(
            f"release-body paths OK: {cited} cited path(s), all resolve in {args.openapi_snapshot}",
        )
        return 0

    print(
        f"release-body paths FAILED: {len(unresolved)} cited path(s) do not "
        f"resolve in {args.openapi_snapshot}:",
        file=sys.stderr,
    )
    for citation, closest in unresolved:
        if closest is not None:
            print(f"  - {citation}  (closest snapshot path: {closest})", file=sys.stderr)
        else:
            print(f"  - {citation}  (no near match in snapshot)", file=sys.stderr)
    print(
        "\nFix: amend the release body to cite the shipped path, "
        "or pass --allow-path if the citation is intentionally "
        "outside the snapshot's surface.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
