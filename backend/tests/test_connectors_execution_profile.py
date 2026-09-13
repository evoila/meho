# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``ExecutionProfile`` schema + closed auth catalog (#1969).

Covers the four acceptance criteria of G0.28-T3:

* the ``Literal`` auth-scheme catalog carries no path/template/extractor
  fields;
* boot-load (``validate_execution_profile``) crashes on an unknown scheme,
  and the API boundary (Pydantic ``Literal``) rejects an unknown scheme;
* a reserved scheme produces a distinct typed error naming the
  typed-connector alternative.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from meho_backplane.connectors.profile import (
    DEFAULT_EXPIRY_STATUSES,
    NAMED_AUTH_SCHEMES,
    RESERVED_AUTH_SCHEMES,
    AuthSchemeName,
    AuthSpec,
    ExecutionProfile,
    ExecutionProfileError,
    FingerprintSpec,
    PaginationSpec,
    ReservedAuthSchemeError,
    UnknownAuthSchemeError,
    validate_execution_profile,
)

_FINGERPRINT = FingerprintSpec(
    path="/api/v2.0/systeminfo",
    version_key="harbor_version",
    version_splitter="dash",
)
_PAGINATION = PaginationSpec(strategy="none", items_key="value")


def _profile(**auth_overrides: object) -> ExecutionProfile:
    """Build a valid ExecutionProfile with a basic auth block by default."""
    auth = {"scheme": "basic", "secret_fields": ("username", "password")}
    auth.update(auth_overrides)
    return ExecutionProfile(
        product="harbor",
        version="2.x",
        auth=AuthSpec(**auth),
        fingerprint=_FINGERPRINT,
        probe="delegate",
        pagination=_PAGINATION,
    )


# --------------------------------------------------------------------------
# Catalog shape — no DSL fields, closed Literal
# --------------------------------------------------------------------------


def test_named_schemes_match_literal_exactly() -> None:
    """The runtime named set is derived from the Literal — no drift."""
    assert frozenset(typing.get_args(AuthSchemeName)) == NAMED_AUTH_SCHEMES
    assert {
        "basic",
        "static_header",
        "session_login",
        "session_login_basic",
        "session_login_token",
        "oauth2_mint",
    } == NAMED_AUTH_SCHEMES


def test_named_and_reserved_sets_are_disjoint() -> None:
    assert NAMED_AUTH_SCHEMES.isdisjoint(RESERVED_AUTH_SCHEMES)


def test_auth_spec_has_no_dsl_fields() -> None:
    """AuthSpec must carry no path/template/expression/extractor field.

    This is the #1177 rejected-DSL line, enforced as a schema-shape
    assertion so a future edit that adds a token_location/field_map/etc.
    fails this test loudly.
    """
    fields = set(AuthSpec.model_fields)
    # The oauth2_mint external-issuer fields (token_url/scope/audience, #3571)
    # name *where* the token is minted and *what* scope/audience the grant
    # carries — a single absolute endpoint + two opaque strings, never a
    # path/template/expression the substrate would interpret. They stay on
    # the right side of the #1177 line, so they are added to the expected
    # set rather than to `forbidden`.
    assert fields == {
        "scheme",
        "secret_fields",
        "header_name",
        "value_kind",
        "token_url",
        "scope",
        "audience",
    }
    forbidden = {
        "token_location",
        "field_map",
        "value_template",
        "jsonpath",
        "json_path",
        "expression",
        "extractor",
        "path",
        "template",
    }
    assert fields.isdisjoint(forbidden)


def test_profile_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionProfile(
            product="harbor",
            version="2.x",
            auth=AuthSpec(scheme="basic", secret_fields=("username", "password")),
            fingerprint=_FINGERPRINT,
            probe="delegate",
            pagination=_PAGINATION,
            token_location="header",  # type: ignore[call-arg]
        )


def test_profile_is_frozen() -> None:
    profile = _profile()
    with pytest.raises(ValidationError):
        profile.product = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------
# API-boundary rejection (Pydantic Literal)
# --------------------------------------------------------------------------


def test_unknown_scheme_rejected_at_boundary() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="totally_made_up", secret_fields=("token",))  # type: ignore[arg-type]


def test_reserved_scheme_rejected_at_boundary() -> None:
    """A reserved scheme is not in the Literal, so the boundary rejects it too."""
    with pytest.raises(ValidationError):
        AuthSpec(scheme="kubeconfig", secret_fields=("kubeconfig",))  # type: ignore[arg-type]


