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
    "DEFAULT_EXPIRY_STATUSES",
    "NAMED_AUTH_SCHEMES",
    "RESERVED_AUTH_SCHEMES",
    "VERSION_SPLITTERS",
    "AuthSchemeName",
    "AuthSpec",
    "CursorTokenPagination",
    "ExecutionProfile",
    "ExecutionProfileError",
    "FingerprintSpec",
    "PaginationSpec",
    "ProbeSpec",
    "ReservedAuthSchemeError",
    "StaticHeaderValueKind",
    "UnknownAuthSchemeError",
    "VersionSplitter",
    "validate_execution_profile",
]


#: The default session-expiry / auth-failure status set a profile declares
#: when it does not override ``expiry_statuses``. ``401`` is the
#: connector-agnostic load-bearing case (every session connector re-logs in
#: once on a 401); appliances with their own expiry code add it explicitly
#: (vRLI declares ``{401, 440}``). This is the **single profile-declared
#: source** consumed by both the session-retry harness (T4 #1970) and the
#: dispatcher's auth-class classification arm
#: (:func:`~meho_backplane.operations._errors.is_auth_failed_status`): the
#: status set is parameterised once on the profile, never duplicated across
#: a connector-side ``_SESSION_EXPIRED_STATUSES`` and a dispatcher-side
#: ``_AUTH_FAILED_STATUSES``. It carries **no** per-status remediation /
#: action — classification stays central (#1973); the profile only narrows
#: the closed status *set*.
DEFAULT_EXPIRY_STATUSES: frozenset[int] = frozenset({401})


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

#: The closed catalog of named version splitters a :class:`FingerprintSpec`
#: may select to render the upstream version string into
#: ``(version, build)``. Each value names a vetted Python splitter (see
#: :func:`split_version`) hoisted from an existing connector's
#: ``_parse_*_version`` helper — **never** a free format string / regex.
#: A free regex would re-open the rejected-DSL door (#1177): the operator
#: would be authoring a parser, not selecting a reviewed one.
#:
#: * ``none`` — the raw fingerprint field is the version verbatim; no
#:   build component (the default for an API whose version endpoint already
#:   returns a clean ``MAJOR.MINOR.PATCH``).
#: * ``dash`` — split on the first ``-``: ``"v2.11.0-abc1234"`` →
#:   ``("v2.11.0", "abc1234")`` (harbor's ``_parse_harbor_version``).
#: * ``vrli_five_part`` — dot-split a 5-part vRLI version
#:   ``"9.0.0.0.21761695"`` → ``("9.0.0", "21761695")``: ``parts[0:3]``
#:   joined as the public version, ``parts[4]`` as the build
#:   (vcf_logs' ``_parse_vrli_version``).
#:
#: Adding a value is a deliberate act backed by a real connector's parse
#: shape — there is no ``custom`` / ``regex`` escape hatch.
VersionSplitter = Literal["none", "dash", "vrli_five_part"]

#: Runtime mirror of :data:`VersionSplitter` for shape assertions / docs.
#: Derived from the ``Literal`` via :func:`typing.get_args` so the two
#: cannot drift.
VERSION_SPLITTERS: frozenset[str] = frozenset(get_args(VersionSplitter))


def split_version(splitter: VersionSplitter, raw: str | None) -> tuple[str | None, str | None]:
    """Render *raw* into ``(version, build)`` using the named *splitter*.

    The single dispatch point for the closed :data:`VersionSplitter`
    catalog — :class:`ProfiledRestConnector.fingerprint` calls this with
    the splitter named in the profile and the raw value read from the
    fingerprint endpoint's literal top-level key. A blank / non-string
    *raw* yields ``(None, None)`` so a malformed appliance response never
    crashes the fingerprint round-trip (mirrors the hand-coded
    ``_parse_harbor_version`` / ``_parse_vrli_version`` tolerance).

    The match is exhaustive over the ``Literal``; an unlisted splitter is
    unreachable (the API boundary + boot guard reject it), but a final
    ``raise`` keeps the function total for the type checker.
    """
    if not isinstance(raw, str) or not raw:
        return None, None
    if splitter == "none":
        return raw, None
    if splitter == "dash":
        if "-" in raw:
            version_str, build_str = raw.split("-", 1)
            return version_str or None, build_str or None
        return raw, None
    if splitter == "vrli_five_part":
        parts = raw.split(".")
        version = ".".join(parts[0:3]) if len(parts) >= 3 else raw
        build = parts[4] if len(parts) > 4 else None
        return version, build
    raise ValueError(f"unknown version splitter {splitter!r}")  # pragma: no cover


