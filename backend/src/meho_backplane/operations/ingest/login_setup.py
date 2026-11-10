# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Put a connector's login setup into the connector table of its scope.

A login setup is the
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` class
MEHO builds from the login type an operator picks at ingest
(``meho connector ingest --auth-scheme <name>``), or from a profile shipped
with the catalog. :func:`plan_login_setup` decides where it goes and what
the ingest answer says about it, and :func:`apply_login_setup` puts it
there:

* **Built-in scope** (``tenant_id`` is ``None``): the shared v2 table, so
  it applies to every tenant. A bare stand-in on the key is replaced.
* **Tenant scope**: the tenant's own table
  (:func:`~meho_backplane.connectors.registry.set_tenant_connector`), so it
  applies only to that tenant's calls. When the shared key is free, a bare
  stand-in is put there, so the product and the implementation stay known
  to target checks; the stand-in cannot call the vendor.
* A second login setup for the same scope and key replaces the first one
  when it differs (``"replaced"``), and is ``"unchanged"`` when it is the
  same. The caller commits the audit row for ``"registered"`` and
  ``"replaced"`` before it applies the change, so no call uses a login
  setup without its audit row.
* When hand-written connector code serves the shared key, or a shipped or
  built-in login setup does, the login setup is not used, and the outcome
  says so (``"served_by_connector_code"`` /
  ``"served_by_builtin_login_setup"``). A shipped login setup is never
  replaced.

The functions here are synchronous: a replace has no ``await`` between
removing the old class and adding the new one, so no call sees the key
empty.

An ingest decides between ``"registered"``, ``"replaced"`` and
``"unchanged"`` from the saved login type of the scope, not from this
table, because another replica may have changed the saved login type
(:meth:`~meho_backplane.operations.ingest.service.ReviewService.record_ingested_login_setup`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from meho_backplane.connectors.base import Connector, shim_kind
from meho_backplane.connectors.registry import (
    deregister_connector_v2,
    deregister_tenant_connector,
    get_connector_v2,
    get_tenant_connector,
    register_connector_v2,
    set_tenant_connector,
)
from meho_backplane.operations._lookup import shipped_profile_triples
from meho_backplane.operations.ingest._internals import ConnectorScope
from meho_backplane.operations.ingest.api_schemas import LoginSetupOutcome
from meho_backplane.operations.ingest.connector_registration import (
    ensure_connector_class_registered,
)

__all__ = [
    "LOGIN_SETUP_APPLIED",
    "LoginSetupChange",
    "LoginSetupOutcome",
    "apply_login_setup",
    "current_login_setup",
    "plan_login_setup",
    "remove_login_setup",
]

#: Outcomes after which the scope's calls use the new login setup.
LOGIN_SETUP_APPLIED: Final[frozenset[str]] = frozenset({"registered", "replaced"})


@dataclass(frozen=True, slots=True)
class LoginSetupChange:
    """The outcome, and the class a replace took off the scope's key."""

    outcome: LoginSetupOutcome
    previous: type[Connector] | None = None


def _key(scope: ConnectorScope) -> tuple[str, str, str]:
    return (scope.product, scope.version, scope.impl_id)


def _served_outcome(shared: type[Connector]) -> LoginSetupOutcome | None:
    """Return the outcome when *shared* serves the key for every tenant, else ``None``."""
    kind = shim_kind(shared)
    if kind == "none":
        return "served_by_connector_code"
    if kind == "profiled":
        return "served_by_builtin_login_setup"
    return None


def _same_setup(existing: type[Connector] | None, new: type[Connector]) -> bool:
    if existing is None:
        return False
    return getattr(existing, "profile", None) == getattr(new, "profile", None)


def _replace_shared(key: tuple[str, str, str], cls: type[Connector]) -> None:
    product, version, impl_id = key
    deregister_connector_v2(product=product, version=version, impl_id=impl_id)
    register_connector_v2(product=product, version=version, impl_id=impl_id, cls=cls)


def _plan_builtin(key: tuple[str, str, str], cls: type[Connector]) -> LoginSetupChange:
    shared = get_connector_v2(*key)
    if shared is None or shim_kind(shared) == "bare":
        return LoginSetupChange("registered")
    if shim_kind(shared) == "none":
        return LoginSetupChange("served_by_connector_code")
    if _same_setup(shared, cls):
        return LoginSetupChange("unchanged")
    if key in shipped_profile_triples():
        return LoginSetupChange("served_by_builtin_login_setup")
    return LoginSetupChange("replaced", previous=shared)


def _plan_for_tenant(
    tenant_id: UUID, key: tuple[str, str, str], cls: type[Connector]
) -> LoginSetupChange:
    shared = get_connector_v2(*key)
    if shared is not None:
        served = _served_outcome(shared)
        if served is not None:
            return LoginSetupChange(served)
    existing = get_tenant_connector(tenant_id, *key)
    if _same_setup(existing, cls):
        return LoginSetupChange("unchanged")
    if existing is None:
        return LoginSetupChange("registered")
    return LoginSetupChange("replaced", previous=existing)


def plan_login_setup(scope: ConnectorScope, cls: type[Connector]) -> LoginSetupChange:
    """Return what :func:`apply_login_setup` would do for *cls*, without changing anything.

    The caller commits the audit row of a ``"registered"`` or
    ``"replaced"`` outcome first, then applies the change.
    """
    if scope.tenant_id is None:
        return _plan_builtin(_key(scope), cls)
    return _plan_for_tenant(scope.tenant_id, _key(scope), cls)


def apply_login_setup(scope: ConnectorScope, cls: type[Connector]) -> None:
    """Make *scope*'s calls use the login setup *cls*; see the module docstring.

    Does nothing when another class serves the key for every tenant, or
    when the scope already holds the same login setup.
    """
    key = _key(scope)
    product, version, impl_id = key
    change = plan_login_setup(scope, cls)
    if change.outcome not in LOGIN_SETUP_APPLIED:
        return
    if scope.tenant_id is None:
        _replace_shared(key, cls)
        return
    if get_connector_v2(*key) is None:
        ensure_connector_class_registered(
            product=product, version=version, impl_id=impl_id, base_url=None
        )
    set_tenant_connector(
        tenant_id=scope.tenant_id, product=product, version=version, impl_id=impl_id, cls=cls
    )


def current_login_setup(
    tenant_id: UUID | None, product: str, version: str, impl_id: str
) -> type[Connector] | None:
    """Return the login setup class that *tenant_id*'s scope holds for the key, or ``None``.

    For the built-in scope this is the shared class when it is a login
    setup (``shim_kind == "profiled"``). A tenant's scope holds only its
    own class.
    """
    if tenant_id is not None:
        return get_tenant_connector(tenant_id, product, version, impl_id)
    shared = get_connector_v2(product, version, impl_id)
    if shared is not None and shim_kind(shared) == "profiled":
        return shared
    return None


def remove_login_setup(scope: ConnectorScope) -> bool:
    """Remove the login setup of *scope* from memory. Returns whether one was removed.

    Runs after a connector delete removed the scope's saved login type. A
    tenant's class is removed from that tenant's table only; other
    tenants keep theirs. For the built-in scope the shared class is
    removed when it is a login setup an ingest made; a shipped login
    setup and hand-written code stay.
    """
    product, version, impl_id = _key(scope)
    if scope.tenant_id is not None:
        return deregister_tenant_connector(
            tenant_id=scope.tenant_id, product=product, version=version, impl_id=impl_id
        )
    if current_login_setup(None, product, version, impl_id) is None:
        return False
    if _key(scope) in shipped_profile_triples():
        return False
    return deregister_connector_v2(product=product, version=version, impl_id=impl_id)
