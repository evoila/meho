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
    OVF_PROPERTY_VALUE_MARKER,
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
# F2.2 -- OVF PropertyParams structural redaction
# ===========================================================================
#
# A vSphere OVF deploy body carries operator-supplied property values in a
# type-discriminated union member ``{type: "PropertyParams", properties:
# [{id, value}]}`` under a dynamic ``additional_parameters[]`` index. Those
# values are OVF property inputs that commonly carry appliance credentials,
# keyed on a vendor-specific ``id`` no key-name heuristic or credential-shape
# net can place -- so a plaintext value would otherwise round-trip into the
# trace. The structural rule redacts every property value while leaving the
# id and every non-secret field intact. The property values below are fake
# placeholder strings, deliberately *not* credential-shaped, so the test
# proves the *structural* rule fires (not the underlying shape net).


def _ovf_deploy_body() -> dict[str, Any]:
    """A synthetic OVF deploy request body with a PropertyParams block."""
    return {
        "deployment_spec": {
            "accept_all_eula": True,
            "name": "keep-vm-name",
            "network_mappings": {"NetA": "network-11", "NetB": "network-22"},
            "additional_parameters": [
                {
                    "type": "PropertyParams",
                    "properties": [
                        {"id": "guest.password", "value": "PLACEHOLDER-pw-value"},
                        {"id": "guest.rootpw", "value": "PLACEHOLDER-root-value"},
                        {"id": "guest.hostname", "value": "PLACEHOLDER-host-value"},
                    ],
                }
            ],
        },
        "target": {"resource_pool_id": "resgroup-9"},
    }


def test_ovf_property_params_values_are_redacted_ids_and_others_survive() -> None:
    body = _ovf_deploy_body()
    out = redact_body(body, paths=(), content_type="application/json")

    spec = out.value["deployment_spec"]
    props = spec["additional_parameters"][0]["properties"]
    # Every property value redacted, regardless of the id.
    assert [p["value"] for p in props] == [OVF_PROPERTY_VALUE_MARKER] * 3
    # The ids (config keys, not secrets) survive for debugging.
    assert [p["id"] for p in props] == ["guest.password", "guest.rootpw", "guest.hostname"]
    # Non-secret deploy fields survive verbatim.
    assert spec["name"] == "keep-vm-name"
    assert spec["network_mappings"] == {"NetA": "network-11", "NetB": "network-22"}
    assert spec["accept_all_eula"] is True
    assert out.value["target"] == {"resource_pool_id": "resgroup-9"}
    # No fake property value leaks anywhere in the recorded body.
    _assert_no_secret(out.value, "PLACEHOLDER-pw-value")
    _assert_no_secret(out.value, "PLACEHOLDER-root-value")
    _assert_no_secret(out.value, "PLACEHOLDER-host-value")
    assert out.uncertain is False


def test_ovf_property_params_redacted_in_response_body_too() -> None:
    """The structural rule runs on response bodies as well as requests."""
    out = redact_body(_ovf_deploy_body(), paths=(), content_type="application/json")
    props = out.value["deployment_spec"]["additional_parameters"][0]["properties"]
    assert all(p["value"] == OVF_PROPERTY_VALUE_MARKER for p in props)


def test_ovf_property_params_redacted_via_redact_span_both_directions() -> None:
    """End-to-end through redact_span: request and response both scrubbed."""
    body = _ovf_deploy_body()
    red = redact_span(
        op_id="POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy",
        connector_id="vmware-rest-9.0",
        method="POST",
        request_body=body,
        response_body=body,
        request_content_type="application/json",
        response_content_type="application/json",
    )
    for recorded in (red.request_body, red.response_body):
        props = recorded["deployment_spec"]["additional_parameters"][0]["properties"]
        assert all(p["value"] == OVF_PROPERTY_VALUE_MARKER for p in props)
    assert red.body_recorded is True
    assert red.uncertain is False
    _assert_no_secret(red.request_body, "PLACEHOLDER-pw-value")


def test_body_without_property_params_is_unchanged() -> None:
    """Regression: a body carrying no PropertyParams block round-trips intact."""
    body = {
        "deployment_spec": {
            "name": "vm-x",
            "network_mappings": {"NetA": "network-1"},
            # A benign additional_parameters member of a different subtype.
            "additional_parameters": [{"type": "DeploymentOptionParams", "selected_key": "small"}],
        },
        "target": {"resource_pool_id": "resgroup-1"},
        "properties": [{"id": "not-a-property-params-block", "value": "keep-me"}],
    }
    out = redact_body(body, paths=(), content_type="application/json")
    assert out.value == body
    assert out.uncertain is False