class FingerprintSpec(BaseModel):
    """Declarative fingerprint recipe for a profiled connector.

    Names the GET endpoint a profiled connector calls to fingerprint the
    upstream, the literal top-level response key carrying the version
    string, and a *named* :data:`VersionSplitter` that renders it into
    ``(version, build)``. Carries **no** path expression / regex /
    template — ``version_key`` is a single literal top-level key (the same
    #1177 line as :class:`PaginationSpec`), and the splitter is a closed
    enum, not a format string. The reviewer enforces "literal top-level
    key, no dotted paths" exactly as for pagination.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="GET path the fingerprint reads (e.g. '/api/v2.0/systeminfo').")
    authenticated: bool = Field(
        default=True,
        description=(
            "Whether the fingerprint GET requires the profile's auth headers. "
            "False for unauthenticated version endpoints (vRLI's "
            "'/api/v2/version')."
        ),
    )
    version_key: str = Field(
        description=(
            "A single literal top-level response key carrying the version "
            "string (e.g. 'harbor_version', 'version'). NOT a dotted path / "
            "JSONPath / wildcard — the value is read as response[version_key]. "
            "The 'no dotted paths' constraint is review-enforced."
        ),
    )
    version_splitter: VersionSplitter = Field(
        default="none",
        description=(
            "Named splitter (closed enum) rendering the version value into "
            "(version, build). 'none' = verbatim; 'dash' = harbor's first-'-' "
            "split; 'vrli_five_part' = vRLI's 5-part dot split."
        ),
    )

    @field_validator("path", "version_key")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fingerprint path and version_key must be non-blank")
        return value

    @field_validator("version_key")
    @classmethod
    def _version_key_is_literal_top_level(cls, value: str) -> str:
        """Reject a dotted / wildcard version_key — it must be a literal key.

        The #1177 substrate-minimalism line: response-field selection is a
        single literal top-level key, never a path expression. A ``.`` / ``[``
        / ``*`` in the key means the operator is trying to author a JSONPath;
        refuse it at the schema boundary so the rejected-DSL door stays shut.
        """
        if any(ch in value for ch in (".", "[", "]", "*")):
            raise ValueError(
                f"version_key must be a literal top-level response key, not a "
                f"dotted/wildcard path: {value!r} (no JSONPath — #1177)"
            )
        return value


class ProbeSpec(BaseModel):
    """Declarative probe recipe, or the ``'delegate'`` sentinel.

    A profile either delegates its probe to the fingerprint round-trip
    (the SDDC Manager / NSX precedent — :attr:`ProbeSpec` is the string
    ``'delegate'`` in that case, modelled on :class:`ExecutionProfile`)
    or names a dedicated health GET: its :attr:`path`, the literal
    top-level :attr:`ok_field` to read, and the :attr:`ok_value` that
    field must equal for ``ok=True`` (harbor's ``GET /api/v2.0/health``
    with ``status == 'healthy'``).

    ``ok_field`` is a single literal top-level key — the same
    no-dotted-paths line as :attr:`FingerprintSpec.version_key`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="GET health path (e.g. '/api/v2.0/health').")
    ok_field: str = Field(
        description=(
            "A single literal top-level response key whose value decides "
            "reachability (e.g. 'status'). NOT a dotted path — read as "
            "response[ok_field]. The 'no dotted paths' constraint is "
            "review-enforced."
        ),
    )
    ok_value: str = Field(
        description="The value ok_field must equal for ok=True (e.g. 'healthy').",
    )

    @field_validator("path", "ok_field", "ok_value")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("probe path, ok_field and ok_value must be non-blank")
        return value

    @field_validator("ok_field")
    @classmethod
    def _ok_field_is_literal_top_level(cls, value: str) -> str:
        """Reject a dotted / wildcard ok_field — literal top-level key only (#1177)."""
        if any(ch in value for ch in (".", "[", "]", "*")):
            raise ValueError(
                f"ok_field must be a literal top-level response key, not a "
                f"dotted/wildcard path: {value!r} (no JSONPath — #1177)"
            )
        return value


