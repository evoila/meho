# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The documented ``meho-tenant-admin`` policy allows the KV deletes MEHO sends (#3945).

``docs/cross-repo/connector-vault-tenant-policy.md`` section 2.3 holds the
policy body for the ``tenant_admin`` role. This test takes that body from
the doc, fills in a tenant id the way Vault fills in
``{{identity.entity.metadata.tenant_id}}``, and works out which rights the
policy gives on the exact path each delete op is checked against
(:func:`vault_kv_write_target_path`). The rights must pass the same check
MEHO runs before it asks for approval (:func:`_capabilities_grant_write`).

The path matching is a small model of Vault's rule for the patterns this
policy uses: a pattern ending in ``*`` matches by prefix, any other pattern
must match exactly, and when several patterns match, only the most specific
one counts (here: the longest). The test refuses ``+`` patterns, so the
model stays correct for this body.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from meho_backplane.connectors.vault.ops import (
    VAULT_KV_WRITE_CAPABILITIES,
    _capabilities_grant_write,
    vault_kv_write_target_path,
)

_DOC = (
    Path(__file__).resolve().parents[2] / "docs" / "cross-repo" / "connector-vault-tenant-policy.md"
)
_TEMPLATE = "{{identity.entity.metadata.tenant_id}}"
_PATH_RULE = re.compile(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]\s*\}')

_TENANT = str(uuid.UUID("3f8c0000-0000-4000-8000-000000000001"))
_OTHER_TENANT = str(uuid.UUID("3f8c0000-0000-4000-8000-000000000002"))


def _admin_policy_body() -> str:
    text = _DOC.read_text(encoding="utf-8")
    section = text.split("### 2.3 `meho-tenant-admin`", 1)[1].split("\n## ", 1)[0]
    match = re.search(r"```hcl\n(.*?)```", section, re.DOTALL)
    assert match, "section 2.3 has no hcl block"
    return match.group(1)


def _rules(tenant_id: str) -> dict[str, list[str]]:
    body = _admin_policy_body().replace(_TEMPLATE, tenant_id)
    rules = {path: re.findall(r'"([^"]+)"', caps) for path, caps in _PATH_RULE.findall(body)}
    assert rules, "no path rules found in the policy body"
    assert not any("+" in path for path in rules), "the matcher does not model '+' patterns"
    return rules


def _granted(rules: dict[str, list[str]], request_path: str) -> list[str]:
    matches = [
        pattern
        for pattern in rules
        if (pattern.endswith("*") and request_path.startswith(pattern[:-1]))
        or pattern == request_path
    ]
    if not matches:
        return []
    return rules[max(matches, key=len)]


@pytest.mark.parametrize("op_id", ["vault.kv.delete", "vault.kv.metadata.delete"])
@pytest.mark.parametrize("key", ["app/db", "privileged/break-glass"])
def test_tenant_admin_policy_allows_the_delete_in_its_own_tenant(op_id: str, key: str) -> None:
    path = vault_kv_write_target_path(
        op_id, {"mount": "secret", "path": f"tenants/{_TENANT}/{key}"}
    )

    granted = _granted(_rules(_TENANT), path)

    assert _capabilities_grant_write(granted, VAULT_KV_WRITE_CAPABILITIES[op_id]), (path, granted)


@pytest.mark.parametrize("op_id", ["vault.kv.delete", "vault.kv.metadata.delete"])
def test_tenant_admin_policy_does_not_reach_another_tenant(op_id: str) -> None:
    path = vault_kv_write_target_path(
        op_id, {"mount": "secret", "path": f"tenants/{_OTHER_TENANT}/app/db"}
    )

    assert _granted(_rules(_TENANT), path) == []
