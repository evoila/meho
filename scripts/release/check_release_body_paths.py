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
5. For each citation that *spells out an HTTP method*
   (``POST /api/v1/operations/search``), additionally assert the
   method is one the resolved path exposes in the snapshot. This
   catches verb/method drift on a path that does exist — a class the
   path-existence check (step 4) is blind to. A bare citation with no
   leading verb is path-checked only.
6. Exit 0 on full match; exit 1 with a diagnostic listing every
   path that didn't resolve and every verb that doesn't match.

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

* ``0`` — every cited path resolves (or matches a whitelist entry)
  and every verb-prefixed citation names a method the path exposes.
* ``1`` — at least one citation failed: a path that didn't resolve
  (report lists the cited token and the closest OpenAPI prefix
  match), or a verb-prefixed citation naming a method the resolved
  path doesn't expose (report lists the path and the verbs it does).
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

#: HTTP methods that count as operations on an OpenAPI path item. Other
#: keys under a path-item object (``parameters``, ``summary``, ``$ref``,
#: ``servers``, ``description``) are not verbs and are excluded when
#: deriving a path's exposed method set.
_OPENAPI_VERBS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: Matches a verb-prefixed citation: an uppercase HTTP method immediately
#: before an ``/api/v<N>/...`` token (e.g. ``POST /api/v1/operations/search``).
#: The verb and the path are captured separately so the path can be
#: validated for existence (templatised, like a bare citation) *and* the
#: verb checked against the path's actual method set. A bare citation
#: with no leading verb does not match here and is method-unchecked —
#: only the explicit verb in the prose is held to the snapshot.
_VERB_PATH_RE: Final = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+"
    r"(/api/v[0-9]+/[A-Za-z0-9_/{}\-.]*)"
)


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


def extract_verb_paths(release_body: str) -> list[tuple[str, str]]:
    """Return every ``<VERB> /api/v*`` citation as a ``(verb, path)`` pair.

    The verb is lower-cased to match the OpenAPI method-key convention;
    the path is stripped of trailing prose punctuation the same way
    :func:`extract_paths` does. Order-preserving, deduped on the
    ``(verb, path)`` pair so a path cited under two different verbs (a
    ``GET`` and a ``POST`` of the same route) is checked once per verb.

    Only citations that spell out an HTTP method are returned; a bare
    ``/api/v1/...`` citation has no verb to validate and is covered by
    the path-existence check alone.
    """
    seen: dict[tuple[str, str], None] = {}
    for verb, raw in _VERB_PATH_RE.findall(release_body):
        cleaned = _strip_trailing_punct(raw)
        if not cleaned:
            continue
        pair = (verb.lower(), cleaned)
        if pair not in seen:
            seen[pair] = None
    return list(seen.keys())