@pytest.mark.parametrize("scheme", sorted(NAMED_AUTH_SCHEMES))
def test_every_named_scheme_constructs(scheme: str) -> None:
    extra: dict[str, object] = {}
    if scheme == "static_header":
        extra["value_kind"] = "bearer"
    spec = AuthSpec(scheme=scheme, secret_fields=("token",), **extra)  # type: ignore[arg-type]
    assert spec.scheme == scheme


# --------------------------------------------------------------------------
# secret_fields / header_name validation
# --------------------------------------------------------------------------


def test_secret_fields_must_be_nonempty() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="basic", secret_fields=())


def test_secret_fields_must_be_nonblank() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="basic", secret_fields=("username", "  "))


def test_header_name_defaults_to_authorization() -> None:
    assert AuthSpec(scheme="basic", secret_fields=("username",)).header_name == "Authorization"


def test_header_name_must_be_nonblank() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="basic", secret_fields=("username",), header_name=" ")


# --------------------------------------------------------------------------
# value_kind bound to static_header
# --------------------------------------------------------------------------


def test_static_header_requires_value_kind() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="static_header", secret_fields=("token",))


def test_static_header_value_kind_raw_and_bearer() -> None:
    for kind in ("bearer", "raw"):
        spec = AuthSpec(scheme="static_header", secret_fields=("token",), value_kind=kind)  # type: ignore[arg-type]
        assert spec.value_kind == kind


def test_value_kind_forbidden_for_non_static_header() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="basic", secret_fields=("username",), value_kind="bearer")


def test_value_kind_literal_closed() -> None:
    with pytest.raises(ValidationError):
        AuthSpec(scheme="static_header", secret_fields=("token",), value_kind="custom")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# oauth2_mint external-issuer fields (token_url / scope / audience, #3571)
# --------------------------------------------------------------------------

_EXTERNAL_ISSUER_URL = "https://idp.example.test/realms/example/protocol/openid-connect/token"


def test_oauth2_mint_defaults_have_no_external_issuer_fields() -> None:
    """Omitting the new fields keeps the byte-identical target-relative mint."""
    spec = AuthSpec(scheme="oauth2_mint", secret_fields=("client_id", "client_secret"))
    assert spec.token_url is None
    assert spec.scope is None
    assert spec.audience is None


def test_oauth2_mint_accepts_external_issuer_fields() -> None:
    """An external https issuer + scope + audience validate on oauth2_mint."""
    spec = AuthSpec(
        scheme="oauth2_mint",
        secret_fields=("client_id", "client_secret"),
        token_url=_EXTERNAL_ISSUER_URL,
        scope="svc",
        audience="downstream-api",
    )
    assert spec.token_url == _EXTERNAL_ISSUER_URL
    assert spec.scope == "svc"
    assert spec.audience == "downstream-api"


@pytest.mark.parametrize("field", ["token_url", "scope", "audience"])
@pytest.mark.parametrize("scheme", ["basic", "static_header", "session_login"])
def test_external_issuer_fields_forbidden_on_non_oauth2_scheme(field: str, scheme: str) -> None:
    """token_url/scope/audience are rejected on every scheme but oauth2_mint."""
    auth: dict[str, object] = {"scheme": scheme, "secret_fields": ("token",)}
    if scheme == "static_header":
        auth["value_kind"] = "bearer"
    auth[field] = _EXTERNAL_ISSUER_URL if field == "token_url" else "v"
    with pytest.raises(ValidationError, match=r"only.*valid for the oauth2_mint scheme"):
        AuthSpec(**auth)  # type: ignore[arg-type]


def test_token_url_allows_https_public_host() -> None:
    AuthSpec(
        scheme="oauth2_mint",
        secret_fields=("client_id", "client_secret"),
        token_url=_EXTERNAL_ISSUER_URL,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://keycloak:8080/realms/example/protocol/openid-connect/token",
        "http://kc.svc.cluster.local/token",
        "http://kc.internal/token",
        "http://10.1.2.3/token",
        "http://127.0.0.1:8080/token",
        "http://[::1]:8080/token",
    ],
)
def test_token_url_allows_http_for_internal_host(url: str) -> None:
    """Plaintext http is accepted only for a cluster-internal / private issuer."""
    spec = AuthSpec(
        scheme="oauth2_mint",
        secret_fields=("client_id", "client_secret"),
        token_url=url,
    )
    assert spec.token_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://idp.example.test/token",  # dotted public FQDN
        "http://8.8.8.8/token",  # globally-routable public IP
    ],
)
def test_token_url_rejects_http_for_public_host(url: str) -> None:
    with pytest.raises(ValidationError, match="must use https for a public host"):
        AuthSpec(
            scheme="oauth2_mint",
            secret_fields=("client_id", "client_secret"),
            token_url=url,
        )


