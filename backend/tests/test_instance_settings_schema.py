# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.instance_settings.schema` (no DB, no app)."""

from __future__ import annotations

import base64

import pytest

from meho_backplane.instance_settings.schema import (
    find_secret_shaped_values,
    parse_settings_file,
)

_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_PEM_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow -- made-up test value
    "MIIBOgIBAAJBAKj34GkxFhD9\n"
    "-----END RSA PRIVATE KEY-----\n"
)


@pytest.mark.parametrize(
    "value",
    [
        _JWT,
        f"Bearer {_JWT}",
        _PEM_KEY,
        base64.b64encode(_PEM_KEY.encode()).decode(),
        base64.b64encode(_JWT.encode()).decode(),
        base64.urlsafe_b64encode(bytes(range(32))).decode(),  # a Fernet-style key
        base64.b64encode(b"\x30\x82\x01\x3a\x02\x01\x00" + bytes(range(80))).decode(),  # DER key
        "*",
        "<keep>",
        "<REDACTED>",
    ],
)
def test_secret_shaped_values_are_found(value: str) -> None:
    sentences = find_secret_shaped_values({"spec": {"notes": value}})
    assert len(sentences) == 1
    assert "`spec.notes`" in sentences[0]
    assert value not in sentences[0]


@pytest.mark.parametrize(
    "value",
    [
        "vc-01.corp.example.com",
        "tenants/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/vc-01",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "productionvcenterclusterhost",
        "change-window-for-production",
        "Writes only between 20:00 and 06:00.",
        "Rack 12 Row B Datacenter Frankfurt Building2 East",
        "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----\n",
        "@example.com,ops@corp.example",
    ],
)
def test_ordinary_values_pass(value: str) -> None:
    assert find_secret_shaped_values({"notes": value}) == []


def test_repeated_key_is_a_mistake() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nkind: Settings\nspec: {tenants: []}\n"
    )
    assert parsed.model is None or parsed.mistakes
    assert any("`kind` appears twice" in m for m in parsed.mistakes)


def test_unknown_api_version_is_one_sentence() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v2\nkind: Settings\nspec: {tenants: []}\n"
    )
    assert parsed.mistakes == [
        "`apiVersion` is not a file format this MEHO reads; use `meho.evoila.com/v1`."
    ]


def test_invalid_uuid_does_not_echo_the_value() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n"
        b"  tenants:\n    - id: not-a-uuid-xyz\n"
    )
    assert parsed.mistakes == ["`spec.tenants[0].id` must be a UUID."]


def test_repeated_target_name_is_a_mistake() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        b"    - targets:\n"
        b"        - {name: a, product: ssh, host: 10.0.0.1}\n"
        b"        - {name: a, product: ssh, host: 10.0.0.2}\n"
    )
    assert any("targets[1].name` repeats" in m for m in parsed.mistakes)


def test_two_tenants_need_ids() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        b"    - policies: {announce_gate_enabled: true}\n"
        b"    - id: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n"
    )
    assert "`spec.tenants[0].id` is required when the file names more than one tenant." in (
        parsed.mistakes
    )


def test_strict_booleans() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        b"    - policies: {announce_gate_enabled: 'maybe'}\n"
    )
    assert parsed.mistakes == [
        "`spec.tenants[0].policies.announce_gate_enabled` must be true or false."
    ]


@pytest.mark.parametrize(
    ("key", "credential"),
    [
        ("password", True),
        ("adminPassword", True),
        ("api_key", True),
        ("apiKey", True),
        ("client-secret", True),
        ("bearer_token", True),
        ("private_key", True),
        ("token_url", False),
        ("secret_path", False),
        ("basic_username", False),
        ("signature_header", False),
        ("vault_role", False),
    ],
)
def test_credential_key_names_under_extras(key: str, credential: bool) -> None:
    found = find_secret_shaped_values({"extras": {key: "plain"}})
    assert bool(found) is credential


def test_credential_key_outside_extras_is_left_to_the_schema() -> None:
    # Outside ``extras`` the model refuses unknown keys anyway; ``secret_ref`` is legal.
    assert find_secret_shaped_values({"secret_ref": "tenants/a/b"}) == []


def test_secret_stops_all_later_checks() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        b"    - targets:\n        - {name: a, product: ssh, extras: {password: x}}\n"
    )
    # No "host is required": once a secret is found nothing else runs.
    assert parsed.model is None
    assert len(parsed.mistakes) == 1
    assert "extras.password" in parsed.mistakes[0]


def test_repeated_secret_shaped_key_is_redacted() -> None:
    parsed = parse_settings_file(
        f"apiVersion: meho.evoila.com/v1\nkind: Settings\n{_JWT}: 1\n{_JWT}: 2\n".encode()
    )
    assert parsed.model is None
    assert any("`<redacted>` appears twice" in m for m in parsed.mistakes)
    assert not any(_JWT.split(".")[1] in m for m in parsed.mistakes)


@pytest.mark.parametrize(
    "content",
    [
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nmetadata: {exportedAt: 2026-99-99}\n",
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nmetadata: {exportedAt: 2026-02-30}\n",
        b"apiVersion: meho.evoila.com/v1\nkind: \x07Settings\n",
    ],
)
def test_unreadable_values_are_mistakes(content: bytes) -> None:
    parsed = parse_settings_file(content)
    assert parsed.model is None or parsed.mistakes
    assert parsed.mistakes


def test_binary_value_is_refused_without_decoding() -> None:
    """``!!binary`` would hide a JWT from the secret check until after it ran."""
    encoded = base64.b64encode(_JWT.encode()).decode()
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        b"    - targets:\n        - name: a\n          product: ssh\n          host: h\n"
        b"          extras:\n            blob: !!binary " + encoded.encode() + b"\n"
    )
    assert parsed.model is None
    assert parsed.mistakes == [
        "Line 10: binary values (`!!binary`) are not accepted; the settings file holds text only."
    ]


@pytest.mark.parametrize(
    "content",
    [
        f"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec: !{_JWT} x\n",
        f"apiVersion: meho.evoila.com/v1\nkind: !<tag:{_JWT}> Settings\n",
        f"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec: [{_JWT}\n",
    ],
)
def test_yaml_errors_never_copy_the_input(content: str) -> None:
    parsed = parse_settings_file(content.encode())
    assert parsed.model is None
    (mistake,) = parsed.mistakes
    assert mistake.startswith("The file is not valid YAML")
    assert _JWT.split(".")[1] not in mistake
    assert "eyJ" not in mistake


def test_secret_field_map_is_a_target_key() -> None:
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        b"    - targets:\n        - name: a\n          product: ssh\n          host: h\n"
        b"          secret_field_map: {password: pw, username: user}\n"
    )
    assert parsed.mistakes == []
    assert parsed.model is not None
    (target,) = parsed.model.spec.tenants[0].targets or []
    assert target.secret_field_map == {"password": "pw", "username": "user"}
