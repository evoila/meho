# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``ExecutionProfile`` schema + the closed named auth-scheme catalog.

G0.28-T3 (#1969) — the load-bearing schema half of Initiative #1965 (make
ingested REST read ops dispatchable from a reviewed declarative profile).
T1 (#1967) landed :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`,
the dispatchable sibling of the auto-shim whose one hand-coded slot is
``auth_headers``. This module defines the reviewed declarative data that
fills that slot.

**The catalog selects a vetted extractor by name — it is not a DSL.**
``AuthSchemeName`` is a closed :data:`typing.Literal` enumerating the four
auth shapes a profile can declare. Each value *names a vetted Python
extractor* (hoisted from the existing connectors in T4 #1970); the profile
carries **no** ``token_location`` / ``field_map`` / ``value_template`` /
JSONPath fields. That boundary is the #1177/#1178 substrate-minimalism
line, the same one ``runbooks/schemas.Verify`` holds for the verify DSL
("No JSONPath. No comparison operators. No boolean composition." —
``docs/architecture/runbooks.md``). A path-expression auth block would
re-open the rejected-DSL footgun: the agent would be configuring an
interpreter, not selecting a reviewed extractor.

**The catalog is grounded in all 14 HTTP connectors' ``auth_headers``,
not picked from memory.** The coverage trace lives in
``docs/codebase/connector-auth-coverage.md``. Six connectors fit a named
scheme (harbor / sddc_manager / vcf_fleet / vcf_operations → ``basic``;
argocd → ``static_header``; keycloak → ``oauth2_mint``; vcf_logs / vmware_rest
→ ``session_login``). The remaining eight need bespoke Python the closed
catalog deliberately does **not** model — github (App RS256 JWT), gcloud
(SA-JSON / ADC impersonation), vault (operator-JWT-forward), kubernetes
(kubeconfig), nsx (cookie-jar session), vcf_automation (dual-plane
session) — and are listed in :data:`RESERVED_AUTH_SCHEMES`. A profile that
names a reserved scheme raises :exc:`ReservedAuthSchemeError`, whose
remediation is "author a typed connector", distinct from the auto-shim's
``unreplaced_auto_shim`` "register a per-product subclass" guidance.

**Credentials never live in the profile — only secret-field *names*.**
``AuthSpec.secret_fields`` names the keys the extractor reads out of the
operator-resolved secret bundle (``username`` / ``password`` / ``token`` /
``client_id`` / ``client_secret``); the values are resolved at dispatch
from the secret broker, never serialized into the reviewed profile.

**Closed-set enforcement is two-layered**, mirroring the ``safety_level``
``Literal`` + DB-CHECK and ``AuthModel`` StrEnum precedents:

* **API boundary.** ``AuthSpec.scheme`` is the :data:`AuthSchemeName`
  ``Literal`` — Pydantic rejects an unlisted scheme at
  ``model_validate`` time (the route returns 422), so a malformed profile
  never reaches the registry.
* **Startup load.** :func:`validate_execution_profile` (called from the
  same boot path as :func:`~meho_backplane.operations.ingest.catalog.load_catalog`)
  re-asserts the scheme against the live :data:`NAMED_AUTH_SCHEMES` /
  :data:`RESERVED_AUTH_SCHEMES` partition and crashes the lifespan
  (``CI app-boot smoke fails``) on an unlisted or reserved scheme.
  :data:`NAMED_AUTH_SCHEMES` is derived from the ``Literal`` via
  :func:`typing.get_args`, so the boundary and boot checks cannot disagree.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "NAMED_AUTH_SCHEMES",
    "RESERVED_AUTH_SCHEMES",
    "AuthSchemeName",
    "AuthSpec",
    "ExecutionProfile",
    "ExecutionProfileError",
    "ReservedAuthSchemeError",
    "StaticHeaderValueKind",
    "UnknownAuthSchemeError",
    "validate_execution_profile",
]


#: The closed catalog of named auth schemes a profile may declare. Each
#: value selects a vetted Python extractor (hoisted in T4 #1970), never a
#: path/template expression. Grounded in the 14-connector coverage trace
#: (``docs/codebase/connector-auth-coverage.md``):
#:
#: * ``basic`` — HTTP Basic ``Authorization: Basic b64(user:pass)``
#:   (harbor / sddc_manager / vcf_fleet / vcf_operations / hetzner_robot).
#: * ``static_header`` — a fixed pre-issued token placed in a header
#:   (argocd's static API token). ``value_kind`` picks bearer-wrapping vs
#:   raw placement.
#: * ``session_login`` — exchange credentials at a login endpoint for a
#:   short-lived token, then send it as ``Bearer`` (vcf_logs / vmware_rest).
#: * ``oauth2_mint`` — OAuth2 client-credentials form grant minting a
#:   ``Bearer`` token (keycloak admin).
#:
#: Adding a value here is a deliberate act: it must be backed by a vetted
#: extractor and the coverage trace updated. There is no "custom" / "other"
#: escape hatch — that is the rejected-DSL door (#1177).
AuthSchemeName = Literal[
    "basic",
    "static_header",
    "session_login",
    "oauth2_mint",
]

#: Runtime mirror of :data:`AuthSchemeName` for the startup-load check.
#: Derived from the ``Literal`` via :func:`typing.get_args` so the two
#: cannot drift — the ``Literal`` is the single source of truth for the
#: API boundary and this set re-derives from it for the boot guard.
NAMED_AUTH_SCHEMES: frozenset[str] = frozenset(get_args(AuthSchemeName))

#: Auth shapes that a profile *cannot* model — they need bespoke Python a
#: declarative ``dict[str, str]`` extractor can't express (stateful cookie
#: jars, RS256 JWT minting, SA-JSON impersonation, kubeconfig clients,
#: operator-JWT forwarding, dual-plane sessions). Naming one of these in a
#: profile raises :exc:`ReservedAuthSchemeError` with "author a typed
#: connector" remediation. Listed (not silently absent) so the boot guard
#: and the error message can name the typed-connector alternative per
#: connector. Grounded in the same coverage trace.
RESERVED_AUTH_SCHEMES: frozenset[str] = frozenset(
    {
        "github_app_jwt",  # github — RS256 App-JWT mint -> installation token
        "gcp_sa_impersonation",  # gcloud — ADC + SA impersonation
        "operator_jwt_forward",  # vault — operator-context OIDC JWT forward
        "kubeconfig",  # kubernetes — cert/token embedded in an ApiClient
        "cookie_jar_session",  # nsx — Set-Cookie JSESSIONID + X-XSRF-TOKEN
        "dual_plane_session",  # vcf_automation — provider + tenant logins
    }
)

#: How a ``static_header`` scheme places its pre-issued token value.
#: ``bearer`` → ``Authorization: Bearer <value>`` (argocd's shape). ``raw``
#: → the value is placed verbatim into the named header (e.g. an
#: ``X-Api-Key`` style header). A closed enum, not a free template — the
#: substrate places the value, it does not interpolate it.
StaticHeaderValueKind = Literal["bearer", "raw"]


class ExecutionProfileError(RuntimeError):
    """Base for ``ExecutionProfile`` boot-time / coherence failures.

    Carries a human-readable remediation message. Raised by
    :func:`validate_execution_profile` at the startup-load layer, parallel
    to :class:`~meho_backplane.operations.ingest.catalog.CatalogError`.
    """


class UnknownAuthSchemeError(ExecutionProfileError):
    """A profile names an auth scheme in neither the named nor reserved set.

    The Pydantic ``Literal`` already rejects this at the API boundary; this
    is the belt-and-suspenders boot guard for a profile that reached the
    registry through a non-validated path (e.g. a hand-edited stored row).
    Crashes the lifespan, like an unparseable catalog.
    """


class ReservedAuthSchemeError(ExecutionProfileError):
    """A profile names a *reserved* auth scheme — one that needs typed Python.

    Distinct from :exc:`UnknownAuthSchemeError` (a typo / unknown value)
    and from the dispatcher's ``unreplaced_auto_shim`` cause (register a
    per-product subclass). The remediation here is **author a typed
    connector**: the auth shape (cookie-jar session, RS256 JWT mint,
    SA-JSON impersonation, kubeconfig, operator-JWT forward, dual-plane
    session) cannot be expressed as a declarative named extractor returning
    ``dict[str, str]``, so a reviewed profile is the wrong tool — a
    hand-coded :class:`~meho_backplane.connectors.adapters.http.HttpConnector`
    subclass is.
    """

    def __init__(self, scheme: str) -> None:
        self.scheme = scheme
        super().__init__(
            f"auth scheme {scheme!r} is reserved: it requires bespoke Python "
            f"a declarative ExecutionProfile cannot express. Author a typed "
            f"connector (an HttpConnector subclass implementing auth_headers) "
            f"rather than attaching a profile."
        )


class AuthSpec(BaseModel):
    """The declarative auth block of an :class:`ExecutionProfile`.

    Selects a named, vetted extractor (:attr:`scheme`) and names the
    secret-bundle keys it reads (:attr:`secret_fields`). Carries **no**
    path/template/expression field — that is the rejected-DSL line. The
    only scheme-shaping knob is :attr:`value_kind`, a closed enum that only
    applies to ``static_header`` (bearer-wrap vs raw placement).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: AuthSchemeName = Field(
        description=(
            "Named auth scheme selecting a vetted extractor. Closed Literal "
            "— reserved/unknown schemes are rejected here (422) and at boot."
        )
    )
    secret_fields: tuple[str, ...] = Field(
        description=(
            "Names of the secret-bundle keys the extractor reads at dispatch "
            "(e.g. ('username', 'password'), ('token',), "
            "('client_id', 'client_secret')). NAMES only — credential values "
            "are resolved from the secret broker, never stored in the profile."
        )
    )
    header_name: str = Field(
        default="Authorization",
        description=(
            "The header the extractor writes the auth value into. Defaults to "
            "Authorization (basic / session_login / oauth2_mint / a bearer "
            "static_header); a raw static_header may name a custom header "
            "(e.g. X-Api-Key)."
        ),
    )
    value_kind: StaticHeaderValueKind | None = Field(
        default=None,
        description=(
            "static_header only: 'bearer' wraps the value as "
            "'Bearer <value>', 'raw' places it verbatim. Must be None for "
            "every other scheme; required for static_header."
        ),
    )

    @field_validator("secret_fields")
    @classmethod
    def _secret_fields_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject an empty or blank-named secret-field list.

        Every named scheme reads at least one secret key; an empty list is
        a malformed profile, not a credential-less auth shape (that would
        be a reserved/typed concern).
        """
        if not value:
            raise ValueError("secret_fields must name at least one secret key")
        if any(not field.strip() for field in value):
            raise ValueError("secret_fields entries must be non-blank")
        return value

    @field_validator("header_name")
    @classmethod
    def _header_name_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("header_name must be non-blank")
        return value

    @model_validator(mode="after")
    def _value_kind_matches_scheme(self) -> AuthSpec:
        """Bind ``value_kind`` to ``static_header`` exclusively.

        ``value_kind`` is the static_header bearer/raw knob; it is
        meaningless (and so forbidden) for the other schemes, and required
        for static_header. Keeping the rule here — rather than splitting
        AuthSpec into per-scheme subclasses — keeps the single-model API
        boundary the route validates against.
        """
        if self.scheme == "static_header":
            if self.value_kind is None:
                raise ValueError("static_header requires value_kind ('bearer' or 'raw')")
        elif self.value_kind is not None:
            raise ValueError(f"value_kind is only valid for static_header, not {self.scheme!r}")
        return self


class ExecutionProfile(BaseModel):
    """Reviewed declarative data that makes an ingested REST connector dispatchable.

    Plugs into :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`
    to fill its one hand-coded slot (``auth_headers``) with vetted
    declarative data. T3 (#1969) defines the schema + the auth catalog;
    the session/token machinery the schemes drive lands in T4 (#1970), the
    profile-driven fingerprint/probe + pagination in T6 (#1972).

    Frozen and ``extra="forbid"`` — a profile is a reviewed artifact; an
    unrecognized key is a malformed profile, not a forward-compat
    extension point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str = Field(
        description="Product slug the profile covers (matches the connector's product)."
    )
    version: str = Field(
        description="Version label the profile covers (the ingested spec's version)."
    )
    auth: AuthSpec = Field(
        description="The named auth scheme + secret-field names for this profile."
    )

    @field_validator("product", "version")
    @classmethod
    def _identity_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("product and version must be non-blank")
        return value


def validate_execution_profile(profile: ExecutionProfile) -> None:
    """Re-assert a profile's auth scheme at the startup-load layer.

    Mirrors :func:`~meho_backplane.operations.ingest.catalog.load_catalog`'s
    boot-crash discipline: the Pydantic ``Literal`` already rejects an
    unlisted scheme at the API boundary, but a stored profile can reach the
    registry through a non-validated path (a hand-edited row, a future
    persistence layer). This guard crashes the lifespan — and therefore CI
    app-boot smoke — on:

    * a scheme in :data:`RESERVED_AUTH_SCHEMES` → :exc:`ReservedAuthSchemeError`
      (author a typed connector);
    * a scheme in neither the named nor reserved set →
      :exc:`UnknownAuthSchemeError`.

    A scheme in :data:`NAMED_AUTH_SCHEMES` passes. The two runtime sets are
    derived from / kept in lockstep with the :data:`AuthSchemeName`
    ``Literal`` so the boundary and boot checks cannot disagree.
    """
    scheme = profile.auth.scheme
    if scheme in NAMED_AUTH_SCHEMES:
        return
    if scheme in RESERVED_AUTH_SCHEMES:
        raise ReservedAuthSchemeError(scheme)
    raise UnknownAuthSchemeError(
        f"auth scheme {scheme!r} is not a known scheme; valid named schemes "
        f"are {sorted(NAMED_AUTH_SCHEMES)} (reserved, typed-only: "
        f"{sorted(RESERVED_AUTH_SCHEMES)})"
    )
