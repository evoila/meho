# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Adversarial tests for the flight-recorder redaction engine (Task #3213).

These tests are written from the attacker's chair: the engine's contract
is that a trace never contains a secret, and the F5 agent-access override
rests entirely on that. So the suite *plants* synthetic, PLACEHOLDER-shaped
secrets in every capture vector -- unknown headers, allowlisted header
values, declared and undeclared nested body paths, oversized bodies cut
mid-token, malformed JSON, binary bodies -- and asserts none survive into
the agent-readable output, and that every state the engine cannot prove
redacted comes back marked uncertain (the operator-only degrade).

All secrets here are synthetic and share the ``PLACEHOLDER`` sentinel so a
secret-scanner never trips on a real credential shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.redaction.flight_recorder import (
    BODY_OMITTED_MARKER,
    BODY_PATH_MARKER,
    HEADER_ALLOWLIST,
    SECRET_FAMILY_OMITTED_MARKER,
    UNPLACEABLE_FAMILY_MARKER,
    BodyExclusion,
    BodyPathRedactionConfig,
    classify_body_exclusion,
    redact_body,
    redact_headers,
    redact_span,
)
from meho_backplane.redaction.flight_recorder import bodies as fr_bodies
from meho_backplane.settings import get_settings

# --- Synthetic, PLACEHOLDER-shaped secrets ---------------------------------
# Each is crafted to match a credential *shape* the underlying Tier-1
# engine detects, so the shape net can be exercised without a real secret.
_BEARER = "Bearer PLACEHOLDER-TOKEN-abcdef0123456789"
_JWT = "eyJPLACEHOLDER0aaa.PLACEHOLDER0bbbbb.PLACEHOLDER0ccccc"
_AUTH_HEADER = "Authorization: Bearer PLACEHOLDER-abcdef012345"
_API_KEY = "api_key=PLACEHOLDER-KEY-abcdef012345"
_SENTINEL = "PLACEHOLDER"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings env so ``get_settings()`` resolves in the classifier.

    Mirrors the convention in ``test_service_grants.py`` -- the
    delete-shaped branch is single-sourced with the grant guard's
    ``Settings.service_grant_delete_shaped_patterns``.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _assert_no_secret(obj: Any, *needles: str) -> None:
    """Recursively assert none of *needles* appear anywhere in *obj*."""
    blob = repr(obj)
    for needle in needles:
        assert needle not in blob, f"secret {needle!r} survived in {blob!r}"


# ===========================================================================
# F2.1 -- header allowlist (fail-closed, allowlist not blocklist)
# ===========================================================================

_SECRET_HEADER_NAMES = [
    "Authorization",
    "Proxy-Authorization",
    "WWW-Authenticate",
    "Proxy-Authenticate",
    "Cookie",
    "Set-Cookie",
    "X-Api-Key",
    "X-Auth-Token",
    "X-Vault-Token",
    "X-Csrf-Token",
    "X-Xsrf-Token",
    "X-Amz-Security-Token",
    "X-Acme-Invented-Session",  # unknown vendor header -> allowlist must drop
    "Location",
    "Content-Location",
    "Referer",
    "X-Forwarded-For",
    "X-Real-Ip",
]


@pytest.mark.parametrize("name", _SECRET_HEADER_NAMES)
def test_header_allowlist_strips_every_secret_header(name: str) -> None:
    out = redact_headers({name: f"{_SENTINEL}-{name}-secret", "Content-Type": "application/json"})
    assert name.lower() not in out.value
    _assert_no_secret(out.value, _SENTINEL)
    # A dropped header is not an uncertainty: the allowlist proved it out.
    assert out.uncertain is False
    assert out.value.get("content-type") == "application/json"


def test_header_allowlist_is_case_insensitive() -> None:
    out = redact_headers({"CoNtEnT-TyPe": "application/json", "AUTHORIZATION": _BEARER})
    assert out.value == {"content-type": "application/json"}


def test_non_allowlisted_header_value_is_never_read() -> None:
    """A dropped header's value must be stripped *unread* (F2.1)."""

    class _ExplodingValue:
        def __str__(self) -> str:  # pragma: no cover - must never run
            raise AssertionError("non-allowlisted header value was read")

        def __repr__(self) -> str:  # pragma: no cover - must never run
            raise AssertionError("non-allowlisted header value was read")

    # No exception => the value was never touched.
    out = redact_headers({"X-Secret-Token": _ExplodingValue()})
    assert out.value == {}


def test_secret_smuggled_into_allowlisted_header_value_is_scrubbed() -> None:
    """Defense-in-depth: a shaped secret in a safe header is scrubbed."""
    out = redact_headers({"User-Agent": f"app/1.0 {_BEARER}", "Server": _JWT})
    _assert_no_secret(out.value, _SENTINEL)
    assert "user-agent" in out.value  # header kept, value scrubbed


