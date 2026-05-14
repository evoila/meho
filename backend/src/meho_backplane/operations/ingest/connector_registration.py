# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Auto-register a minimal ``HttpConnector`` shim for a freshly-ingested connector.

G0.7-T2 (#403) of Initiative #389. The first time an operator runs
``meho connector ingest --product <p> --version <v> --impl <i>``
against a triple that has no concrete connector class yet, T2 wires
up a thin :class:`HttpConnector` subclass dynamically so the
:func:`~meho_backplane.connectors.resolver.resolve_connector` tie-break
ladder can route to the new connector for read operations the
moment the operator flips the review queue to ``enabled``. Subsequent
ingestions against the same ``(product, version, impl_id)`` skip
this step — the registry is in-memory and the second call sees the
shim already present.

The shim is intentionally **minimal**:

* It fixes the v2 registry-key attributes (``product``, ``version``,
  ``impl_id``, ``supported_version_range``, ``priority``) so the
  resolver picks it up.
* It stashes ``base_url`` on the class for any future override (the
  default :meth:`HttpConnector._base_url` reads from the target, but
  some ingested connectors will need to override it; recording the
  arg is harmless and avoids an inevitable follow-up).
* It raises :class:`NotImplementedError` from :meth:`fingerprint`,
  :meth:`probe`, :meth:`execute`, and :meth:`auth_headers` with a
  message naming the G3.x Initiative responsible for replacing it.

The motivation for "raise" rather than "guess" is operator clarity:
a dispatch attempt against the auto-shim should produce a clear
"this connector is registration-only; a per-product subclass must
ship" error rather than a half-correct fingerprint that pretends
the integration is live. Per-product auth divergence (vSphere's
``POST /api/session``, NSX's XSRF token, vCF's dual-plane) is
authored as a real subclass per G3.x Initiative; that subclass
REPLACES this shim at code-merge time by registering the same
``(product, version, impl_id)`` key.

The dynamically-generated class name follows the pattern
``GenericRest_<product>_<version>_<impl_id>`` (with ``.`` / ``-``
replaced by ``_`` for Python identifier validity). The name is
visible in startup-log ``connector_registered_v2`` events and in
``list_connector_impls()`` diagnostic output so operators can
identify auto-shims at a glance.

Concurrency note: connector registration races (two ingestion calls
against the same triple in flight at the same time) are caught by
the registry's RuntimeError-on-duplicate. The helper checks the
registry under no lock; if a race lands, the second call returns
``False`` (the loser sees the registration as a no-op). v0.2
ingestion is operator-triggered (single-shot CLI), so the race is
theoretical — but the swallow-on-RuntimeError fallback keeps the
helper robust against a future MCP-tool-driven trigger model where
two operators might fire simultaneously.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    register_connector_v2,
)

__all__ = [
    "ensure_connector_class_registered",
]

_log = structlog.get_logger(__name__)

# Match the leading "<major>.<minor>" portion of the version string.
# Anything that isn't ``<int>.<int>`` (e.g. ``"1.x"``, ``"latest"``,
# semver pre-releases) falls back to "any version" (None) so the
# resolver doesn't over-constrain on a version shape it can't parse.
_VERSION_PREFIX_RE = re.compile(r"^(\d+)\.(\d+)")


def _derive_supported_version_range(version: str) -> str | None:
    """Derive a PEP 440-style range from the version string.

    ``"9.0"`` → ``">=9.0,<10.0"``. ``"9.0.1"`` → ``">=9.0,<10.0"``
    (major.minor compatibility window). ``"1.x"`` / ``"latest"`` /
    other non-numeric shapes → ``None`` (matches any version,
    matching the v1 Connector default that empty / None
    ``supported_version_range`` accepts any fingerprinted version).

    Per-product connectors that ship as real subclasses (G3.x) will
    set this attribute explicitly and override the auto-shim; the
    derivation here is only ever load-bearing for the brief window
    between first ingestion and the per-product subclass landing.
    """
    match = _VERSION_PREFIX_RE.match(version)
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    return f">={major}.{minor},<{major + 1}.0"