@pytest.mark.parametrize(
    "url",
    [
        "/realms/example/protocol/openid-connect/token",  # relative, not absolute
        "ftp://idp.example.test/token",  # non-http(s) scheme
        "https:///token",  # no host
        "   ",  # blank
    ],
)
def test_token_url_rejects_non_absolute_http_url(url: str) -> None:
    with pytest.raises(ValidationError):
        AuthSpec(
            scheme="oauth2_mint",
            secret_fields=("client_id", "client_secret"),
            token_url=url,
        )


@pytest.mark.parametrize("field", ["scope", "audience"])
def test_scope_audience_reject_blank(field: str) -> None:
    with pytest.raises(ValidationError, match="non-blank"):
        AuthSpec(
            scheme="oauth2_mint",
            secret_fields=("client_id", "client_secret"),
            **{field: "   "},
        )


def test_boot_guard_passes_for_external_issuer_profile() -> None:
    """The startup-load scheme guard accepts a profile carrying the new fields."""
    profile = ExecutionProfile(
        product="acme",
        version="1",
        auth=AuthSpec(
            scheme="oauth2_mint",
            secret_fields=("client_id", "client_secret"),
            token_url=_EXTERNAL_ISSUER_URL,
            scope="svc",
            audience="downstream-api",
        ),
        fingerprint=_FINGERPRINT,
        probe="delegate",
        pagination=_PAGINATION,
    )
    # Does not raise — the guard partitions on scheme only; the optional
    # fields are transparent to it.
    validate_execution_profile(profile)


# --------------------------------------------------------------------------
# Startup-load boot guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", sorted(NAMED_AUTH_SCHEMES))
def test_validate_passes_for_named_schemes(scheme: str) -> None:
    extra: dict[str, object] = {}
    if scheme == "static_header":
        extra["value_kind"] = "raw"
    profile = ExecutionProfile(
        product="p",
        version="1",
        auth=AuthSpec(scheme=scheme, secret_fields=("token",), **extra),  # type: ignore[arg-type]
        fingerprint=_FINGERPRINT,
        probe="delegate",
        pagination=_PAGINATION,
    )
    # No raise.
    validate_execution_profile(profile)


def test_validate_raises_reserved_for_reserved_scheme() -> None:
    """A reserved scheme reaching the boot guard raises the distinct typed error.

    Constructed via model_construct to bypass the Literal (simulating a
    hand-edited stored row that reached the registry off the validated path).
    """
    auth = AuthSpec.model_construct(
        scheme="kubeconfig",  # type: ignore[arg-type]
        secret_fields=("kubeconfig",),
        header_name="Authorization",
        value_kind=None,
    )
    profile = ExecutionProfile.model_construct(product="k8s", version="1", auth=auth)
    with pytest.raises(ReservedAuthSchemeError) as exc:
        validate_execution_profile(profile)
    assert exc.value.scheme == "kubeconfig"
    # Distinct remediation: author a typed connector, NOT the auto-shim message.
    assert "typed connector" in str(exc.value)
    assert "unreplaced_auto_shim" not in str(exc.value)
    assert isinstance(exc.value, ExecutionProfileError)


def test_validate_raises_unknown_for_bogus_scheme() -> None:
    auth = AuthSpec.model_construct(
        scheme="nonsense",  # type: ignore[arg-type]
        secret_fields=("token",),
        header_name="Authorization",
        value_kind=None,
    )
    profile = ExecutionProfile.model_construct(product="x", version="1", auth=auth)
    with pytest.raises(UnknownAuthSchemeError) as exc:
        validate_execution_profile(profile)
    # Names the valid named schemes to guide the operator.
    assert "basic" in str(exc.value)


def test_reserved_and_unknown_errors_are_distinct() -> None:
    assert issubclass(ReservedAuthSchemeError, ExecutionProfileError)
    assert issubclass(UnknownAuthSchemeError, ExecutionProfileError)
    assert not issubclass(ReservedAuthSchemeError, UnknownAuthSchemeError)
    assert not issubclass(UnknownAuthSchemeError, ReservedAuthSchemeError)


