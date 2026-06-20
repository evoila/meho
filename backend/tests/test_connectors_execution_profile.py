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
    NAMED_AUTH_SCHEMES,
    RESERVED_AUTH_SCHEMES,
    AuthSchemeName,
    AuthSpec,
    ExecutionProfile,
    ExecutionProfileError,
    ReservedAuthSchemeError,
    UnknownAuthSchemeError,
    validate_execution_profile,
)


def _profile(**auth_overrides: object) -> ExecutionProfile:
    """Build a valid ExecutionProfile with a basic auth block by default."""
    auth = {"scheme": "basic", "secret_fields": ("username", "password")}
    auth.update(auth_overrides)
    return ExecutionProfile(product="harbor", version="2.x", auth=AuthSpec(**auth))


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
    assert fields == {"scheme", "secret_fields", "header_name", "value_kind"}
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
