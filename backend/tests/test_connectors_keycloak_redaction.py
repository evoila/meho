# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the keycloak read-op secret scrubber (G3.13-T2 #1394).

Pins the recursive redaction contract directly — no event loop, no HTTP —
so a regression in the scrubber surfaces as a fast, isolated failure
rather than an E2E surprise.
"""

from __future__ import annotations

from meho_backplane.connectors.keycloak.redaction import REDACTED, redact_secret_fields


def test_scrubs_top_level_client_secret() -> None:
    """A ClientRepresentation's ``secret`` is replaced, other fields kept."""
    client = {"clientId": "meho-backplane", "secret": "s3cr3t", "redirectUris": ["/cb"]}
    out = redact_secret_fields(client)
    assert out["secret"] == REDACTED
    assert out["clientId"] == "meho-backplane"
    assert out["redirectUris"] == ["/cb"]


def test_scrubs_user_credentials_subtree() -> None:
    """A UserRepresentation's ``credentials`` list is replaced wholesale."""
    user = {"username": "op", "credentials": [{"type": "password", "value": "hash"}]}
    out = redact_secret_fields(user)
    assert out["credentials"] == REDACTED
    assert out["username"] == "op"


def test_scrubs_nested_secret_in_protocol_mapper() -> None:
    """A secret nested inside a list-of-dicts is caught by the recursive walk."""
    client = {
        "clientId": "c",
        "protocolMappers": [
            {"name": "m", "config": {"secret": "nested-secret", "claim.name": "x"}}
        ],
    }
    out = redact_secret_fields(client)
    mapper_config = out["protocolMappers"][0]["config"]
    assert mapper_config["secret"] == REDACTED
    assert mapper_config["claim.name"] == "x"


def test_scrubs_credential_value_and_data_fields() -> None:
    """``value`` / ``secretData`` / ``credentialData`` are all scrubbed."""
    cred = {"type": "password", "value": "v", "secretData": "s", "credentialData": "d"}
    out = redact_secret_fields(cred)
    assert out["value"] == REDACTED
    assert out["secretData"] == REDACTED
    assert out["credentialData"] == REDACTED
    assert out["type"] == "password"


def test_scrubs_smtp_server_password() -> None:
    """A ``RealmRepresentation.smtpServer`` password (Map<String,String>) is scrubbed.

    The smtp relay password rides under the ``password`` key of the smtp
    map; the read ops claim to scrub any nested secret, so ``password`` must
    be covered alongside the camelCase representation fields.
    """
    realm = {"realm": "meho", "smtpServer": {"host": "smtp.test", "password": "smtp-pass"}}
    out = redact_secret_fields(realm)
    assert out["smtpServer"]["host"] == "smtp.test"
    assert out["smtpServer"]["password"] == REDACTED


def test_scrubs_generic_credential_keys_case_insensitively() -> None:
    """The generic credential spellings are scrubbed regardless of casing.

    A representation is ``additionalProperties: true``, so a credential can
    arrive under any of the well-known spellings and under any casing.
    """
    blob = {
        "Password": "p",
        "client_secret": "cs",
        "TOKEN": "t",
        "private_key": "pk",
        "keep": "visible",
    }
    out = redact_secret_fields(blob)
    assert out["Password"] == REDACTED
    assert out["client_secret"] == REDACTED
    assert out["TOKEN"] == REDACTED
    assert out["private_key"] == REDACTED
    assert out["keep"] == "visible"


def test_input_not_mutated() -> None:
    """The scrub returns a new structure; the input is left untouched."""
    client = {"secret": "keep-me-original"}
    out = redact_secret_fields(client)
    assert client["secret"] == "keep-me-original"
    assert out["secret"] == REDACTED


def test_scalars_and_lists_pass_through() -> None:
    """Non-secret scalars and plain lists are unchanged."""
    assert redact_secret_fields("plain") == "plain"
    assert redact_secret_fields(42) == 42
    assert redact_secret_fields([1, {"a": 2}]) == [1, {"a": 2}]