def test_reserved_set_names_typed_connectors() -> None:
    """Each reserved scheme corresponds to a real typed connector's auth shape."""
    assert {
        "github_app_jwt",
        "gcp_sa_impersonation",
        "operator_jwt_forward",
        "kubeconfig",
        "cookie_jar_session",
        "dual_plane_session",
    } == RESERVED_AUTH_SCHEMES


# --------------------------------------------------------------------------
# expiry_statuses — the single profile-declared source (#1973)
# --------------------------------------------------------------------------


def test_expiry_statuses_defaults_to_401() -> None:
    """A profile omitting the field gets the connector-agnostic {401} floor."""
    profile = _profile()
    assert profile.expiry_statuses == frozenset({401})
    assert profile.expiry_statuses == DEFAULT_EXPIRY_STATUSES


def test_expiry_statuses_vrli_declares_401_and_440() -> None:
    """The vRLI appliance declares its own 440 expiry code alongside 401."""
    profile = ExecutionProfile(
        product="vcf_logs",
        version="2.x",
        auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
        fingerprint=_FINGERPRINT,
        probe="delegate",
        pagination=_PAGINATION,
        expiry_statuses=frozenset({401, 440}),
    )
    assert profile.expiry_statuses == frozenset({401, 440})


def test_expiry_statuses_coerces_a_list() -> None:
    """A JSON-shaped list round-trips into the frozenset field."""
    profile = ExecutionProfile.model_validate(
        {
            "product": "vcf_logs",
            "version": "2.x",
            "auth": {"scheme": "session_login", "secret_fields": ["username", "password"]},
            "fingerprint": _FINGERPRINT.model_dump(),
            "probe": "delegate",
            "pagination": _PAGINATION.model_dump(),
            "expiry_statuses": [401, 440],
        }
    )
    assert profile.expiry_statuses == frozenset({401, 440})


def test_expiry_statuses_rejects_empty_set() -> None:
    """Every profile recognises at least one expiry status."""
    with pytest.raises(ValidationError, match="at least one status"):
        ExecutionProfile(
            product="harbor",
            version="2.x",
            auth=AuthSpec(scheme="basic", secret_fields=("username", "password")),
            fingerprint=_FINGERPRINT,
            probe="delegate",
            pagination=_PAGINATION,
            expiry_statuses=frozenset(),
        )


@pytest.mark.parametrize("bad_status", [200, 302, 403, 404, 422, 429, 500, 503])
def test_expiry_statuses_rejects_non_expiry_status(bad_status: int) -> None:
    """Only the 401 floor + 4xx vendor codes (>=440) are admissible."""
    with pytest.raises(ValidationError, match="vendor session-expiry"):
        ExecutionProfile(
            product="harbor",
            version="2.x",
            auth=AuthSpec(scheme="basic", secret_fields=("username", "password")),
            fingerprint=_FINGERPRINT,
            probe="delegate",
            pagination=_PAGINATION,
            expiry_statuses=frozenset({401, bad_status}),
        )


def test_expiry_statuses_requires_401_floor() -> None:
    """Dropping 401 stops classifying the connector-agnostic expiry case."""
    with pytest.raises(ValidationError, match="must include 401"):
        ExecutionProfile(
            product="vcf_logs",
            version="2.x",
            auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
            fingerprint=_FINGERPRINT,
            probe="delegate",
            pagination=_PAGINATION,
            expiry_statuses=frozenset({440}),
        )


def test_expiry_statuses_serialization_roundtrip() -> None:
    """A profile round-trips through JSON preserving its declared set."""
    profile = ExecutionProfile(
        product="vcf_logs",
        version="2.x",
        auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
        fingerprint=_FINGERPRINT,
        probe="delegate",
        pagination=_PAGINATION,
        expiry_statuses=frozenset({401, 440}),
    )
    restored = ExecutionProfile.model_validate_json(profile.model_dump_json())
    assert restored.expiry_statuses == frozenset({401, 440})


def test_expiry_statuses_extra_forbid_unaffected() -> None:
    """The field is a known key; an unrelated extra key still fails."""
    with pytest.raises(ValidationError):
        ExecutionProfile(
            product="harbor",
            version="2.x",
            auth=AuthSpec(scheme="basic", secret_fields=("username", "password")),
            fingerprint=_FINGERPRINT,
            probe="delegate",
            pagination=_PAGINATION,
            bogus="x",  # type: ignore[call-arg]
        )
