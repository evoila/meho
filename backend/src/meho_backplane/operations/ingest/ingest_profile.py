# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synthesise a minimal :class:`ExecutionProfile` from an operator's auth selection.

Initiative #2271 / Goal #221, Task #2289. The boot-time stamping path
(:mod:`meho_backplane.operations.ingest.boot_stamp`, #2288) turns a
*shipped* profile-backed catalog row into a dispatchable
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`. This
module is the **non-catalog on-ramp**: when an operator ingests an arbitrary
spec and *selects* a named auth scheme (``meho connector ingest --auth-scheme
session_login_token``), the ingest pipeline synthesises a minimal
:class:`ExecutionProfile` here and stamps the same profiled connector — so a
hand-authored spec becomes dispatchable without a hand-coded connector class.

The boundary (Initiative #2271, grounded in #1177 / Goal #1964 Non-goals) is
strict: the operator selects a **named scheme from the closed catalog**
(:data:`~meho_backplane.connectors.profile.AuthSchemeName`) plus, optionally,
the **names** of the secret-bundle keys the scheme reads. There is **no**
free-form auth config — no login URL, body template, token JSONPath, or header
name. Those are the rejected-DSL / credential-forwarding door. The login path,
request body shape, and token extractor for each scheme are vetted Python
selected by the scheme name in
:data:`~meho_backplane.connectors._shared.profile_auth.SESSION_SCHEME_SPECS`;
the operator picks *which* vetted shape, never authors one.

Two knobs the profile schema needs but the operator does not supply are
defaulted here:

* **secret-field names** — :data:`DEFAULT_SECRET_FIELDS` maps each named
  scheme to the field names its vetted extractor reads (e.g.
  ``(username, password)`` for the session schemes, ``(client_id,
  client_secret)`` for ``oauth2_mint``). An operator who stores the
  credential under different key names overrides them via
  ``auth_secret_fields``; the values are always resolved from the target's
  ``secret_ref`` at dispatch, never carried in the ingest request.
* **fingerprint / probe / pagination** — a non-catalog ingest carries no
  fingerprint recipe (the operator selected auth, not a version endpoint), so
  :func:`build_ingest_execution_profile` fills these with the conservative
  :data:`_DEFAULT_FINGERPRINT` / ``"delegate"`` / :data:`_DEFAULT_PAGINATION`
  defaults. They keep the profile schema-valid without inventing a per-spec
  recipe: op dispatch runs off the ``EndpointDescriptor`` row through
  ``dispatch_ingested`` (auth + the op path), and the ``strategy="none"``
  pagination default never unwraps ``items_key`` (only the ``cursor_token``
  loop reads it). The fingerprint/probe defaults are exercised only by an
  explicit ``fingerprint`` / ``probe`` call, which the auth-only on-ramp does
  not wire; a connector that needs a real fingerprint recipe ships as a
  profile-backed catalog row (#2288), not this on-ramp.
"""

from __future__ import annotations

from meho_backplane.connectors.profile import (
    NAMED_AUTH_SCHEMES,
    AuthSchemeName,
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
    StaticHeaderValueKind,
)

__all__ = [
    "DEFAULT_SECRET_FIELDS",
    "DEFAULT_STATIC_HEADER_VALUE_KIND",
    "build_ingest_execution_profile",
]

#: Per-scheme default secret-field **names** the vetted extractor reads at
#: dispatch. The values are the field names each scheme's ``build_*`` /
#: ``build_static_headers`` helper looks up in the resolved secret bundle
#: (see :mod:`meho_backplane.connectors._shared.profile_auth`): the session
#: schemes and ``basic`` read ``username`` / ``password``, ``static_header``
#: reads a single ``token`` field, and ``oauth2_mint`` reads ``client_id`` /
#: ``client_secret``. Names only — the credential *values* live in the
#: target's ``secret_ref`` and are resolved by the broker at dispatch.
#:
#: Every member of :data:`~meho_backplane.connectors.profile.NAMED_AUTH_SCHEMES`
#: must have an entry (asserted below and pinned by a test) so a scheme added
#: to the closed catalog cannot silently reach this on-ramp without a
#: reviewed default.
DEFAULT_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "basic": ("username", "password"),
    "static_header": ("token",),
    "session_login": ("username", "password"),
    "session_login_basic": ("username", "password"),
    "session_login_token": ("username", "password"),
    "oauth2_mint": ("client_id", "client_secret"),
}

#: ``static_header`` is the one scheme whose :class:`AuthSpec` requires a
#: ``value_kind``; the auth-only on-ramp exposes no knob for it, so it defaults
#: to bearer-wrapping (argocd's shape). An operator needing raw placement in a
#: custom header authors a profile-backed catalog row instead.
DEFAULT_STATIC_HEADER_VALUE_KIND: StaticHeaderValueKind = "bearer"

# Every named scheme must carry a default secret-field set — a scheme in the
# closed catalog with no entry here would raise a KeyError at ingest rather
# than surfacing at review, which the missing-default is: a reviewer forgot to
# extend this map when adding the scheme.
assert set(DEFAULT_SECRET_FIELDS) == NAMED_AUTH_SCHEMES, (
    "DEFAULT_SECRET_FIELDS must cover exactly NAMED_AUTH_SCHEMES; drift: "
    f"missing={NAMED_AUTH_SCHEMES - set(DEFAULT_SECRET_FIELDS)}, "
    f"extra={set(DEFAULT_SECRET_FIELDS) - NAMED_AUTH_SCHEMES}"
)

#: Conservative fingerprint recipe for an auth-only on-ramp profile. A
#: non-catalog ingest names no version endpoint, so the default reads a
#: top-level ``version`` key off ``/`` verbatim. Schema-valid and never
#: reached by op dispatch (only an explicit ``fingerprint`` call uses it).
_DEFAULT_FINGERPRINT = FingerprintSpec(path="/", version_key="version", version_splitter="none")

#: Single-request pagination default: ``strategy="none"`` means the dispatch
#: loop issues one request and never unwraps ``items_key`` (only the
#: ``cursor_token`` loop reads it), so the placeholder key is inert.
_DEFAULT_PAGINATION = PaginationSpec(strategy="none", items_key="value")


def build_ingest_execution_profile(
    *,
    product: str,
    version: str,
    auth_scheme: AuthSchemeName,
    secret_fields: tuple[str, ...] | None = None,
) -> ExecutionProfile:
    """Synthesise a minimal :class:`ExecutionProfile` from an auth selection.

    Builds the profile the ingest pipeline stamps onto a non-catalog
    connector: the operator-selected *auth_scheme* plus its secret-field
    names (the operator's *secret_fields*, or :data:`DEFAULT_SECRET_FIELDS`
    for the scheme when ``None``), with the conservative fingerprint / probe /
    pagination defaults this module documents.

    *auth_scheme* is already validated against the closed
    :data:`~meho_backplane.connectors.profile.AuthSchemeName` catalog at the
    API boundary (the request schema's ``Literal`` rejects unknown / reserved
    values with a closed-set 422); the :class:`AuthSpec` constructed here
    re-asserts it. ``static_header`` gets :data:`DEFAULT_STATIC_HEADER_VALUE_KIND`
    since the on-ramp exposes no ``value_kind`` knob; every other scheme leaves
    ``value_kind`` unset (the schema forbids it off ``static_header``).

    Raises :class:`pydantic.ValidationError` when *secret_fields* is empty or
    carries a blank name — the same fail-loud contract :class:`AuthSpec`
    enforces for a hand-authored profile.
    """
    fields = secret_fields if secret_fields is not None else DEFAULT_SECRET_FIELDS[auth_scheme]
    value_kind: StaticHeaderValueKind | None = (
        DEFAULT_STATIC_HEADER_VALUE_KIND if auth_scheme == "static_header" else None
    )
    auth = AuthSpec(scheme=auth_scheme, secret_fields=tuple(fields), value_kind=value_kind)
    return ExecutionProfile(
        product=product,
        version=version,
        auth=auth,
        fingerprint=_DEFAULT_FINGERPRINT,
        probe="delegate",
        pagination=_DEFAULT_PAGINATION,
    )
