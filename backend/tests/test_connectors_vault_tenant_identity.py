# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the tenant-scoped Vault identity wiring (#1724).

Covers the helper that maps an :class:`Operator` onto the entity metadata and
role→policy binding the templated per-tenant ACL policies require
(``docs/cross-repo/connector-vault-tenant-policy.md``):

* ``tenant_entity_metadata`` sets ``tenant_id`` into entity metadata from
  ``operator.tenant_id`` (criterion: the entity-write path carries
  ``tenant_id`` so ``{{identity.entity.metadata.tenant_id}}`` resolves);
* ``TENANT_POLICY_FOR_ROLE`` is exhaustive over :class:`TenantRole` and binds
  exactly one templated policy per role;
* ``tenant_group_policies`` returns that policy as the group's ``policies``
  value, and ``tenant_group_name`` builds a flat (slash-free) handle.

Pure-helper tests — no Vault I/O, no dispatch — because the helper is a
side-effect-free mapping layer feeding the existing identity ops.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vault.tenant_identity import (
    TENANT_ID_METADATA_KEY,
    TENANT_POLICY_FOR_ROLE,
    tenant_entity_metadata,
    tenant_group_name,
    tenant_group_policies,
)


def _operator(tenant_id: UUID, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub="op-1724",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=role,
    )


def test_entity_metadata_carries_tenant_id_from_operator() -> None:
    """The entity-write metadata carries operator.tenant_id as a canonical str."""
    tenant = UUID("3f8c2b1a-0000-4000-8000-000000000abc")
    metadata = tenant_entity_metadata(_operator(tenant))

    assert metadata == {"tenant_id": "3f8c2b1a-0000-4000-8000-000000000abc"}
    # The key must match the policy template's {{identity.entity.metadata.<k>}}.
    assert TENANT_ID_METADATA_KEY in metadata
    # Vault stores metadata values as strings, never a UUID object.
    assert isinstance(metadata[TENANT_ID_METADATA_KEY], str)


def test_entity_metadata_renders_uuid_canonical_lowercase() -> None:
    """An upper-cased UUID claim still renders dashed-lowercase (str(UUID))."""
    metadata = tenant_entity_metadata(_operator(UUID("AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE")))
    assert metadata["tenant_id"] == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_role_policy_table_is_exhaustive_over_tenant_role() -> None:
    """Every TenantRole binds to exactly one templated policy — no gaps."""
    assert set(TENANT_POLICY_FOR_ROLE) == set(TenantRole)
    # Distinct policy per role (roles are capabilities on the shared path).
    assert len(set(TENANT_POLICY_FOR_ROLE.values())) == len(TenantRole)
    # At most three policies, by the issue's ≤3 constraint.
    assert len(TENANT_POLICY_FOR_ROLE) <= 3


@pytest.mark.parametrize(
    ("role", "expected_policy"),
    [
        (TenantRole.READ_ONLY, "meho-tenant-read-only"),
        (TenantRole.OPERATOR, "meho-tenant-operator"),
        (TenantRole.TENANT_ADMIN, "meho-tenant-admin"),
    ],
)
def test_group_policies_binds_one_policy_per_role(role: TenantRole, expected_policy: str) -> None:
    """tenant_group_policies returns the single role policy for group.write."""
    assert tenant_group_policies(role) == [expected_policy]


def test_group_name_is_a_flat_slashless_handle() -> None:
    """Group names are flat handles; never carry a slash (Vault constraint)."""
    name = tenant_group_name("3f8c2b1a-0000-4000-8000-000000000abc", TenantRole.OPERATOR)
    assert name == "meho-tenant-3f8c2b1a-0000-4000-8000-000000000abc-operator"
    assert "/" not in name