def load_openapi_paths(snapshot_path: Path) -> dict[str, set[str]]:
    """Load each ``paths`` entry and its method set from an OpenAPI snapshot.

    Returns a mapping of ``path-template -> {lowercased HTTP method}`` so a
    caller can validate both that a cited path exists *and* that a
    verb-prefixed citation names a method the path actually exposes. The
    method set is derived from each OpenAPI path-item object's keys,
    filtered to the recognised HTTP verbs — ``parameters`` / ``summary`` /
    ``$ref`` and other non-operation keys are excluded.

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

    methods_by_path: dict[str, set[str]] = {}
    for path, item in paths.items():
        verbs: set[str] = set()
        if isinstance(item, dict):
            verbs = {key.lower() for key in item if key.lower() in _OPENAPI_VERBS}
        methods_by_path[path] = verbs
    return methods_by_path


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


def _resolve_template(citation: str, openapi_templates: set[str]) -> str | None:
    """Return the OpenAPI path-template a citation resolves to, if any.

    A citation resolves when one of its templatised forms is a literal key
    in the snapshot. Used by the verb check to find *which* path-item a
    verb-prefixed citation lands on so its method set can be consulted.
    Returns ``None`` when the path itself doesn't resolve (in which case
    the path-existence check already reports it — the verb check stays
    silent to avoid a duplicate diagnostic).
    """
    matched = _templatise(citation, openapi_templates) & openapi_templates
    if not matched:
        return None
    # Deterministic pick when a concrete citation collides with multiple
    # templates (rare): the shortest, then lexicographically-first, is
    # the most specific literal match.
    return sorted(matched, key=lambda t: (len(t), t))[0]


def check_release_body(
    release_body: str,
    openapi_paths: set[str] | dict[str, set[str]],
    allow_paths: set[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Return the list of unresolved citations.

    Args:
        release_body: full markdown text of the proposed release.
        openapi_paths: the templated paths from the OpenAPI snapshot. A
            bare ``set`` of templates validates path existence only; a
            ``dict`` mapping each template to its method set
            (as :func:`load_openapi_paths` returns) additionally validates
            that a verb-prefixed citation names a method the path exposes
            — catching verb/method drift on a path that does exist (e.g.
            a body advertising ``POST /api/v1/operations/search`` for a
            GET-only route).
        allow_paths: path patterns that should be treated as
            resolved even when absent from ``openapi_paths``.

    Returns:
        A list of ``(citation, hint_or_None)`` pairs for every citation
        that didn't resolve — by missing path, or (when method info is
        supplied) by naming a verb the resolved path doesn't expose. The
        hint is the closest snapshot path for a missing-path failure, or
        the list of methods the path *does* expose for a verb mismatch.
        Empty list = all good.
    """
    allow = allow_paths or set()
    methods_by_path: dict[str, set[str]] | None = (
        openapi_paths if isinstance(openapi_paths, dict) else None
    )
    openapi_templates: set[str] = set(openapi_paths)
    unresolved: list[tuple[str, str | None]] = []
    unresolved_paths: set[str] = set()

    for citation in extract_paths(release_body):
        if citation in allow:
            continue
        candidates = _templatise(citation, openapi_templates)
        if candidates & openapi_templates:
            continue
        if candidates & allow:
            continue
        unresolved.append((citation, _closest_template(citation, openapi_templates)))
        unresolved_paths.add(citation)

    # Verb/method drift: a path that DOES exist but is cited under a
    # method it doesn't expose. Only runs when method info is supplied,
    # and only for citations whose path resolved above (an unresolved
    # path is already reported — don't double-flag it).
    if methods_by_path is not None:
        for verb, citation in extract_verb_paths(release_body):
            if citation in allow or citation in unresolved_paths:
                continue
            template = _resolve_template(citation, openapi_templates)
            if template is None:
                continue
            exposed = methods_by_path.get(template, set())
            if verb in exposed:
                continue
            exposed_hint = (
                "exposes " + ", ".join(sorted(m.upper() for m in exposed))
                if exposed
                else "exposes no operations"
            )
            unresolved.append((f"{verb.upper()} {citation}", f"{template} {exposed_hint}"))

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
        openapi_paths = load_openapi_paths(args.openapi_snapshot)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load OpenAPI snapshot: {exc}", file=sys.stderr)
        return 2

    unresolved = check_release_body(body, openapi_paths, set(args.allow_path))

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
    for citation, hint in unresolved:
        if hint is None:
            print(f"  - {citation}  (no near match in snapshot)", file=sys.stderr)
        elif " " in citation:
            # Verb-prefixed citation whose path exists but method drifted:
            # the hint already spells out the methods the path exposes.
            print(f"  - {citation}  (snapshot path {hint})", file=sys.stderr)
        else:
            print(f"  - {citation}  (closest snapshot path: {hint})", file=sys.stderr)
    print(
        "\nFix: amend the release body to cite the shipped path and method, "
        "or pass --allow-path if the citation is intentionally "
        "outside the snapshot's surface.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
