# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the RabbitMQ credential-redaction helper (#2233).

The load-bearing safety nuance: shovel / federation / parameter /
definitions payloads echo back stored ``amqp://user:pass@`` URIs and user
``password_hash`` values, so the connector must blank them before the
result is surfaced. These tests pin the two redaction rules — AMQP URI
userinfo blanking and sensitive-key value blanking — against fixtures
shaped like the real Management API payloads.
"""

from __future__ import annotations

from meho_backplane.connectors.rabbitmq.redact import REDACTED, redact_rabbitmq_payload


def test_amqp_userinfo_is_blanked_but_host_preserved() -> None:
    """AC: an ``amqp://u:p@h`` URI has its userinfo blanked, host kept."""
    out = redact_rabbitmq_payload({"src-uri": "amqp://u:p@h:5672/%2Fvhost"})
    assert out["src-uri"] == f"amqp://{REDACTED}@h:5672/%2Fvhost"
    assert "u:p@" not in out["src-uri"]


def test_amqps_userinfo_is_blanked() -> None:
    """The ``amqps`` (TLS) scheme is redacted the same way."""
    out = redact_rabbitmq_payload("amqps://admin:s3cr3t@dr.example.com")
    assert out == f"amqps://{REDACTED}@dr.example.com"
    assert "s3cr3t" not in out


def test_credential_free_amqp_uri_is_untouched() -> None:
    """A URI with no userinfo is returned unchanged (no spurious ``***``)."""
    out = redact_rabbitmq_payload("amqp://broker.internal:5672/%2F")
    assert out == "amqp://broker.internal:5672/%2F"


def test_password_and_secret_keys_are_blanked_regardless_of_type() -> None:
    """Keys containing ``password``/``secret`` have their value blanked."""
    out = redact_rabbitmq_payload(
        {
            "password": "hunter2",
            "password_hash": "abc123hash",
            "client_secret": "shhh",
            "Secret": 42,
            "name": "keep-me",
        }
    )
    assert out["password"] == REDACTED
    assert out["password_hash"] == REDACTED
    assert out["client_secret"] == REDACTED
    assert out["Secret"] == REDACTED
    # A non-sensitive key is untouched.
    assert out["name"] == "keep-me"


def test_shovel_parameter_fixture_is_fully_redacted() -> None:
    """AC: a shovel fixture with ``amqp://u:p@h`` + a ``password`` key is redacted."""
    fixture = [
        {
            "vhost": "/",
            "component": "shovel",
            "name": "to-dr",
            "value": {
                "src-uri": "amqp://svc:p@ssw0rd@primary:5672",  # trufflehog:ignore
                "dest-uri": [
                    "amqps://svc:p@ssw0rd@dr:5671",  # trufflehog:ignore
                    "amqp://backup:pw@dr2",  # trufflehog:ignore
                ],
                "src-queue": "events",
                "password": "should-not-appear",
            },
        }
    ]
    out = redact_rabbitmq_payload(fixture)
    blob = repr(out)
    assert "p@ssw0rd" not in blob
    assert "should-not-appear" not in blob
    assert "pw@dr2" not in blob
    # Structure and non-secret fields survive.
    assert out[0]["name"] == "to-dr"
    assert out[0]["value"]["src-queue"] == "events"
    assert out[0]["value"]["password"] == REDACTED
    assert out[0]["value"]["dest-uri"][1] == f"amqp://{REDACTED}@dr2"


def test_definitions_fixture_redacts_password_hash_and_uris() -> None:
    """AC: a definitions fixture redacts user password_hash + shovel URIs."""
    definitions = {
        "rabbitmq_version": "3.13.7",
        "users": [
            {"name": "guest", "password_hash": "R0tSecretHash==", "tags": ["administrator"]},
        ],
        "parameters": [
            {
                "component": "shovel",
                "name": "s1",
                "value": {"src-uri": "amqp://u:leaky@h", "dest-uri": "amqp://h2"},
            }
        ],
    }
    out = redact_rabbitmq_payload(definitions)
    blob = repr(out)
    assert "R0tSecretHash==" not in blob
    assert "leaky" not in blob
    assert out["rabbitmq_version"] == "3.13.7"
    assert out["users"][0]["password_hash"] == REDACTED
    assert out["users"][0]["name"] == "guest"
    assert out["parameters"][0]["value"]["src-uri"] == f"amqp://{REDACTED}@h"


def test_redaction_does_not_mutate_input() -> None:
    """The walk returns a new structure; the caller's payload is untouched."""
    original = {"value": {"src-uri": "amqp://u:p@h"}, "password": "x"}
    snapshot = {"value": {"src-uri": "amqp://u:p@h"}, "password": "x"}
    _ = redact_rabbitmq_payload(original)
    assert original == snapshot


def test_non_string_scalars_pass_through() -> None:
    """Ints/bools/None survive the walk unchanged."""
    out = redact_rabbitmq_payload({"running": True, "messages": 12, "node": None})
    assert out == {"running": True, "messages": 12, "node": None}