def test_headers_not_a_mapping_is_uncertain() -> None:
    out = redact_headers(["Authorization", _BEARER])
    assert out.uncertain is True
    assert out.value == {}


def test_headers_none_is_certain_empty() -> None:
    out = redact_headers(None)
    assert out.uncertain is False
    assert out.value == {}


def test_allowlisted_header_non_string_value_dropped_not_uncertain() -> None:
    out = redact_headers({"Content-Length": 1234, "Content-Type": "application/json"})
    assert out.value == {"content-type": "application/json"}
    assert out.uncertain is False
    assert out.reasons  # a reason was recorded for the drop


def test_allowlist_membership_excludes_all_known_secret_names() -> None:
    for name in _SECRET_HEADER_NAMES:
        assert name.lower() not in HEADER_ALLOWLIST


# ===========================================================================
# F2.2 -- per-connector body-path redaction
# ===========================================================================


def test_body_path_config_validates_and_compiles_globs() -> None:
    cfg = BodyPathRedactionConfig(connector_id="acme", paths=("credentials", "items.*.password"))
    assert cfg.paths == ("credentials", "items.*.password")
    with pytest.raises(ValueError, match="blank"):
        BodyPathRedactionConfig(connector_id="acme", paths=("  ",))


def test_declared_top_level_path_redacts_whole_subtree() -> None:
    body = {"credentials": {"user": "u", "pass": f"{_SENTINEL}-pw"}, "name": "keep"}
    out = redact_body(body, paths=("credentials",), content_type="application/json")
    assert out.value["credentials"] == BODY_PATH_MARKER
    assert out.value["name"] == "keep"
    assert out.uncertain is False
    _assert_no_secret(out.value, _SENTINEL)


def test_declared_nested_and_glob_paths_redact() -> None:
    body = {
        "items": [
            {"password": f"{_SENTINEL}-a"},
            {"password": f"{_SENTINEL}-b"},
        ],
        "deep": {"level": {"secret": f"{_SENTINEL}-c"}},
    }
    out = redact_body(
        body,
        paths=("items.*.password", "**.secret"),
        content_type="application/json",
    )
    assert out.value["items"][0]["password"] == BODY_PATH_MARKER
    assert out.value["items"][1]["password"] == BODY_PATH_MARKER
    assert out.value["deep"]["level"]["secret"] == BODY_PATH_MARKER
    _assert_no_secret(out.value, _SENTINEL)


def test_undeclared_credential_shaped_value_caught_by_shape_net() -> None:
    """A shaped secret at an *undeclared* nested path must not survive."""
    body = {"a": {"b": {"c": _BEARER}}, "note": _JWT, "auth_line": _AUTH_HEADER}
    out = redact_body(body, paths=(), content_type="application/json")
    _assert_no_secret(out.value, _SENTINEL)
    assert out.uncertain is False


def test_non_secret_content_survives_redaction() -> None:
    body = {"vm": "vm-42", "power": "on", "count": 3}
    out = redact_body(body, paths=("credentials",), content_type="application/json")
    assert out.value == body


def test_raw_json_string_body_is_parsed_and_redacted() -> None:
    out = redact_body(
        '{"password_field": "keep", "token_line": "token=PLACEHOLDER-abcdefgh"}',
        paths=("password_field",),
        content_type="application/json",
    )
    assert out.value["password_field"] == BODY_PATH_MARKER
    _assert_no_secret(out.value, _SENTINEL)


def test_json_bytes_body_is_decoded_and_redacted() -> None:
    out = redact_body(
        b'{"secret": "PLACEHOLDER-x", "ok": 1}',
        paths=("secret",),
        content_type="application/json",
    )
    assert out.value["secret"] == BODY_PATH_MARKER
    assert out.value["ok"] == 1


# ===========================================================================
# F2 -- redaction-uncertainty (fail-closed on every ambiguity)
# ===========================================================================


def test_malformed_json_is_uncertain_and_omitted() -> None:
    out = redact_body('{"a": "PLACEHOLDER-secret", ', content_type="application/json")
    assert out.uncertain is True
    assert out.value == BODY_OMITTED_MARKER
    _assert_no_secret(out.value, _SENTINEL)


def test_binary_body_is_uncertain_and_omitted() -> None:
    out = redact_body(b"\x00\x01PLACEHOLDER\xff\xfe", content_type="application/octet-stream")
    assert out.uncertain is True
    assert out.value == BODY_OMITTED_MARKER


