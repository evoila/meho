# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``credential_cache_key``: the cache key for login tokens and sessions.

Two targets in one tenant that use the same secret against the same login
address get the same key, so they share one token. A different secret, login
address (scheme, host, port), tenant or login input gives a different key.
An unset port is the scheme's default port. A target with no secret
reference keeps a key of its own.

Fixture values are made up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from meho_backplane.connectors._shared.cache_key import (
    credential_cache_key,
    target_cache_key,
)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000a001")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b001")


@dataclass(frozen=True)
class _Target:
    id: uuid.UUID
    tenant_id: uuid.UUID
    host: str = "appliance.test.invalid"
    port: int | None = 443
    fqdn: str | None = None
    secret_ref: str | None = "targets/shared/svc-login"
    extras: Any = None


def _target(**changes: Any) -> _Target:
    return replace(_Target(id=uuid.uuid4(), tenant_id=_TENANT_A), **changes)


def test_two_targets_with_one_credential_share_one_key() -> None:
    first = _target()
    second = _target()
    assert first.id != second.id
    assert credential_cache_key(first) == credential_cache_key(second)
    assert target_cache_key(first) != target_cache_key(second)


def test_host_case_and_spaces_do_not_split_the_key() -> None:
    first = _target(host="Appliance.Test.Invalid")
    second = _target(host=" appliance.test.invalid ")
    assert credential_cache_key(first) == credential_cache_key(second)


def test_a_service_path_on_the_host_keeps_its_case() -> None:
    """The adapter sends the path as is, so paths that differ in case are different logins."""
    upper = _target(host="Appliance.Test.Invalid/Org-A")
    lower_name = _target(host="appliance.test.invalid/Org-A")
    lower_path = _target(host="appliance.test.invalid/org-a")
    assert credential_cache_key(upper) == credential_cache_key(lower_name)
    assert credential_cache_key(upper) != credential_cache_key(lower_path)
    assert credential_cache_key(upper)[3] == "appliance.test.invalid/Org-A"


def test_a_different_credential_gives_a_different_key() -> None:
    base = _target()
    for other in (
        _target(secret_ref="targets/shared/other-login"),
        _target(host="other-appliance.test.invalid"),
        _target(port=8443),
        _target(fqdn="org-a.appliance.test.invalid"),
        _target(extras={"scheme": "http"}),
    ):
        assert credential_cache_key(base) != credential_cache_key(other), other


def test_http_and_https_targets_never_share_a_key() -> None:
    """An HTTPS login token must not be sent over HTTP to another service."""
    https = _target(port=None)
    plain = _target(port=None, extras={"scheme": "http"})
    explicit_https = _target(port=None, extras={"scheme": "https"})
    assert credential_cache_key(https) != credential_cache_key(plain)
    assert credential_cache_key(https) == credential_cache_key(explicit_https)
    assert credential_cache_key(https)[2] == "https"
    assert credential_cache_key(plain)[2] == "http"


def test_an_unset_port_is_the_default_port_of_the_scheme() -> None:
    """Both targets reach the same login address, so they share one key."""
    assert credential_cache_key(_target(port=None)) == credential_cache_key(_target(port=443))
    assert credential_cache_key(_target(port=None))[4] == "443"
    http_unset = _target(port=None, extras={"scheme": "http"})
    http_80 = _target(port=80, extras={"scheme": "http"})
    assert credential_cache_key(http_unset) == credential_cache_key(http_80)
    assert credential_cache_key(http_unset) != credential_cache_key(_target(port=80))


def test_a_secret_reference_override_replaces_the_target_secret() -> None:
    """A login that uses another secret than the target's own keys on that secret."""
    first = _target(secret_ref="targets/shared/tenant-a")
    second = _target(secret_ref="targets/shared/tenant-b")
    assert credential_cache_key(first) != credential_cache_key(second)
    shared = "targets/shared/provider"
    assert credential_cache_key(first, secret_ref=shared) == credential_cache_key(
        second, secret_ref=shared
    )
    assert credential_cache_key(first, secret_ref=shared)[1] == shared
    assert credential_cache_key(first, secret_ref=None) == credential_cache_key(first)


def test_tenants_stay_apart() -> None:
    in_a = _target()
    in_b = _target(tenant_id=_TENANT_B)
    assert credential_cache_key(in_a) != credential_cache_key(in_b)
    assert credential_cache_key(in_a)[0] == str(_TENANT_A)
    assert credential_cache_key(in_b)[0] == str(_TENANT_B)


def test_login_inputs_are_part_of_the_key() -> None:
    first = _target()
    second = _target()
    assert credential_cache_key(first, "domain-a") == credential_cache_key(second, "domain-a")
    assert credential_cache_key(first, "domain-a") != credential_cache_key(second, "domain-b")
    assert credential_cache_key(first, None) != credential_cache_key(second, "domain-a")


def test_a_target_with_no_secret_reference_keeps_its_own_key() -> None:
    first = _target(secret_ref=None)
    second = _target(secret_ref="")
    assert credential_cache_key(first) == target_cache_key(first)
    assert credential_cache_key(second) == target_cache_key(second)
    assert credential_cache_key(first) != credential_cache_key(_target(secret_ref=None))
