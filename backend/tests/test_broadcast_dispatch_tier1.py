# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the broadcast param scrub (meho-internal #151).

:func:`~meho_backplane.broadcast.events.scrub_broadcast_params` replaces
secret-named keys and runs the Tier-1 deterministic redactor over string
values. It reports whether it found secret material. Scalar config values
under secret-like names (``bind_secret_id``, ``secret_id_ttl``) pass through.
"""

from __future__ import annotations

import pytest

from meho_backplane.broadcast.events import (
    _is_secret_param_name,
    scrub_broadcast_params,
)

# Secret fixtures assembled from fragments so gitleaks' built-in rules
# do not false-positive on the test source.
_BEARER_SECRET = "abcdef12" + "345678ab" + "cd"


# ---------------------------------------------------------------------------
# scrub_broadcast_params unit contract
# ---------------------------------------------------------------------------


def test_scrub_replaces_secret_named_keys_recursively() -> None:
    scrubbed, found = scrub_broadcast_params(
        {"spec": {"env": [{"name": "X", "sessionToken": _BEARER_SECRET}]}},
    )
    assert found is True
    assert scrubbed == {
        "spec": {"env": [{"name": "X", "sessionToken": "[REDACTED:param_name]"}]},
    }


def test_scrub_benign_params_pass_through_unchanged() -> None:
    params = {"cluster": "prod", "replicas": 3, "labels": ["a", "b"]}
    scrubbed, found = scrub_broadcast_params(params)
    assert found is False
    assert scrubbed == params


def test_scrub_scalar_values_under_secretish_names_pass_through() -> None:
    params = {"bind_secret_id": True, "secret_id_ttl": 3600, "secret_id_num_uses": 0}
    scrubbed, found = scrub_broadcast_params(params)
    assert found is False
    assert scrubbed == params


def test_scrub_tier1_pass_redacts_labelled_string_shapes() -> None:
    scrubbed, found = scrub_broadcast_params(
        {"note": "Authorization: Bearer " + _BEARER_SECRET},
    )
    assert found is True
    assert _BEARER_SECRET not in str(scrubbed)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("password", True),
        ("user_password", True),
        ("passphrase", True),
        ("clientSecret", True),
        ("client-secret", True),
        ("sessionToken", True),
        ("api_key", True),
        ("api-key", True),
        ("apikey", True),
        ("private_key", True),
        ("credentials", True),
        ("kubeconfig", True),
        ("secret_id", True),
        ("continue_token", True),
        # attribute / reference names — not the secret itself
        ("token_policies", False),
        ("token_ttl", False),
        ("secret_name", False),
        ("secret_path", False),
        ("role_name", False),
        ("ssh_public_key", False),
        ("label_key", False),
        ("keyspace", False),
        ("", False),
    ],
)
def test_is_secret_param_name(name: str, expected: bool) -> None:
    assert _is_secret_param_name(name) is expected
