# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hard-excluded op families for the flight-recorder redaction engine.

Task #3213 (F2.3). Some op families carry secrets *as their body*:
credential reads, session mints, token issuance, password resets, key
material. For those, no per-connector body-path config is trustworthy --
the whole body is the secret. This module classifies an op and, when it
belongs to such a family, tells the caller to **never record its body**,
regardless of config.

Single-sourced with the sensitivity classifier
----------------------------------------------
The **primary** secret-bearing check is not a bespoke list: it delegates
to :func:`meho_backplane.broadcast.events.classify_op`, the authoritative
op-sensitivity classifier the audit/broadcast plane already uses to
collapse ``credential_read`` / ``credential_mint`` / ``credential_write``
ops to aggregate-only. Any op that classifier calls secret-bearing (its
curated ``_CREDENTIAL_*_OPS`` allowlists -- ``vault.kv.read``,
``harbor.robot.create``, ``vault.kv.put``, ``k8s.secret.create``, ...)
never records a body here either, and the two planes cannot drift.
Structured secret bodies of those ops (``{"data": {"password": ...}}``)
carry no in-leaf label the credential-shape net could catch, so this
delegation -- not the pattern/tag nets below -- is what actually protects
them. The pattern globs and tag tokens are a *broadening* net on top,
covering generic (``METHOD:/path``) ops and hand-authored tags the typed
classifier does not see.