def test_non_utf8_json_typed_body_is_uncertain() -> None:
    out = redact_body(b"\xff\xfePLACEHOLDER", content_type="application/json")
    assert out.uncertain is True
    assert out.value == BODY_OMITTED_MARKER


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "text/html", "application/xml"],
)
def test_known_non_json_content_type_is_uncertain(content_type: str) -> None:
    out = redact_body("password=PLACEHOLDER-pw123456", content_type=content_type)
    assert out.uncertain is True
    assert out.value == BODY_OMITTED_MARKER
    _assert_no_secret(out.value, _SENTINEL)


def test_unknown_content_type_non_json_string_is_uncertain() -> None:
    out = redact_body("this is not json PLACEHOLDER", content_type=None)
    assert out.uncertain is True
    _assert_no_secret(out.value, _SENTINEL)


def test_unknown_content_type_valid_json_string_is_certain() -> None:
    out = redact_body('{"ok": true}', content_type=None)
    assert out.uncertain is False
    assert out.value == {"ok": True}


def test_truncated_body_is_uncertain_even_when_parseable() -> None:
    out = redact_body({"partial": "data"}, content_type="application/json", truncated=True)
    assert out.uncertain is True
    assert "truncated" in " ".join(out.reasons)


def test_oversized_body_truncated_mid_secret_never_leaks_to_agent() -> None:
    """A body cut mid-token: whatever the parse outcome, it is uncertain."""
    big = {"filler": "x" * 100_000, "tail_secret": _BEARER}
    import json as _json

    serialized = _json.dumps(big)
    cut = serialized[: serialized.index("Bearer") + 10]  # slice through the token
    out = redact_body(cut, content_type="application/json", truncated=True)
    assert out.uncertain is True
    _assert_no_secret(out.value, _SENTINEL)


def test_body_path_runtime_fault_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any fault while matching declared globs drops the body, uncertain."""

    def _boom(_globs: Any, _path: str) -> bool:
        raise RuntimeError("glob engine exploded")

    monkeypatch.setattr(fr_bodies, "path_matches", _boom)
    out = redact_body({"x": _BEARER}, paths=("x",), content_type="application/json")
    assert out.uncertain is True
    assert out.value == BODY_OMITTED_MARKER
    _assert_no_secret(out.value, _SENTINEL)


def test_empty_body_is_certain_none() -> None:
    for empty in (None, "", "   ", b""):
        out = redact_body(empty, content_type="application/json")
        assert out.uncertain is False
        assert out.value is None


# ===========================================================================
# F2.3 -- hard-excluded op families (single-sourced with delete-shaped)
# ===========================================================================

_SECRET_FAMILY_OPS = [
    "vault.sys.auth.enable",
    "keycloak.user.reset_password",
    "rke2.token.rotate",
    "k8s.secret.create",
    "sddc.credential.list",
    "secret.move",
    "acme.session.login",
    "acme.session.logout",
    "provider.oauth.exchange",
    "idp.token.mint",
    "GET:/key",
    "GET:/api/keys",
    "POST:/auth/login",
]


@pytest.mark.parametrize("op_id", _SECRET_FAMILY_OPS)
def test_secret_bearing_family_never_records_body(op_id: str) -> None:
    result = classify_body_exclusion(op_id)
    assert result.excluded is True
    assert result.family == "secret-bearing"
    # A placed exclusion is certain -- deliberate, safe omission.
    assert result.uncertain is False


def test_secret_bearing_tag_excludes_body() -> None:
    result = classify_body_exclusion("acme.generic.op", tags=["session"])
    assert result.excluded is True
    assert result.family == "secret-bearing"


@pytest.mark.parametrize(
    "op_id",
    ["DELETE:/vms/{id}", "vmware.vm.delete", "cluster.node.destroy", "cache.entries.purge"],
)
def test_destructive_family_excluded_via_settings_single_source(op_id: str) -> None:
    result = classify_body_exclusion(op_id)
    assert result.excluded is True
    assert result.family == "destructive"


def test_delete_method_excludes_body() -> None:
    result = classify_body_exclusion("acme.thing.remove_it", method="delete")
    assert result.excluded is True
    assert result.family == "destructive"


def test_destructive_tag_excludes_body() -> None:
    result = classify_body_exclusion("acme.thing.wipe", tags=["destructive"])
    assert result.excluded is True
    assert result.family == "destructive"


def test_delete_shaped_is_single_sourced_with_grant_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classifier must read the *same* settings tuple the grant guard uses."""
    monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", "acme.custom.nuke")
    get_settings.cache_clear()
    # The custom pattern now excludes; a formerly delete-shaped default does not.
    assert classify_body_exclusion("acme.custom.nuke").excluded is True
    result_default = classify_body_exclusion("thing.remove")
    # 'thing.remove' is not in the overridden set, and 'remove' is not a
    # secret-family word -> no longer excluded, proving the source is settings.
    assert result_default.excluded is False


