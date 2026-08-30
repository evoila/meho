# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reference add-on test double for the pairing contract proof plane (#3030).

Initiative #2900 gives the backplane a first-class **add-on pairing contract**
across four planes, each shipped and unit-tested on its own:

* pairing + identity (#3025, ``addon-pairing.md``),
* capability advertisement (#3026, same doc),
* durable step-event push (#3027, ``addon-step-events.md``),
* out-of-process audit parent-linkage (#3028, ``addon-parent-linkage.md``).

This module is the **proof plane** for the whole contract: a single reference
add-on *test double* that participates in every plane exactly as a real paired
add-on (first consumers: meho-automation, meho-ssp) would, so CI proves the
planes compose end to end without any external process.

It is deliberately a ``tests/`` harness, **not** a shipped connector or add-on:

* it drives the *real* services (``AddonPairingService``,
  ``AddonCapabilityService``, ``AddonStepEventService``, the
  ``addon_orchestration`` linkage seam) — the double is the add-on, not a
  reimplementation of the backplane;
* the only stub is Keycloak, monkey-patched at the pairing boundary so no live
  realm is needed (mirrors ``test_addon_capability_service``). The
  ``get_service_account_user_id`` stub is load-bearing: pairing captures its
  return value as ``service_account_sub``, the identity join every later plane
  keys on, so a double that forgets to stub it silently pairs with a ``NULL``
  sub and every step-event / linkage assertion fails closed;
* the meta-tool family it advertises is *data* (an ``addon_capability`` row),
  never a tool name on the agent waist — postulate 5. ``test_addon_reference_double``
  pins that advertising it grows no ``tools/list`` surface.

The double is not collected as a test (no ``test_`` prefix); import it::

    from tests.addon_reference_double import ReferenceAddon
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations._audit import agent_session_id_var, parent_audit_id_var
from meho_backplane.operations.addon_capability import AddonCapabilityService
from meho_backplane.operations.addon_capability_schemas import (
    CapabilityDeclaration,
    CapabilityDeclarationResponse,
    CapabilityKind,
    DeclareCapabilitiesRequest,
)
from meho_backplane.operations.addon_orchestration import (
    OrchestrationRun,
    bound_parent_linkage,
    resolve_or_open_orchestration_run,
)
from meho_backplane.operations.addon_pairing import AddonPairingService
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.operations.addon_pairing_schemas import PairAddonRequest
from meho_backplane.operations.addon_step_events import (
    AddonStepEventService,
    StepEventListResponse,
)

#: The monkeypatch target for the pairing service's Keycloak admin client —
#: the single seam the double stubs so pairing needs no live realm.
_KEYCLOAK_PATCH_TARGET = "meho_backplane.operations.addon_pairing.KeycloakAdminClient.from_settings"


def _mock_keycloak(*, internal_id: str, service_account_sub: str) -> MagicMock:
    """A stubbed ``KeycloakAdminClient.from_settings`` for the pairing boundary.

    Returns the client-provisioning triple the real pairing path reads back:
    the created client's internal id, its generated secret, and — crucially —
    the service-account **user id**, which pairing persists as
    ``AddonPairing.service_account_sub`` (the #3027 identity join). Every method
    is an ``AsyncMock`` so the ``async with kc_client:`` context and its awaited
    calls resolve without a live Keycloak.
    """
    client = AsyncMock()
    client.create_client = AsyncMock(return_value=internal_id)
    client.get_client_secret = AsyncMock(return_value="reference-double-secret")
    client.get_service_account_user_id = AsyncMock(return_value=service_account_sub)
    client.delete_client = AsyncMock(return_value=None)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=client)


@dataclass
class ReferenceAddon:
    """A paired add-on modelled end to end against the real contract services.

    Construct with :meth:`pair`, then exercise each plane:

    * :meth:`advertise_meta_tool_family` — capability advertisement (#3026);
    * :meth:`produce_step_event` / :meth:`consume_step_events` — the durable
      step-event push contract (#3027), scoped to this double's own lineage;
    * :meth:`run_orchestration` — out-of-process audit parent-linkage (#3028),
      collapsing multiple dispatches under one work_ref into a single replay
      subtree;
    * :meth:`unpair` — reversible teardown (#3025).

    The double holds exactly the identity a real add-on's client-credentials
    token carries: its ``keycloak_client_id`` (``addon:<name>``) and the
    ``service_account_sub`` captured at pair time. :meth:`operator` mints the
    matching ``PrincipalKind.SERVICE`` operator the linkage seam authorizes
    against.
    """

    tenant_id: uuid.UUID
    name: str
    service_account_sub: str
    keycloak_internal_id: str

    @classmethod
    async def pair(
        cls,
        *,
        tenant_id: uuid.UUID,
        name: str = "reference-double",
        created_by_sub: str = "op-admin",
        service_account_sub: str = "svc-reference-double",
        keycloak_internal_id: str = "kc-reference-double",
        addon_contract_version: int = BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version: int = BACKPLANE_CONTRACT_VERSION,
    ) -> ReferenceAddon:
        """Pair the double through the real :class:`AddonPairingService`.

        Keycloak is stubbed (:func:`_mock_keycloak`) so the handshake runs with
        no live realm; the stub's ``get_service_account_user_id`` return value
        is what pairing persists as ``service_account_sub`` and this double
        records for the identity join every later plane uses.
        """
        request = PairAddonRequest(
            name=name,
            addon_contract_version=addon_contract_version,
            addon_min_backplane_version=addon_min_backplane_version,
        )
        with patch(
            _KEYCLOAK_PATCH_TARGET,
            _mock_keycloak(
                internal_id=keycloak_internal_id,
                service_account_sub=service_account_sub,
            ),
        ):
            await AddonPairingService().pair(tenant_id, created_by_sub, request)
        return cls(
            tenant_id=tenant_id,
            name=name,
            service_account_sub=service_account_sub,
            keycloak_internal_id=keycloak_internal_id,
        )

    @property
    def keycloak_client_id(self) -> str:
        """The add-on's globally-unique OAuth ``clientId`` (``addon:<name>``)."""
        return f"addon:{self.name}"

    def operator(self) -> Operator:
        """The ``SERVICE`` operator a real add-on's client-credentials token yields.

        Carries the double's ``client_id`` and ``sub`` so the orchestration
        linkage seam (#3028) recognises it as this pairing's principal. Minted
        ``read_only`` — the add-on's standing authority, exactly as pairing
        provisions the Keycloak client.
        """
        return Operator(
            sub=self.service_account_sub,
            raw_jwt="reference-double-not-a-real-jwt",
            tenant_id=self.tenant_id,
            tenant_role=TenantRole.READ_ONLY,
            principal_kind=PrincipalKind.SERVICE,
            client_id=self.keycloak_client_id,
        )

    async def advertise_meta_tool_family(
        self,
        family: str,
        *,
        event_kinds: tuple[str, ...] = (),
    ) -> CapabilityDeclarationResponse:
        """Declare a ``meta_tool_family`` capability (plus optional event kinds).

        The advertised *family* is data — an ``addon_capability`` row — never a
        tool name on the agent waist (postulate 5). ``declare`` is replace-all,
        so this is the double's complete surface set.
        """
        capabilities: list[CapabilityDeclaration] = [
            CapabilityDeclaration(kind=CapabilityKind.META_TOOL_FAMILY, name=family)
        ]
        capabilities.extend(
            CapabilityDeclaration(kind=CapabilityKind.EVENT_KIND, name=kind) for kind in event_kinds
        )
        return await AddonCapabilityService().declare(
            self.tenant_id,
            self.name,
            DeclareCapabilitiesRequest(capabilities=capabilities),
        )

    async def produce_step_event(
        self,
        *,
        event_kind: str,
        work_ref: str | None,
        payload: dict[str, object],
        audit_id: uuid.UUID | None = None,
    ) -> None:
        """Produce a step event the backplane attributes to this double.

        Uses the fail-open committed recorder with ``owner_principal_sub`` set
        to the double's own ``service_account_sub`` — the producer-side identity
        (an approval requester's sub, an agent-run's identity sub) that the
        recorder joins to a pairing. An event for a *different* sub is a no-op,
        which is exactly the cross-pairing isolation the contract guarantees.
        """
        await AddonStepEventService().record_if_owned_committed(
            tenant_id=self.tenant_id,
            owner_principal_sub=self.service_account_sub,
            event_kind=event_kind,
            work_ref=work_ref,
            audit_id=audit_id,
            payload=payload,
        )

    async def consume_step_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> StepEventListResponse:
        """Read this double's own durable step-event log, resumable by ``seq``.

        Binds to its pairing exactly as the subscription route does — by the
        caller's token ``sub`` (:meth:`resolve_pairing_for_sub`) — then reads
        strictly forward from ``after_seq``. A double only ever sees events in
        its own lineage; another add-on's log is unreachable, not filtered.
        """
        service = AddonStepEventService()
        pairing = await service.resolve_pairing_for_sub(
            tenant_id=self.tenant_id,
            service_account_sub=self.service_account_sub,
        )
        if pairing is None:
            raise AssertionError(
                "reference double could not bind its subscription — pairing "
                "absent or service_account_sub not captured at pair time"
            )
        return await service.list_for_pairing(
            pairing_id=pairing.id,
            after_seq=after_seq,
            limit=limit,
        )

    async def run_orchestration(
        self,
        *,
        work_ref: str,
        dispatch_op_ids: list[str],
    ) -> OrchestrationRun:
        """Drive a multi-dispatch external run that replays as one audit subtree.

        The first ``call_operation`` under *work_ref* opens the orchestration
        run (synthesised parent audit row); every later dispatch resolves the
        same run and executes under :func:`bound_parent_linkage`, so each
        DISPATCH audit row inherits the run's ``agent_session_id`` +
        ``parent_audit_id``. Each dispatch is modelled by writing the DISPATCH
        row the way ``write_audit_row`` builds it — reading the lineage
        contextvars — so the double proves inheritance, not just wiring.
        Returns the resolved :class:`OrchestrationRun` (its ``session_id`` is
        the replay anchor).
        """
        if not dispatch_op_ids:
            raise ValueError("run_orchestration needs at least one dispatch op_id")
        operator = self.operator()
        run: OrchestrationRun | None = None
        for op_id in dispatch_op_ids:
            # A fresh resolve per dispatch mirrors the out-of-process reality:
            # each call_operation arrives in its own request context and
            # resolve-or-opens the same (client_id, work_ref) run.
            resolved = await resolve_or_open_orchestration_run(operator, work_ref)
            if resolved is None:
                raise AssertionError(
                    "reference double was refused parent-linkage — it must be a "
                    "paired service principal for its own work_ref"
                )
            run = resolved
            async with bound_parent_linkage(run):
                await self._write_dispatch_row(op_id)
        assert run is not None  # non-empty dispatch list guarantees it
        return run

    async def _write_dispatch_row(self, op_id: str) -> None:
        """Insert one DISPATCH audit row exactly as ``write_audit_row`` would.

        Reads ``parent_audit_id_var`` / ``agent_session_id_var`` so a dispatch
        running inside :func:`bound_parent_linkage` records its inherited
        lineage — the property replay descends.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    occurred_at=datetime.now(UTC),
                    operator_sub=self.service_account_sub,
                    tenant_id=self.tenant_id,
                    parent_audit_id=parent_audit_id_var.get(),
                    agent_session_id=agent_session_id_var.get(),
                    method="DISPATCH",
                    path=op_id,
                    status_code=200,
                    duration_ms=Decimal("1.00"),
                    payload={"op_id": op_id},
                )
            )
            await session.commit()

    async def unpair(self) -> bool:
        """Unpair the double, deleting the Keycloak client then the row.

        Reversible: the row is hard-deleted so the backplane returns
        byte-identical to a never-paired one. Keycloak is stubbed as for
        :meth:`pair`.
        """
        with patch(
            _KEYCLOAK_PATCH_TARGET,
            _mock_keycloak(
                internal_id=self.keycloak_internal_id,
                service_account_sub=self.service_account_sub,
            ),
        ):
            return await AddonPairingService().unpair(self.tenant_id, self.name)
