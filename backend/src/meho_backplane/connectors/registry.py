# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector registry — module-level lookup tables for connector classes.

Two registry layers coexist:

* **v1** — single-key ``dict[product, type[Connector]]`` shipped in G0.2-T2
  (#241). Used by lifespan eager-import and by the existing
  ``/api/v1/connectors/{product}/{op_id}`` dispatch route until G0.6 lands
  the v2 dispatcher. Public surface: :func:`register_connector`,
  :func:`get_connector`, :func:`all_connectors`.
* **v2** — three-tuple key ``dict[(product, version, impl_id), type[Connector]]``
  added in G0.6-T2 (#393) so multiple implementations per product can
  coexist (e.g. ``vmware-pyvmomi-7.0`` and ``vmware-rest-9.0``). Public
  surface: :func:`register_connector_v2`, :func:`list_connector_impls`,
  :func:`all_connectors_v2`.

Both layers stay in sync. The shipped v1 entry point
:func:`register_connector` writes to **both** tables (v2 entry has
``version=""`` and ``impl_id=""``) so existing Vault and Kubernetes
registrations participate in v2 resolution without code change.
:func:`resolve_connector` (in :mod:`meho_backplane.connectors.resolver`)
reads the v2 table.

Duplicate registration on either layer raises :exc:`RuntimeError` —
two modules claiming the same key is a programming bug, not a runtime
condition, and should surface as a deploy failure.
"""

import importlib
import pkgutil

import structlog

from meho_backplane.connectors.base import Connector

__all__ = [
    "_eager_import_connectors",
    "all_connectors",
    "all_connectors_v2",
    "clear_registry",
    "get_connector",
    "list_connector_impls",
    "register_connector",
    "register_connector_v2",
]

_log = structlog.get_logger(__name__)

# v1 single-product registry — shipped in G0.2-T2 (#241). Stays as the
# authoritative table for the pre-G0.6 dispatch path; v2 is the layer
# new code resolves against.
_REGISTRY: dict[str, type[Connector]] = {}

# v2 three-tuple registry — G0.6-T2 (#393). Key is (product, version,
# impl_id). v1 registrations land here as (product, "", "") so v2-aware
# code can resolve every shipped connector uniformly.
_REGISTRY_V2: dict[tuple[str, str, str], type[Connector]] = {}


def register_connector(product: str, cls: type[Connector]) -> None:
    """Register a connector class under a product slug (v1 entry).

    Called at module import time from ``connectors/<product>/__init__.py``.
    Also populates the v2 registry as ``(product, "", "")`` so shipped
    v1 entries participate in :func:`resolve_connector` resolution
    without modification.

    Raises :exc:`TypeError` when ``cls`` is not a :class:`Connector` subclass.
    Raises :exc:`RuntimeError` on duplicate registration in either layer.
    """
    if not (isinstance(cls, type) and issubclass(cls, Connector)):
        raise TypeError(f"connector class for product={product!r} must subclass Connector: {cls!r}")
    if product in _REGISTRY:
        raise RuntimeError(
            f"connector already registered for product={product!r}: "
            f"existing={_REGISTRY[product].__name__}, attempted={cls.__name__}"
        )
    key_v2 = (product, "", "")
    if key_v2 in _REGISTRY_V2:
        raise RuntimeError(
            f"connector already registered for v2 key {key_v2!r}: "
            f"existing={_REGISTRY_V2[key_v2].__name__}, attempted={cls.__name__}"
        )
    _REGISTRY[product] = cls
    _REGISTRY_V2[key_v2] = cls
    _log.info("connector_registered", product=product, cls=cls.__name__)
    # Deprecation hint — surfaces in startup logs once per shipped v1
    # connector to flag the upcoming G3.x migration to v2 signatures.
    # Not a warning (deploys are noisy enough); a single info event the
    # operator can grep for.
    _log.info(
        "connector_registered_v1_compat",
        product=product,
        cls=cls.__name__,
        note=(
            "v1 register_connector treats this entry as version='' impl_id=''; "
            "migrate to register_connector_v2 in G3.x"
        ),
    )


def register_connector_v2(
    *,
    product: str,
    version: str,
    impl_id: str,
    cls: type[Connector],
) -> None:
    """Register a connector under the v2 three-tuple key.

    Keyword-only so the call site reads as
    ``register_connector_v2(product="vmware", version="9.0",
    impl_id="vmware-rest", cls=VmwareRestConnector)`` — three positional
    strings would invite ordering bugs.

    Raises :exc:`TypeError` when ``cls`` is not a :class:`Connector` subclass.
    Raises :exc:`RuntimeError` on duplicate registration of the same
    three-tuple key.

    Does **not** write to the v1 registry. v2-only registrations are
    invisible to :func:`get_connector` (which keys on product alone);
    they're only resolvable via the v2 resolver. This is intentional:
    the v1 ``get_connector`` surface predates multi-impl-per-product
    and has no way to disambiguate.
    """
    if not (isinstance(cls, type) and issubclass(cls, Connector)):
        raise TypeError(
            f"connector class for v2 key (product={product!r}, version={version!r}, "
            f"impl_id={impl_id!r}) must subclass Connector: {cls!r}"
        )
    key = (product, version, impl_id)
    if key in _REGISTRY_V2:
        raise RuntimeError(
            f"connector already registered for v2 key {key!r}: "
            f"existing={_REGISTRY_V2[key].__name__}, attempted={cls.__name__}"
        )
    _REGISTRY_V2[key] = cls
    _log.info(
        "connector_registered_v2",
        product=product,
        version=version,
        impl_id=impl_id,
        cls=cls.__name__,
    )


def get_connector(product: str) -> type[Connector] | None:
    """Look up a v1 connector class by product slug. Returns ``None`` if not found."""
    return _REGISTRY.get(product)


def all_connectors() -> dict[str, type[Connector]]:
    """Return a snapshot of the v1 registry — for diagnostics / introspection."""
    return dict(_REGISTRY)


def all_connectors_v2() -> dict[tuple[str, str, str], type[Connector]]:
    """Return a snapshot of the v2 registry — for diagnostics / introspection.

    Includes v1 entries (as ``(product, "", "")``) so a single call lists
    every registered connector regardless of which entry point its module
    used.
    """
    return dict(_REGISTRY_V2)


def list_connector_impls() -> list[tuple[str, str, str]]:
    """Return the v2 keys as a sorted list — for diagnostics.

    Sorted by ``(product, version, impl_id)`` so startup-log greps and
    debug endpoints render deterministically across hosts.
    """
    return sorted(_REGISTRY_V2.keys())


def _eager_import_connectors() -> None:
    """Import every ``connectors/<product>/`` subpackage so registrations land.

    Called from ``main.py`` lifespan. Each subpackage self-registers by
    calling :func:`register_connector` (v1) or :func:`register_connector_v2`
    (v2) at module top-level.

    Subpackages are imported in name-sorted order so startup log lines
    (one ``connector_registered`` event per registration) are stable
    across restarts and across hosts. Behaviour is order-independent
    today, but deterministic ordering keeps deploy diffs comparable and
    avoids surprises if a future connector ever takes a registration-
    time side-effect on another connector's presence.
    """
    import meho_backplane.connectors as pkg

    for _, name, ispkg in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m[1]):
        if ispkg:
            importlib.import_module(f"{pkg.__name__}.{name}")


def clear_registry() -> None:
    """Empty both registry layers. Test-only — never call from production code."""
    _REGISTRY.clear()
    _REGISTRY_V2.clear()
