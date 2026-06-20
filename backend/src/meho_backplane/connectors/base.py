# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Abstract base class for all MEHO connectors.

Every connector implementation (VaultConnector, HttpConnector, etc.) inherits
from :class:`Connector` and provides the three async methods that constitute
the v0.2 surface. The ``Target`` placeholder is replaced with a concrete import
in T5 once G0.3 lands the Target model.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Literal

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import (
    CandidateHint,
    FingerprintResult,
    OperationResult,
    ProbeResult,
    TopologyHints,
)

__all__ = ["Connector", "ShimKind", "shim_kind"]

# Forward declaration — replaced with `from meho_backplane.targets import Target`
# in G0.2-T5 once G0.3 lands the Target model.
type Target = Any

#: G0.28-T1 (#1967) — tri-state dispatchability classification of a
#: connector class, replacing the binary ``issubclass(GenericRestConnector)``
#: predicate that the resolver, dispatcher, ingest-registration, and
#: delete-connector sites used as a "this is a dead shim" discriminator.
#:
#: * ``"none"`` — a hand-coded connector (the default for every
#:   :class:`Connector` subclass): a bespoke per-product class. Fully
#:   dispatchable; the resolver's most-dispatchable tier.
#: * ``"profiled"`` — a
#:   :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`:
#:   an ingested REST connector made dispatchable by a reviewed declarative
#:   ``ExecutionProfile`` (G0.28 Initiative #1965). Dispatchable, but loses
#:   the resolver tie-break to a more-specific hand-coded class so a vetted
#:   profile cannot shadow a bespoke connector (the #1750/#1798 footgun).
#: * ``"bare"`` — a
#:   :class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`
#:   auto-shim: scaffolded on first ingest so a spec resolves before any
#:   per-product class exists, but non-dispatchable (its ``auth_headers`` /
#:   ``execute`` raise :class:`NotImplementedError`). The resolver demotes
#:   it whenever any dispatchable candidate is present.
ShimKind = Literal["bare", "profiled", "none"]


