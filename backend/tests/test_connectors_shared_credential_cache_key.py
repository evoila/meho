# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``credential_cache_key``: the cache key for login tokens and sessions.

Two targets in one tenant that use the same secret against the same login
host get the same key, so they share one token. A different secret, login
host, tenant or login input gives a different key. A target with no secret
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


def test_a_different_credential_gives_a_different_key() -> None:
    base = _target()
    for other in (
        _target(secret_ref="targets/shared/other-login"),
        _target(host="other-appliance.test.invalid"),
        _target(port=8443),
        _target(fqdn="org-a.appliance.test.invalid"),
    ):
        assert credential_cache_key(base) != credential_cache_key(other), other


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
