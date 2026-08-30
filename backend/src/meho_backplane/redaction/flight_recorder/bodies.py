# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-connector body-path redaction for the flight-recorder engine.

Task #3213 (F2.2). Each connector declares -- as a set of dotted-path
globs -- which request/response body paths must be scrubbed before a
body is recorded. This module applies that declarative config, then runs
the shared credential-shape scrub (:mod:`._content`) underneath as
defense-in-depth, and returns a fail-closed
:class:`~meho_backplane.redaction.flight_recorder.verdict.RedactionOutcome`.

Structural proof, not omniscience
---------------------------------
The engine can only claim a body *certain* (agent-readable) when it
**proved** it applied every applicable rule: it parsed the body into a
structure, redacted every declared path, and ran the shape net over
every surviving leaf. Where that proof is impossible it drops the datum
and flags ``uncertain`` (the F5 operator-only degrade):

* **Unparseable / binary / unknown content-type** -- a body it cannot
  turn into a structure cannot be walked to prove redaction.
* **Malformed JSON** -- likewise; a half-object could hide a secret in
  the unparsed remainder.
* **Truncated** -- a body cut mid-token can split a secret across the
  boundary so neither half matches a shape; the caller signals this via
  ``truncated=True`` and the outcome is uncertain even though the parsed
  prefix is scrubbed.
* **Body-path config error** -- any fault while matching the declared
  globs fails closed to a dropped, uncertain body.

The dotted-path glob grammar is reused verbatim from the Tier-2 matcher
(:mod:`meho_backplane.redaction.path_glob`): ``*`` matches one segment,
``**`` matches any depth. A declared path that lands on an interior node
(object or array) redacts the **whole subtree** at that path, so a
declared ``credentials`` object never round-trips even partially.