def test_top_level_property_params_block_is_redacted_both_directions() -> None:
    """The rule is keyed on the type marker, not a path: a PropertyParams
    block at the *top level* (not nested under ``deployment_spec``) is
    redacted just the same, on request and response bodies."""
    body = {
        "type": "PropertyParams",
        "properties": [
            {"id": "guest.password", "value": "PLACEHOLDER-top-pw"},
            {"id": "guest.hostname", "value": "PLACEHOLDER-top-host"},
        ],
    }
    for content in (body, {"additional_parameters": [dict(body)]}):
        red = redact_span(
            op_id="POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy",
            connector_id="vmware-rest-9.0",
            method="POST",
            request_body=content,
            response_body=content,
            request_content_type="application/json",
            response_content_type="application/json",
        )
        for recorded in (red.request_body, red.response_body):
            props = _find_property_list(recorded)
            assert props is not None
            assert all(p["value"] == OVF_PROPERTY_VALUE_MARKER for p in props)
            assert [p["id"] for p in props] == ["guest.password", "guest.hostname"]
        assert red.uncertain is False
        _assert_no_secret(red.request_body, "PLACEHOLDER-top-pw")
        _assert_no_secret(red.response_body, "PLACEHOLDER-top-host")


def _find_property_list(node: Any) -> Any:
    """Return the ``properties`` list of the first PropertyParams block found."""
    if isinstance(node, dict):
        if node.get("type") == "PropertyParams" and isinstance(node.get("properties"), list):
            return node["properties"]
        for value in node.values():
            found = _find_property_list(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_property_list(item)
            if found is not None:
                return found
    return None


@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"type": "PropertyParams"}, id="properties-absent"),
        pytest.param(
            {"type": "PropertyParams", "properties": "not-a-list"}, id="properties-string"
        ),
        pytest.param(
            {"type": "PropertyParams", "properties": {"id": "x", "value": "PLACEHOLDER-map"}},
            id="properties-mapping",
        ),
        pytest.param(
            {"type": "PropertyParams", "properties": [{"id": "guest.hostname"}]},
            id="entry-without-value",
        ),
    ],
)
def test_malformed_property_params_never_crashes(block: dict[str, Any]) -> None:
    """A PropertyParams block that does not match the ``[{id, value}]`` shape
    must not crash and must not raise the fail-closed uncertainty flag: there
    is no property ``value`` string to redact, so the block round-trips."""
    out = redact_body(dict(block), paths=(), content_type="application/json")
    assert out.uncertain is False
    assert out.value == block


def test_property_params_non_string_value_is_redacted() -> None:
    """A non-string property ``value`` (int / nested object) is still replaced
    by the marker -- the rule redacts the value regardless of its type."""
    body = {
        "type": "PropertyParams",
        "properties": [
            {"id": "guest.enabled", "value": 1},
            {"id": "guest.config", "value": {"nested": "PLACEHOLDER-nested"}},
            {"id": "guest.password", "value": "PLACEHOLDER-str-pw"},
        ],
    }
    out = redact_body(body, paths=(), content_type="application/json")
    assert [p["value"] for p in out.value["properties"]] == [OVF_PROPERTY_VALUE_MARKER] * 3
    assert out.uncertain is False
    _assert_no_secret(out.value, "PLACEHOLDER-nested")
    _assert_no_secret(out.value, "PLACEHOLDER-str-pw")


def test_property_params_redaction_is_idempotent() -> None:
    """Redacting an already-redacted body yields the same result (redact twice
    == redact once), so a re-processed capture never double-mangles."""
    body = _ovf_deploy_body()
    once = redact_body(body, paths=(), content_type="application/json").value
    twice = redact_body(once, paths=(), content_type="application/json").value
    assert twice == once


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


def test_deeply_nested_preparsed_body_fails_closed_without_raising() -> None:
    """A pathologically deep pre-parsed body must not raise into the caller."""
    node: Any = {"leaf": _BEARER}
    for _ in range(6000):
        node = {"n": node}
    # Must not raise (F7 best-effort contract); fails closed to uncertain.
    out = redact_body(node, content_type="application/json")
    assert out.uncertain is True
    assert out.value == BODY_OMITTED_MARKER
    span = redact_span(
        op_id="vmware.vm.list", response_body=node, response_content_type="application/json"
    )
    assert span.uncertain is True
    _assert_no_secret(span, _SENTINEL)


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


