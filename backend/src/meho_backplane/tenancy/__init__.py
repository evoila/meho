# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Minimal v0.2 tenant lifecycle: just-in-time seeding.

v0.2 has no tenant-provisioning surface (CRUD + RBAC is v0.3+). The
``tenant`` table is the FK keystone every per-tenant write joins on,
yet migration ``0002`` deliberately ships it empty and defers seeding.
This package closes that gap with the lowest-friction mechanism: an
idempotent get-or-create issued from the authenticated request path,
keyed to the verified ``tenant_id`` JWT claim. See
:func:`meho_backplane.tenancy.ensure.ensure_tenant`.
"""

from meho_backplane.tenancy.ensure import ensure_tenant

__all__ = ["ensure_tenant"]