@pytest.mark.parametrize("op_id", ["vmware.vm.list", "GET:/vms", "nsx.segment.get"])
def test_benign_read_ops_are_not_excluded(op_id: str) -> None:
    assert classify_body_exclusion(op_id).excluded is False


@pytest.mark.parametrize("op_id", [None, "", "   "])
def test_unplaceable_op_is_excluded_and_uncertain(op_id: str | None) -> None:
    result = classify_body_exclusion(op_id)
    assert isinstance(result, BodyExclusion)
    assert result.excluded is True
    assert result.uncertain is True  # F5: cannot place -> operator-only


# ===========================================================================
# F2 -- span combiner (the capture-side entry point)
# ===========================================================================


def test_span_secret_op_records_no_body_but_stays_certain() -> None:
    span = redact_span(
        op_id="keycloak.token.mint",
        connector_id="keycloak-1.0",
        method="POST",
        request_headers={"Authorization": _BEARER, "Content-Type": "application/json"},
        request_body={"password": "PLACEHOLDER-pw"},
        response_body={"access_token": "PLACEHOLDER-tok"},
        request_content_type="application/json",
        response_content_type="application/json",
    )
    assert span.body_recorded is False
    assert span.request_body == SECRET_FAMILY_OMITTED_MARKER
    assert span.response_body == SECRET_FAMILY_OMITTED_MARKER
    assert span.uncertain is False
    assert "authorization" not in span.request_headers
    _assert_no_secret(span, _SENTINEL)


def test_span_unplaceable_op_is_uncertain() -> None:
    span = redact_span(op_id=None, request_body={"a": 1}, request_content_type="application/json")
    assert span.uncertain is True
    assert span.request_body == UNPLACEABLE_FAMILY_MARKER


def test_span_uncertain_body_propagates_to_span_verdict() -> None:
    span = redact_span(
        op_id="vmware.vm.list",
        response_body=b"\x00PLACEHOLDER\xff",
        response_content_type="application/octet-stream",
    )
    assert span.uncertain is True
    _assert_no_secret(span, _SENTINEL)


def test_span_end_to_end_all_vectors_planted() -> None:
    """Plant a secret in every vector at once; none may reach the agent view."""
    span = redact_span(
        op_id="vmware.vm.get",
        connector_id="vmware-rest-9.0",
        method="GET",
        tags=["read"],
        request_headers={"X-Vault-Token": f"{_SENTINEL}-req", "Accept": "application/json"},
        response_headers={"Set-Cookie": f"session={_SENTINEL}", "Content-Type": "application/json"},
        request_body={"note": _BEARER},
        response_body={"config": {"creds": _JWT}, "secret_path": "PLACEHOLDER-bare"},
        request_content_type="application/json",
        response_content_type="application/json",
        body_paths=("config.creds", "secret_path"),
    )
    assert span.uncertain is False  # everything provably redacted
    assert span.body_recorded is True
    assert span.response_body["config"]["creds"] == BODY_PATH_MARKER
    assert span.response_body["secret_path"] == BODY_PATH_MARKER
    assert "x-vault-token" not in span.request_headers
    assert "set-cookie" not in span.response_headers
    _assert_no_secret(span, _SENTINEL)


# ===========================================================================
# Property / adversarial battery -- shaped secrets at many nested locations
# ===========================================================================

_NEST_SHAPES: list[Any] = [
    lambda s: {"x": s},
    lambda s: {"a": {"b": {"c": s}}},
    lambda s: [s, "ok"],
    lambda s: {"items": [{"v": s}, {"v": "ok"}]},
    lambda s: {"deep": [[{"k": s}]]},
    lambda s: {"mixed": {"list": ["ok", {"leaf": s}]}},
]


@pytest.mark.parametrize("shape", _NEST_SHAPES)
@pytest.mark.parametrize("secret", [_BEARER, _JWT, _AUTH_HEADER, _API_KEY])
def test_property_shaped_secret_never_survives_anywhere(shape: Any, secret: str) -> None:
    """A credential-shaped secret at any nesting is scrubbed by the shape net."""
    body = shape(secret)
    out = redact_body(body, paths=(), content_type="application/json")
    assert out.uncertain is False
    _assert_no_secret(out.value, _SENTINEL)


@pytest.mark.parametrize("shape", _NEST_SHAPES)
def test_property_declared_path_redacts_any_value_shape(shape: Any) -> None:
    """A declared '**' path scrubs a value of ANY shape (not just credential-shaped)."""
    body = shape("PLACEHOLDER-opaque-nonshaped-value")
    # '**' matches any leaf path -> every leaf becomes the path marker.
    out = redact_body(body, paths=("**",), content_type="application/json")
    assert out.uncertain is False
    _assert_no_secret(out.value, _SENTINEL)