Parse posture
-------------
JSON is the only structurally-redactable body shape, so the engine
parses a raw str/bytes body as JSON when the content-type says JSON *or*
is unknown/absent (an opportunistic attempt that still fails closed on a
parse error). A content-type that is *known* to be non-JSON
(``text/plain``, form-encoded, XML, ``application/octet-stream``,
``image/*`` …) is dropped uncertain without an attempt -- those shapes
carry credentials the JSON path config cannot address, and the ops whose
bodies *are* credentials are already hard-excluded by family
(:mod:`.families`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator

from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.flight_recorder.verdict import (
    BODY_OMITTED_MARKER,
    BODY_PATH_MARKER,
    RedactionOutcome,
)
from meho_backplane.redaction.path_glob import glob_to_regex, path_matches

__all__ = ["BodyPathRedactionConfig", "redact_body"]


#: Content-type bases the engine will not attempt to parse as JSON. A
#: body of one of these shapes is dropped uncertain (fail-closed) rather
#: than recorded, because the per-connector JSON path config cannot
#: structurally address it.
_NON_JSON_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "text/plain",
        "text/html",
        "text/csv",
        "text/xml",
        "application/xml",
        "application/x-www-form-urlencoded",
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
    }
)

#: Content-type prefixes treated as non-JSON binary/opaque.
_NON_JSON_PREFIXES: Final[tuple[str, ...]] = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "multipart/",
)


class BodyPathRedactionConfig(BaseModel):
    """A connector's declared body-path redaction config.

    ``paths`` is a tuple of dotted-path globs (see module docstring)
    applied to **both** request and response bodies. Globs are validated
    -- and compiled -- at construction so a malformed pattern fails when
    the connector's config is loaded, not silently at first dispatch.

    The config is frozen and side-effect-free; it is data a connector
    ships, resolved and handed to :func:`redact_body` by the caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: str
    paths: tuple[str, ...] = ()

    @field_validator("paths")
    @classmethod
    def _paths_compile(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip()
            if not stripped:
                raise ValueError("body-path glob must not be blank or whitespace-only")
            try:
                glob_to_regex(stripped)
            except re.error as exc:  # pragma: no cover -- glob_to_regex escapes literals
                raise ValueError(f"invalid body-path glob {stripped!r}: {exc}") from exc
            normalized.append(stripped)
        return tuple(normalized)


def redact_body(
    body: Any,
    *,
    paths: tuple[str, ...] = (),
    content_type: str | None = None,
    truncated: bool = False,
) -> RedactionOutcome:
    """Redact one body against its connector's declared *paths*, fail-closed.

    Parameters
    ----------
    body
        The raw body: a parsed ``dict`` / ``list`` (or scalar), a JSON
        ``str``, or ``bytes``. ``None`` / empty round-trips to a recorded
        ``None`` (nothing to redact).
    paths
        The connector's declared dotted-path globs (usually
        ``BodyPathRedactionConfig.paths``).
    content_type
        The body's declared content-type, used to decide parseability.
    truncated
        ``True`` when the capture layer cut this body at a cap. Forces an
        uncertain verdict: a secret split across the cut is unverifiable.

    Returns a :class:`RedactionOutcome`. ``uncertain=True`` means the
    caller must degrade this trace to operator-only.
    """
    if _is_empty_body(body):
        return RedactionOutcome(value=None, reasons=("empty body",))

    parsed, parse_reason = _parse_body(body, content_type)
    if parse_reason is not None:
        return RedactionOutcome(
            value=BODY_OMITTED_MARKER,
            uncertain=True,
            reasons=(parse_reason,),
        )

    try:
        path_redacted = _apply_path_redaction(parsed, paths)
        scrubbed, _fired = scrub_content(path_redacted)
    except Exception as exc:  # any walk/glob/scrub fault fails closed (F2 uncertainty)
        # Covers a RecursionError from an adversarially deep pre-parsed
        # body handed straight in (bypassing the guarded JSON parse), so
        # the engine never raises into its best-effort (F7) caller.
        return RedactionOutcome(
            value=BODY_OMITTED_MARKER,
            uncertain=True,
            reasons=(f"body redaction failed ({type(exc).__name__}): dropped fail-closed",),
        )

    if truncated:
        return RedactionOutcome(
            value=scrubbed,
            uncertain=True,
            reasons=("body was truncated: tail unverifiable (fail-closed uncertain)",),
        )
    return RedactionOutcome(value=scrubbed)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_empty_body(body: Any) -> bool:
    """``True`` for ``None`` / empty string / empty bytes -- nothing to redact."""
    if body is None:
        return True
    if isinstance(body, str):
        return body.strip() == ""
    if isinstance(body, (bytes, bytearray)):
        return len(body) == 0
    return False


def _parse_body(body: Any, content_type: str | None) -> tuple[Any, str | None]:
    """Return ``(parsed, None)`` or ``(None, reason)`` when unparseable.

    Already-parsed containers (``Mapping`` / non-str ``Sequence``) and
    scalars pass through unchanged -- the caller handed us a structure we
    can walk. A raw str/bytes body is parsed as JSON only when the
    content-type permits (JSON or unknown); a known-non-JSON body is
    refused fail-closed.
    """
    if isinstance(body, Mapping):
        return body, None
    if isinstance(body, (bytes, bytearray)):
        base = _content_type_base(content_type)
        if _is_known_non_json(base):
            return None, (
                f"binary / non-JSON body (content-type {base!r}): "
                "cannot parse to prove redaction (fail-closed)"
            )
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return None, "non-UTF-8 body: cannot parse to prove redaction (fail-closed)"
        return _parse_json_text(text, content_type)
    if isinstance(body, str):
        base = _content_type_base(content_type)
        if _is_known_non_json(base):
            return None, (
                f"non-JSON body (content-type {base!r}): cannot structurally redact (fail-closed)"
            )
        return _parse_json_text(body, content_type)
    if isinstance(body, Sequence):
        # A list handed in already parsed.
        return body, None
    # Bool / int / float / other scalar already parsed -- nothing to
    # structurally redact; the shape scrub is a no-op on non-strings.
    return body, None


def _parse_json_text(text: str, content_type: str | None) -> tuple[Any, str | None]:
    """Attempt to parse *text* as JSON; fail closed on any parse fault."""
    try:
        return json.loads(text), None
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        if _is_json_content_type(content_type):
            return None, (
                f"malformed JSON body ({type(exc).__name__}): cannot prove redaction (fail-closed)"
            )
        return None, (
            "body is not parseable JSON and content-type is unknown: "
            "cannot structurally redact (fail-closed)"
        )


def _content_type_base(content_type: str | None) -> str:
    """Lowercased media type without parameters (``application/json`` from
    ``application/json; charset=utf-8``). Empty string when absent."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _is_json_content_type(content_type: str | None) -> bool:
    base = _content_type_base(content_type)
    return base == "application/json" or base.endswith("+json") or base == "text/json"


def _is_known_non_json(base: str) -> bool:
    """``True`` for a content-type base we refuse to parse as JSON.

    An empty base (absent content-type) returns ``False`` so the engine
    still *attempts* an opportunistic JSON parse -- which itself fails
    closed if the body is not JSON.
    """
    if not base:
        return False
    if _is_json_content_type(base):
        return False
    if base in _NON_JSON_CONTENT_TYPES:
        return True
    return any(base.startswith(prefix) for prefix in _NON_JSON_PREFIXES)


def _apply_path_redaction(node: Any, globs: tuple[str, ...]) -> Any:
    """Redact every node whose dotted path matches a declared glob."""
    if not globs:
        return node
    return _walk_paths(node, globs, "")


def _walk_paths(node: Any, globs: tuple[str, ...], path: str) -> Any:
    if path and path_matches(globs, path):
        # Redact the whole node (leaf or subtree) at a matching path.
        return BODY_PATH_MARKER
    if isinstance(node, Mapping):
        return {
            str(key): _walk_paths(value, globs, _join_path(path, str(key)))
            for key, value in node.items()
        }
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        return [
            _walk_paths(item, globs, _join_path(path, str(index)))
            for index, item in enumerate(node)
        ]
    return node


def _join_path(parent: str, child: str) -> str:
    if not parent:
        return child
    return f"{parent}.{child}"