class CursorTokenPagination(BaseModel):
    """The cursor-token pagination strategy (e.g. gcloud's ``nextPageToken``).

    The list op sends the cursor under request param :attr:`req_param`
    (``pageToken``); the response carries the next cursor under the
    literal top-level key :attr:`resp_field` (``nextPageToken``). The
    dispatch loop reads ``response[resp_field]``; when it is falsy the
    loop stops. Both are literal top-level keys — no dotted paths (#1177).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    req_param: str = Field(
        description="Request query-param name the next cursor is sent under (e.g. 'pageToken')."
    )
    resp_field: str = Field(
        description=(
            "A single literal top-level response key carrying the next cursor "
            "(e.g. 'nextPageToken'). NOT a dotted path — read as "
            "response[resp_field]. The 'no dotted paths' constraint is "
            "review-enforced."
        ),
    )

    @field_validator("req_param", "resp_field")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("req_param and resp_field must be non-blank")
        return value

    @field_validator("resp_field")
    @classmethod
    def _resp_field_is_literal_top_level(cls, value: str) -> str:
        """Reject a dotted / wildcard resp_field — literal top-level key only (#1177)."""
        if any(ch in value for ch in (".", "[", "]", "*")):
            raise ValueError(
                f"resp_field must be a literal top-level response key, not a "
                f"dotted/wildcard path: {value!r} (no JSONPath — #1177)"
            )
        return value


class PaginationSpec(BaseModel):
    """The named pagination strategy for a profiled connector's list ops.

    A profile selects exactly one strategy via the closed
    :attr:`strategy` enum and supplies the literal top-level
    :attr:`items_key` under which each page's rows live (gcloud's
    ``accounts``). The dispatch loop unwraps ``response[items_key]`` per
    page and concatenates.

    * ``none`` — single request, no looping. :attr:`cursor` must be
      ``None``; :attr:`items_key` still names the rows key for a
      consistent unwrapped shape.
    * ``cursor_token`` — follow a cursor token until exhausted
      (:class:`CursorTokenPagination`). :attr:`cursor` is required.

    Link-header / offset pagination are deliberately **not** modelled —
    they are net-new (file a separate task when a vendor needs them), per
    #1972's out-of-scope note. ``items_key`` is a single literal
    top-level key — no dotted paths (#1177).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: Literal["none", "cursor_token"] = Field(
        description="Named pagination strategy (closed enum: 'none' | 'cursor_token')."
    )
    items_key: str = Field(
        description=(
            "A single literal top-level response key under which each page's "
            "rows live (e.g. 'accounts', 'value'). NOT a dotted path — read as "
            "response[items_key]. The 'no dotted paths' constraint is "
            "review-enforced."
        ),
    )
    cursor: CursorTokenPagination | None = Field(
        default=None,
        description=(
            "Cursor config; required for strategy='cursor_token', forbidden for strategy='none'."
        ),
    )

    @field_validator("items_key")
    @classmethod
    def _items_key_is_literal_top_level(cls, value: str) -> str:
        """Reject a blank / dotted / wildcard items_key — literal top-level key only (#1177)."""
        if not value.strip():
            raise ValueError("items_key must be non-blank")
        if any(ch in value for ch in (".", "[", "]", "*")):
            raise ValueError(
                f"items_key must be a literal top-level response key, not a "
                f"dotted/wildcard path: {value!r} (no JSONPath — #1177)"
            )
        return value

    @model_validator(mode="after")
    def _cursor_matches_strategy(self) -> PaginationSpec:
        """Bind ``cursor`` to ``strategy='cursor_token'`` exclusively."""
        if self.strategy == "cursor_token":
            if self.cursor is None:
                raise ValueError("strategy='cursor_token' requires a cursor config")
        elif self.cursor is not None:
            raise ValueError(
                f"cursor is only valid for strategy='cursor_token', not {self.strategy!r}"
            )
        return self


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

    T7 (#1973) adds :attr:`expiry_statuses`, the single profile-declared
    session-expiry / auth-failure status set consumed by **both** the
    session-retry harness (T4 #1970) and the dispatcher's auth-class
    classification arm — replacing the duplicated connector-side
    ``_SESSION_EXPIRED_STATUSES`` + dispatcher-side ``_AUTH_FAILED_STATUSES``
    for profiled connectors. It parameterises the closed status *set* only;
    no per-status remediation grammar is introduced (classification stays
    central).

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
    fingerprint: FingerprintSpec = Field(
        description=(
            "Declarative fingerprint recipe: the GET path + literal version "
            "key + named version splitter. Drives "
            "ProfiledRestConnector.fingerprint (#1972)."
        ),
    )
    probe: Literal["delegate"] | ProbeSpec = Field(
        description=(
            "'delegate' (probe via the fingerprint round-trip) or a dedicated "
            "health-GET recipe. Drives ProfiledRestConnector.probe (#1972)."
        ),
    )
    pagination: PaginationSpec = Field(
        description=(
            "The named pagination strategy for the connector's list ops. "
            "Drives the ingested-dispatch pagination loop (#1972)."
        ),
    )
    expiry_statuses: frozenset[int] = Field(
        default=DEFAULT_EXPIRY_STATUSES,
        description=(
            "The non-2xx HTTP statuses this connector treats as a session "
            "expiry / auth failure. The SINGLE source of truth feeding both "
            "the session-retry harness (re-login once on one of these) and "
            "the dispatcher's auth-class classification arm. Defaults to "
            "{401}; an appliance with its own expiry code declares it here "
            "(vRLI: {401, 440}). NOT a status->action map — classification "
            "stays central; this only narrows the closed status set."
        ),
    )

    @field_validator("product", "version")
    @classmethod
    def _identity_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("product and version must be non-blank")
        return value

    @field_validator("expiry_statuses")
    @classmethod
    def _expiry_statuses_valid(cls, value: frozenset[int]) -> frozenset[int]:
        """Constrain the set to the connector-agnostic 401 floor plus vendor codes.

        Two rules, both keeping classification central rather than opening a
        status grammar:

        * ``401`` must be present. It is the connector-agnostic session-expiry
          floor every session connector re-logs in on; a profile that drops
          it would silently stop classifying the universal case.
        * Every other code must be a 4xx **vendor session-expiry** code
          (``>= 440``, ``< 500``) — the band vRLI's ``440`` lives in.
          The dispatcher classifies ``403`` (insufficient permission) and
          ``422`` (invalid payload) on their own dedicated arms, and treats
          ``404`` / ``429`` / 5xx as non-auth ``connector_error``; admitting
          any of those into the expiry set would let a profile silently
          re-route a status the central classifier owns. The closed
          ``{401} plus [440, 500)`` shape is a narrowing of the recognised
          set, not a per-status action map.
        """
        if not value:
            raise ValueError("expiry_statuses must name at least one status code")
        if 401 not in value:
            raise ValueError("expiry_statuses must include 401 (the session-expiry floor)")
        offending = sorted(code for code in value if code != 401 and not 440 <= code < 500)
        if offending:
            raise ValueError(
                f"expiry_statuses may only add 4xx vendor session-expiry codes "
                f"(>=440, like vRLI's 440) to the 401 floor; got {offending}. "
                f"403/422 are classified on their own dispatcher arms and "
                f"404/429/5xx are non-auth — classification stays central."
            )
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