# Real credential ops whose bodies ARE secrets -- these carry no op-id
# substring the pattern net would catch and/or hyphenated tags the bare-word
# set would miss, so they must be caught by the authoritative classify_op
# delegation. This is the regression guard for the fail-open the adversarial
# review found (vault.kv.read etc. leaking agent-visible).
_REAL_CREDENTIAL_OPS = [
    "vault.kv.read",
    "vault.kv.list",
    "vault.kv.put",
    "vault.kv.patch",
    "harbor.robot.create",
    "vault.token.create",
    "vault.auth.approle.generate_secret_id",
    "vault.auth.userpass.write",
    "k8s.secret.create",
    "k8s.job.create",
    "keycloak.user.create",
    "keycloak.user.reset_password",
    "sddc.credential.list",
    "rke2.token.rotate",
    # #3717 — the vSphere guest-ops composites that log into the guest. The
    # guest OS password rides the downstream vim ``NamePasswordAuthentication``
    # request body (not a param), so the ``_CREDENTIAL_WRITE_OPS`` pin is what
    # makes ``classify_body_exclusion`` suppress the span body. All five must
    # be excluded; ``net.show`` (no in-guest login) is intentionally absent.
    "vmware.composite.vm.guest.file.read",
    "vmware.composite.vm.guest.process.list",
    "vmware.composite.vm.guest.env.read",
    "vmware.composite.vm.guest.file.write",
    "vmware.composite.vm.guest.program.run",
]


@pytest.mark.parametrize("op_id", _REAL_CREDENTIAL_OPS)
def test_real_credential_ops_never_record_body_via_classify_op(op_id: str) -> None:
    """The single most important regression guard: real secret ops excluded."""
    result = classify_body_exclusion(op_id)
    assert result.excluded is True, f"{op_id} MUST be excluded (secret body)"
    assert result.family == "secret-bearing"
    assert result.uncertain is False


def test_vault_kv_read_secret_body_never_reaches_agent_view() -> None:
    """End-to-end: a vault.kv.read secret response is not agent-visible."""
    span = redact_span(
        op_id="vault.kv.read",
        connector_id="vault-1.0",
        method="GET",
        tags=["read-only", "secret-read"],
        response_body={"data": {"data": {"password": "PLACEHOLDER-vault-secret"}}},
        response_content_type="application/json",
    )
    assert span.body_recorded is False
    assert span.response_body == SECRET_FAMILY_OMITTED_MARKER
    assert span.uncertain is False
    _assert_no_secret(span, _SENTINEL)


@pytest.mark.parametrize(
    "tag",
    [
        "secret-read",
        "credential-read",
        "credential-mint",
        "credential-write",
        "secret-write",
        "auth-token",
    ],
)
def test_hyphenated_secret_tags_are_matched(tag: str) -> None:
    result = classify_body_exclusion("acme.generic.op", tags=[tag])
    assert result.excluded is True
    assert result.family == "secret-bearing"


def test_read_only_tag_is_not_secret_bearing() -> None:
    result = classify_body_exclusion("acme.thing.status", tags=["read-only"])
    assert result.excluded is False


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


def test_span_guest_file_read_request_body_never_records_guest_password() -> None:
    """#3717 — a guest.file.read span never records the vim guest password.

    The observed leak: ``InitiateFileTransferFromGuest`` carries the guest OS
    password in the request-body ``NamePasswordAuthentication`` block, which is
    not a declared property nor a secret-*shaped* value, so only the
    ``credential_write`` pin suppresses it. This asserts both bodies are
    omitted — the response transfer-URL is dropped by whole-body omission
    (a strict superset of the prior api_key shape-net redaction), so nothing
    regresses on the response side.
    """
    span = redact_span(
        op_id="vmware.composite.vm.guest.file.read",
        connector_id="vmware-rest-9.0",
        method="POST",
        tags=["composite", "read-only", "guest", "vi-json", "file"],
        request_body={
            "vm": {
                "_typeName": "ManagedObjectReference",
                "type": "VirtualMachine",
                "value": "vm-1",
            },
            "auth": {
                "_typeName": "NamePasswordAuthentication",
                "interactiveSession": False,
                "username": "svc",
                "password": "PLACEHOLDER-guest-pw",
            },
            "guestFilePath": "/etc/app/config",
        },
        response_body={"url": "https://*/guestFile?id=1&token=PLACEHOLDER-api-key"},
        request_content_type="application/json",
        response_content_type="application/json",
    )
    assert span.body_recorded is False
    assert span.request_body == SECRET_FAMILY_OMITTED_MARKER
    assert span.response_body == SECRET_FAMILY_OMITTED_MARKER
    assert span.uncertain is False
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