Single-sourced with the destructive classifier
----------------------------------------------
Per the decision record and its pending cross-ref amendment to
``docs/decisions/governed-delete-operations.md``, the **destructive /
delete-shaped** family also joins the hard-exclusion set. To keep the
two lists from drifting, this module does **not** re-declare the
delete-shaped patterns: it reads them from the same single source the
grant guard uses -- ``Settings.service_grant_delete_shaped_patterns``
(the ``_delete_shaped_reason_by_pattern`` seam at
``operations/service_grants.py``) -- and applies the same descriptor
signals (HTTP ``DELETE`` method, ``destructive`` tag). When the
``destructive`` safety tier (#3196) lands, this classifier already
covers it via the tag it promotes; nothing here changes.

  NOTE (pending cross-ref amendment): the reciprocal pointer from
  ``governed-delete-operations.md`` back to the flight-recorder record is
  a deliberate follow-up (see the "Interactions with sibling decisions"
  section of ``dispatch-flight-recorder.md``). Including the destructive
  family here now honours the decided shape ahead of that docs edit.

Fail-closed over-exclusion
--------------------------
The secret-family pattern set is deliberately broad. Over-excluding an
op (declining to record a benign body) only loses debugging exhaust; it
never leaks. Under-excluding leaks a secret. So when in doubt, the
family patterns match. The patterns are case-sensitive ``fnmatchcase``
globs over the exact op id, spelled to hit both raw HTTP ops
(``METHOD:/path``, method upper-cased) and dotted typed ops
(``vault.sys.auth.enable``) without case folding -- the same convention
the delete-shaped patterns use.

Unplaceable op ids
------------------
A missing / blank op id cannot be classified. Per F5 that is a
redaction-uncertainty trigger ("an op family the classifier could not
place"): the body is withheld **and** the result is marked uncertain, so
the caller degrades the trace to operator-only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import Final

from pydantic import BaseModel, ConfigDict

__all__ = [
    "SECRET_FAMILY_PATTERNS",
    "SECRET_FAMILY_TAGS",
    "SECRET_OP_CLASSES",
    "BodyExclusion",
    "classify_body_exclusion",
]


#: The op-sensitivity classes (from
#: :func:`meho_backplane.broadcast.events.classify_op`) whose bodies are
#: secret material. An op in any of these classes never records a body.
SECRET_OP_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write"}
)


#: Case-sensitive globs matching credential / session-mint / token /
#: secret / key-material op families across both op-id shapes. Broad by
#: design (see module docstring): over-exclusion is the safe direction.
SECRET_FAMILY_PATTERNS: Final[tuple[str, ...]] = (
    # session lifecycle / login (session-mint)
    "*login*",
    "*logout*",
    "*session*",
    # token issuance
    "*token*",
    # credentials / secrets / passwords
    "*credential*",
    "*secret*",
    "*password*",
    "*passwd*",
    # api keys
    "*apikey*",
    "*api_key*",
    "*api-key*",
    # minting / issuance / auth exchanges
    "*mint*",
    "*authenticate*",
    "*authorization*",
    "*.auth",
    "*.auth.*",
    "*:*/auth",
    "*:*/auth/*",
    "*:*/authorize*",
    "*oauth*",
    "*openid*",
    "*saml*",
    # key material
    "*private_key*",
    "*privatekey*",
    "*.pem",
    "*.key",
    "*.keys",
    "*.keys.*",
    "*:*/key",
    "*:*/keys",
)

#: Secret-bearing tag *tokens*. A descriptor tag is treated as
#: secret-bearing when any of its tokens (split on non-alphanumerics)
#: is in this set -- so hyphenated compounds the codebase actually uses
#: (``secret-read``, ``credential-mint``, ``credential-write``,
#: ``secret-write``, ``auth-token``) match, not only bare words. Checked
#: in addition to :func:`classify_op` and the op-id patterns so a
#: hand-authored typed op can opt into the exclusion via a tag.
SECRET_FAMILY_TAGS: Final[frozenset[str]] = frozenset(
    {
        "secret",
        "secrets",
        "credential",
        "credentials",
        "session",
        "token",
        "tokens",
        "auth",
        "authentication",
        "mint",
        "password",
        "passwd",
        "apikey",
        "oauth",
        "saml",
        "openid",
        "key",
        "keys",
        "privatekey",
        "kubeconfig",
    }
)


def _tag_tokens(tag: str) -> set[str]:
    """Split a tag into lowercase alphanumeric tokens.

    ``"credential-read"`` -> ``{"credential", "read"}`` so a hyphenated
    compound matches :data:`SECRET_FAMILY_TAGS` on its ``credential``
    token; ``"read-only"`` -> ``{"read", "only"}`` matches nothing.
    """
    return {token for token in re.split(r"[^a-z0-9]+", tag.lower()) if token}


def _classify_op_class(op_id: str) -> str | None:
    """Return the sensitivity class from the authoritative classifier.

    Delegates to :func:`meho_backplane.broadcast.events.classify_op`
    (lazy import: it lives in a heavier module and keeps this library's
    import graph a leaf). Returns ``None`` if the classifier is
    unavailable or raises -- the pattern/tag nets below still apply, and
    the caller's best-effort (F7) contract handles any propagated fault.
    """
    try:
        from meho_backplane.broadcast.events import classify_op

        return classify_op(op_id)
    except Exception:  # pragma: no cover - classify_op is pure; defensive
        return None


class BodyExclusion(BaseModel):
    """Whether an op's body must be hard-excluded from the flight recorder.

    * ``excluded`` -- ``True`` means: never record this op's body.
    * ``uncertain`` -- ``True`` only for the *unplaceable* case (missing
      op id). A placed exclusion is *certain*: the omission is deliberate
      and safe, so the trace stays agent-readable with a blank body. An
      unplaceable op is uncertain -> operator-only.
    * ``family`` -- the family label that fired (``None`` when the op is
      not excluded).
    * ``reason`` -- human-readable rationale (``None`` when not excluded).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    excluded: bool
    uncertain: bool = False
    family: str | None = None
    reason: str | None = None


def classify_body_exclusion(
    op_id: str | None,
    *,
    tags: Iterable[str] = (),
    method: str | None = None,
    delete_shaped_patterns: tuple[str, ...] | None = None,
    secret_family_patterns: tuple[str, ...] = SECRET_FAMILY_PATTERNS,
) -> BodyExclusion:
    """Classify *op_id* for flight-recorder body exclusion.

    *tags* / *method* are the resolved descriptor signals (optional).
    *delete_shaped_patterns* defaults to the single source of truth,
    ``Settings.service_grant_delete_shaped_patterns``, resolved lazily so
    this stays a pure library in tests that pass an explicit tuple.

    Returns a :class:`BodyExclusion`. When ``excluded`` is ``True`` the
    caller records no body for this op; when ``uncertain`` is also
    ``True`` (unplaceable op), the caller additionally degrades the trace
    to operator-only.
    """
    if op_id is None or not op_id.strip():
        return BodyExclusion(
            excluded=True,
            uncertain=True,
            family=None,
            reason="op id missing/blank: family unplaceable (fail-closed uncertain)",
        )

    tag_set = {tag.strip().lower() for tag in tags if tag and tag.strip()}

    # --- secret-bearing: authoritative classifier first (single source) -
    op_class = _classify_op_class(op_id)
    if op_class in SECRET_OP_CLASSES:
        return BodyExclusion(
            excluded=True,
            family="secret-bearing",
            reason=(
                f"op {op_id!r} classifies as {op_class!r} "
                "(broadcast.events.classify_op); body never recorded"
            ),
        )

    # --- secret-bearing: descriptor tag tokens -------------------------
    matched_tag_tokens = {
        token for tag in tag_set for token in _tag_tokens(tag) if token in SECRET_FAMILY_TAGS
    }
    if matched_tag_tokens:
        return BodyExclusion(
            excluded=True,
            family="secret-bearing",
            reason=(
                f"op {op_id!r} carries secret-bearing tag token(s) "
                f"{sorted(matched_tag_tokens)}; body never recorded"
            ),
        )

    # --- secret-bearing: broadening op-id pattern net ------------------
    for pattern in secret_family_patterns:
        if fnmatchcase(op_id, pattern):
            return BodyExclusion(
                excluded=True,
                family="secret-bearing",
                reason=(
                    f"op {op_id!r} matches secret-bearing family pattern "
                    f"{pattern!r}; body never recorded"
                ),
            )

    # --- destructive / delete-shaped (single-sourced) ------------------
    if delete_shaped_patterns is None:
        from meho_backplane.settings import get_settings

        delete_shaped_patterns = get_settings().service_grant_delete_shaped_patterns
    if (method or "").upper() == "DELETE":
        return BodyExclusion(
            excluded=True,
            family="destructive",
            reason=f"op {op_id!r} is an HTTP DELETE; body never recorded",
        )
    if "destructive" in tag_set:
        return BodyExclusion(
            excluded=True,
            family="destructive",
            reason=f"op {op_id!r} carries the 'destructive' tag; body never recorded",
        )
    for pattern in delete_shaped_patterns:
        if fnmatchcase(op_id, pattern):
            return BodyExclusion(
                excluded=True,
                family="destructive",
                reason=(
                    f"op {op_id!r} matches delete-shaped pattern {pattern!r} "
                    "(single-sourced with the grant guard); body never recorded"
                ),
            )

    return BodyExclusion(excluded=False)