class Connector(ABC):
    """Abstract base for all MEHO connectors.

    Subclasses advertise themselves through five class-level attributes that
    the G0.6 registry v2 (#393) keys on:

    * :attr:`product` — product slug, e.g. ``"vsphere"``, ``"vault"``,
      ``"bind9"``.
    * :attr:`version` — connector implementation version
      (e.g. ``"9.0"`` for a vSphere 9.0 connector). Empty string means
      "unversioned" and preserves v1 single-product registry behaviour.
    * :attr:`impl_id` — implementation discriminator, e.g.
      ``"vmware-rest"`` vs ``"vmware-pyvmomi"``. Empty string preserves
      v1 behaviour.
    * :attr:`supported_version_range` — PEP 440-style version spec
      (e.g. ``">=8.5,<10.0"``) the connector advertises against a
      target's fingerprinted product version. ``None`` means "any
      version" and preserves v1 behaviour.
    * :attr:`priority` — integer tie-break for the registry v2 resolver
      (#393) when two connectors match the same ``(product, version)``;
      higher wins.

    The defaults on the four new attributes are chosen so existing v1
    subclasses (VaultConnector — #244; KubernetesConnector skeleton —
    #321) keep working without modification. Three required async methods
    cover the v0.2 surface; v0.2.next may add streaming.
    """

    # Set on subclass: "vsphere", "vault", "bind9", etc.
    product: str

    # G0.6-T3 (#394) — registry v2 metadata. Defaults preserve v1 behaviour.
    version: str = ""
    impl_id: str = ""
    supported_version_range: str | None = None
    priority: int = 0

    # G0.28-T1 (#1967) — tri-state dispatchability classification. The
    # default ``"none"`` marks every connector a hand-coded class unless it
    # explicitly opts into a shim tier: ``GenericRestConnector`` sets
    # ``"bare"`` and ``ProfiledRestConnector`` sets ``"profiled"``. Read via
    # the module-level :func:`shim_kind` helper, never ``issubclass``. See
    # :data:`ShimKind`.
    _shim_kind: ShimKind = "none"

    @abstractmethod
    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Return the canonical fingerprint shape.

        ``operator`` (optional) is the request-scoped operator. When the
        fingerprint path needs to read per-target vendor credentials from
        Vault (the K8s/vmware/sddc-manager/NSX surface, and any future
        connector that authenticates a session against the fingerprint
        endpoint), the operator's validated Keycloak ``raw_jwt`` flows to
        Vault's JWT/OIDC auth method via
        :func:`~meho_backplane.auth.vault.vault_client_for_operator` —
        the same code path the dispatch surface uses. The probe route
        passes the real route operator; background callers (readiness
        probe, K8s topology refresh) pass ``None`` and the implementation
        falls back to
        :func:`~meho_backplane.connectors._shared.system_operator.synthesise_system_operator`
        which fails closed at the live Vault round-trip (its placeholder
        JWT is deliberately not a valid Keycloak token).

        G0.16-T4 (#1306) widened this signature to converge the probe
        route's Vault credential read with the dispatch path's (the
        ``vault OIDC malformed jwt: must have three parts`` error the
        v0.8.0 dogfood surfaced was the placeholder JWT reaching the
        live Vault loader on the probe route while dispatch passed a
        real operator's JWT). Connectors whose fingerprint does not
        touch Vault (bind9 over SSH, holodeck, the docs surface) accept
        the parameter and ignore it — the widened signature is a
        single-source convergence point, not a behaviour change for
        those connectors.
        """

    @abstractmethod
    async def probe(self, target: Target) -> ProbeResult:
        """Lightweight reachability + auth-challenge check."""

    @abstractmethod
    async def execute(
        self,
        target: Target,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Run a typed operation.

        op_id namespace varies by source_kind:

        * ``'ingested'``  — ``"{METHOD}:{path}"``
          (e.g. ``"GET:/api/vcenter/cluster"``).
        * ``'typed'``     — dotted shape per product
          (e.g. ``"vault.kv.read"``).
        * ``'composite'`` — dotted with ``.composite`` suffix
          (e.g. ``"vmware.composite.vm.create"``).

        In v0.2.next post-G0.6, this method is typically called BY the
        G0.6 dispatcher AFTER lookup + validation; subclasses don't
        implement their own dispatch tables (``register_typed_operation()``
        handles registration). Subclasses MAY still override ``execute()``
        for special transport semantics (streaming, batching) but the
        common path is "look up handler_ref from endpoint_descriptor,
        call the handler".
        """

    # G9.1-T2 (#449) — topology discovery hooks. Default no-op implementations
    # keep every shipped subclass compilable without modification; per-product
    # overrides land in G3.x Initiative tasks (vSphere, Kubernetes, Vault).
    async def discover_topology(self, target: Target) -> TopologyHints:
        """Return the topology snapshot for ``target``.

        The G9.1-T3 refresh service calls this method on demand and on a
        scheduled cadence; the returned :class:`TopologyHints` is diffed
        against existing ``graph_node`` + ``graph_edge`` rows for the
        same ``(tenant_id, target_id)`` and applied as inserts /
        updates / soft-deletes.

        The base-class default returns an empty :class:`TopologyHints`
        with ``discovered_at`` stamped at call time. Connectors that
        can derive nodes + edges from a probe (vSphere, Kubernetes,
        Vault — per Initiative #363) override this method; connectors
        with nothing to contribute inherit the no-op default.
        """
        return TopologyHints(discovered_at=datetime.now(UTC))

    async def list_candidates(
        self,
        seed_target: Target | None = None,
    ) -> list[CandidateHint]:
        """Return potentially-reachable targets the connector inferred.

        The G9.1-T6 ``meho targets discover`` CLI verb surfaces these
        candidates to the operator; ``seed_target`` is optional and lets
        a connector scope the discovery to one known target's reach
        (e.g. for Kubernetes, ``seed_target=cluster-1`` can surface peer
        clusters present in the same kubeconfig context tree).

        The base-class default returns ``[]``. Connectors that can list
        candidates (vSphere — ESXi hosts a vCenter sees but no target
        exists for; Kubernetes — peer cluster contexts) override.
        Auto-registration is out of scope (Initiative #363): the
        operator reviews returned candidates and runs ``meho targets
        create`` to register them.
        """
        return []


def shim_kind(connector: type[Connector] | Connector) -> ShimKind:
    """Return the tri-state dispatchability classification of *connector*.

    Reads the :attr:`Connector._shim_kind` class attribute off either a
    connector **class** (the resolver / ingest-registration / delete sites,
    which classify ``type[Connector]`` candidates) or a connector
    **instance** (the dispatcher, which classifies the live
    ``connector_instance`` it is about to call). The attribute is inherited,
    so the dynamically-synthesised ``AutoShim_*`` subclasses of
    :class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`
    report ``"bare"`` without setting it themselves.

    This is the single classification seam the G0.28 tri-state predicate
    (#1967) routes every former ``issubclass(GenericRestConnector)`` site
    through; see :data:`ShimKind` for the tier semantics.
    """
    return connector._shim_kind