def _identifier_safe_segment(value: str) -> str:
    """Coerce *value* into a valid Python identifier segment.

    Replaces non-alphanumeric characters with ``_`` and prefixes a
    leading underscore when *value* starts with a digit. The result
    is suitable for use as a class-name fragment passed to
    :func:`type` — ``type()`` rejects names that aren't valid
    Python identifiers at the syntactic level.
    """
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", value)
    if not cleaned:
        return "_"
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def _build_generic_rest_connector_class(
    *,
    product: str,
    version: str,
    impl_id: str,
    base_url: str | None,
) -> type[HttpConnector]:
    """Dynamically construct a minimal :class:`HttpConnector` subclass.

    The returned class is fully instantiable — every
    :class:`~meho_backplane.connectors.base.Connector` abstract method
    is overridden by a stub that raises :class:`NotImplementedError`
    naming the G3.x Initiative responsible for shipping the real
    implementation. The class itself can be registered immediately so
    :func:`~meho_backplane.connectors.resolver.resolve_connector`
    routes to it; the dispatch attempt against an uninhabited op is
    what surfaces the "this connector needs a real subclass" message.
    """

    async def _fingerprint_stub(self: Any, target: Any) -> Any:
        raise NotImplementedError(
            f"GenericRestConnector shim for {self.product!r}/{self.version!r}"
            f"/{self.impl_id!r} is registration-only; a per-product "
            "connector subclass must ship via a G3.x Initiative before "
            "fingerprint() is callable."
        )

    async def _probe_stub(self: Any, target: Any) -> Any:
        raise NotImplementedError(
            f"GenericRestConnector shim for {self.product!r}/{self.version!r}"
            f"/{self.impl_id!r} is registration-only; a per-product "
            "connector subclass must ship via a G3.x Initiative before "
            "probe() is callable."
        )

    async def _execute_stub(
        self: Any,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> Any:
        raise NotImplementedError(
            f"GenericRestConnector shim for {self.product!r}/{self.version!r}"
            f"/{self.impl_id!r} is registration-only; a per-product "
            "connector subclass must ship via a G3.x Initiative before "
            "execute() is callable."
        )

    async def _auth_headers_stub(
        self: Any,
        target: Any,
        raw_jwt: str,
    ) -> dict[str, str]:
        raise NotImplementedError(
            f"GenericRestConnector shim for {self.product!r}/{self.version!r}"
            f"/{self.impl_id!r} is registration-only; auth_headers() needs "
            "a per-product override (session token, basic auth, XSRF, etc.) "
            "shipped via a G3.x Initiative."
        )

    name_segments = "_".join(_identifier_safe_segment(seg) for seg in (product, version, impl_id))
    cls_name = f"GenericRest_{name_segments}"
    attrs: dict[str, Any] = {
        "product": product,
        "version": version,
        "impl_id": impl_id,
        "supported_version_range": _derive_supported_version_range(version),
        "priority": 0,
        # ``base_url`` is recorded on the class so a future G3.x
        # subclass that wants to honour the operator-supplied base
        # URL can read it without re-plumbing the ingestion call.
        # ``HttpConnector._base_url`` reads from the target by
        # default — the recorded value is informational in v0.2.
        "_ingested_base_url": base_url,
        # Repeat the docstring on the class so introspection /
        # operator tooling explains the auto-shim shape.
        "__doc__": (
            f"Auto-registered HTTP connector shim for product={product!r} "
            f"version={version!r} impl_id={impl_id!r}. Registration-only; "
            "fingerprint/probe/execute raise NotImplementedError. Replaced "
            "by a per-product subclass per the G3.x Initiative for this "
            "connector."
        ),
        "fingerprint": _fingerprint_stub,
        "probe": _probe_stub,
        "execute": _execute_stub,
        "auth_headers": _auth_headers_stub,
    }
    return type(cls_name, (HttpConnector,), attrs)


def ensure_connector_class_registered(
    *,
    product: str,
    version: str,
    impl_id: str,
    base_url: str | None = None,
) -> bool:
    """Register a :class:`HttpConnector` shim for *(product, version, impl_id)* if absent.

    Returns ``True`` when a new shim was registered, ``False`` when
    a class for the triple was already present in the v2 registry
    (the typical second-ingestion path on the same connector).

    The helper is intentionally idempotent on the registry side:
    a same-process re-ingestion does no work, and a cross-process
    re-ingestion (process restart) registers a fresh shim that
    behaves identically. Real per-product subclasses that ship via
    G3.x Initiatives MUST register at module-import time
    (lifespan-startup ``_eager_import_connectors``) so the shim does
    not paper over a real connector on cold start — the typical
    deployment ordering is: G3.x lands → image rebuilds → process
    restart → real subclass registers at import → ingestion runs
    against the already-registered key and skips this helper.
    """
    key = (product, version, impl_id)
    if key in all_connectors_v2():
        _log.info(
            "ingested_connector_already_registered",
            product=product,
            version=version,
            impl_id=impl_id,
        )
        return False

    cls = _build_generic_rest_connector_class(
        product=product,
        version=version,
        impl_id=impl_id,
        base_url=base_url,
    )
    try:
        register_connector_v2(
            product=product,
            version=version,
            impl_id=impl_id,
            cls=cls,
        )
    except RuntimeError:
        # Concurrent ingestion against the same triple registered the
        # class between our membership check and the register call.
        # Treat as "already registered" rather than propagating —
        # both calls produced the same observable end state.
        _log.info(
            "ingested_connector_race_registered_by_peer",
            product=product,
            version=version,
            impl_id=impl_id,
        )
        return False

    _log.info(
        "ingested_connector_shim_registered",
        product=product,
        version=version,
        impl_id=impl_id,
        cls=cls.__name__,
        supported_version_range=cls.supported_version_range,
    )
    return True
