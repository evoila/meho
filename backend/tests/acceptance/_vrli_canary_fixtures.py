# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.6 vRLI dispatch tests.

This module is the vRLI sibling of :mod:`tests.acceptance._nsx_canary_fixtures`.
The acceptance + E2E modules under :mod:`tests` share the same plumbing: a
registered :class:`~meho_backplane.connectors.vcf_logs.VcfLogsConnector`
instance with a stub credentials loader (so no Vault read fires), a probed
:class:`~meho_backplane.db.models.Target` row, the curated ingested
:class:`~meho_backplane.db.models.EndpointDescriptor` rows from
:data:`~meho_backplane.connectors.vcf_logs.core_ops.VRLI_CORE_OPS` plus the
``vrli.event.query`` typed row (#2295), and a :mod:`respx`-mocked vRLI REST
surface answering the session-create POST plus every read op the connector
dispatches against.

The vRLI delta relative to NSX
-------------------------------

* **Auth shape**: vRLI's session-establish is ``POST /api/v2/sessions``
  with a JSON body ``{username, password, provider}`` (NOT form-encoded,
  NOT HTTP Basic). The response body carries ``sessionId``. Downstream
  calls send ``Authorization: Bearer <sessionId>`` (NOT a cookie pair).
* **401 retry contract**: identical to NSX's posture — re-login once on
  401 from a downstream call; a second 401 raises ``RuntimeError`` naming
  the target. The contract lives on
  :meth:`VcfLogsConnector._get_json_with_session_retry`.
* **Path-template ops**: the ingested ``aggregated-events`` op carries
  ``{constraints}`` in its path — the dispatcher's ``_substitute_path`` fills
  it via the ``x-meho-param-loc='path'`` extension key on the
  parameter_schema. The fixture seeds the descriptor with that key + an
  empty-string default, so dispatch_ingested substitutes an empty trailing
  segment when the caller passes ``constraints=""``. The raw events query is
  the ``vrli.event.query`` typed op since #2295; it builds the constraint
  sub-path in the handler, not via ``_substitute_path``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vcf_logs import (
    VRLI_CONNECTOR_ID,
    VRLI_CORE_GROUPS,
    VRLI_CORE_OPS,
    VRLI_IMPL_ID,
    VRLI_PRODUCT,
    VRLI_VERSION,
    VcfLogsConnector,
)
from meho_backplane.connectors.vcf_logs.session import VcfLogsTargetLike
from meho_backplane.connectors.vcf_logs.typed_ops import VRLI_EVENT_QUERY_OP
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "VRLI_CANARY_BASE_URL",
    "VRLI_CANARY_EVENTS",
    "VRLI_CANARY_FINGERPRINT",
    "VRLI_CANARY_OPERATOR_TENANT",
    "VRLI_CANARY_SESSION_ID",
    "VRLI_CANARY_SESSION_REFRESH_ID",
    "VRLI_EVENT_QUERY_HANDLER_REF",
    "VRLI_FORCE_HANDLE_LIST_OP_ID",
    "VRLI_RESERVED_CONSTRAINT_OP_ID",
    "VRLI_RESERVED_CONSTRAINT_VALUE",
    "VRLI_RESERVED_CONSTRAINT_WIRE_PATH",
    "VRLI_TARGET_NAME",
    "IngestedVrliCanary",
    "_insert_vrli_descriptors",
    "_insert_vrli_reserved_constraint_descriptor",
    "_insert_vrli_typed_event_query_descriptor",
    "_register_vrli_reserved_constraint_route",
    "_register_vrli_routes",
    "_vrli_credentials_loader",
    "ingested_vrli_canary",
    "vrli_acceptance_operator",
]

#: Tenant the vRLI dispatch tests act under. ``tenant_admin``-scoped
#: operator; the descriptor + group rows themselves stay built-in
#: (``tenant_id=None``) — production vRLI content ships as built-in.
VRLI_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-000000000fff")

#: Stable :class:`Target.name` the seeded vRLI target carries. Tests
#: refer to it through :attr:`IngestedVrliCanary.target_name`.
VRLI_TARGET_NAME: str = "vrli-acceptance"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress
#: fires even if respx's transport patching ever regressed. Port 443
#: keeps ``HttpConnector._base_url`` from appending a ``:port``
#: suffix, so the respx ``base_url`` matches the connector's client
#: URL exactly.
VRLI_CANARY_BASE_URL: str = "https://vrli-canary.test.invalid"

#: The session id the canary's first ``POST /api/v2/sessions`` returns
#: in the response body. Subsequent downstream calls land
#: ``Authorization: Bearer <VRLI_CANARY_SESSION_ID>`` on the wire.
VRLI_CANARY_SESSION_ID: str = "canary-vrli-session-token"

#: The session id the canary's *second* ``POST /api/v2/sessions`` returns,
#: used by the 401-retry tests to assert the cache is invalidated +
#: refreshed (not stale-served).
VRLI_CANARY_SESSION_REFRESH_ID: str = "canary-vrli-session-token-refreshed"

#: Persisted as ``Target.fingerprint`` — what the connector resolver
#: reads to bind the target's ``product`` + ``version`` against the
#: ``VcfLogsConnector.supported_version_range`` advertisement
#: (``>=9.0,<10.0``). The probe route normally writes this dict at
#: first-probe time; the dispatch tests seed it directly so the resolver
#: binds the connector without a real probe round-trip.
VRLI_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="vmware",
    product="vrli",
    version=VRLI_VERSION,
    build="21761695",
    reachable=True,
    probed_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    probe_method="GET /api/v2/version",
    extras={"release_name": "VMware Aria Operations for Logs 9.0", "patch": "0"},
).model_dump(mode="json")

#: The list op the JSONFlux force-handle test dispatches. Since #2295 this is
#: the ``vrli.event.query`` **typed** op (``source_kind="typed"``): events.query
#: is the explicit DoD acceptance criterion ("vcf-logs query E2E asserts the
#: JSONFlux handle path"), and the conversion means the handle path is now
#: exercised on the typed dispatch surface — the connector-agnostic reducer
#: wraps the typed handler's ``{events: [...]}`` envelope the same way it wrapped
#: the ingested row's response.
VRLI_FORCE_HANDLE_LIST_OP_ID: str = VRLI_EVENT_QUERY_OP.op_id

#: Dotted handler path the seeded typed ``vrli.event.query`` descriptor carries
#: so the dispatcher's ``import_handler`` walk resolves the bound method and
#: binds it to the connector instance. Built from the op's ``handler_attr`` so
#: it can't drift from the real method name.
VRLI_EVENT_QUERY_HANDLER_REF: str = (
    f"meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector."
    f"{VRLI_EVENT_QUERY_OP.handler_attr}"
)

#: Synthetic event listing — 11 rows so the force-handle reducer sees a
#: populated set with a sample-row slice. Fields match the vRLI event
#: shape (timestamp, hostname, text) the connector's `printQuery`
#: renders.
VRLI_CANARY_EVENTS: dict[str, object] = {
    "events": [
        {
            "timestamp": 1747896000000 + (i * 60000),
            "hostname": f"esx-canary-{i:02d}.lab",
            "text": f"login failure on attempt {i}",
            "source": "syslog",
        }
        for i in range(11)
    ],
    "complete": True,
}

#: Synthetic aggregated-events payload — 3 bins with monotonic values.
_VRLI_CANARY_AGGREGATED: dict[str, object] = {
    "bins": [
        {"minTimestamp": 1747896000000 + (i * 3600000), "value": (i + 1) * 10} for i in range(3)
    ],
}

#: Synthetic fields catalog.
_VRLI_CANARY_FIELDS: dict[str, object] = {
    "fields": [
        {"name": "hostname", "type": "string", "source": "static"},
        {"name": "timestamp", "type": "long", "source": "static"},
        {"name": "text", "type": "string", "source": "static"},
        {"name": "vmw_nsx_thread", "type": "string", "source": "com.vmware.nsx"},
    ],
}

#: Synthetic hosts inventory.
_VRLI_CANARY_HOSTS: dict[str, object] = {
    "hosts": [
        {
            "hostname": f"esx-canary-{i:02d}.lab",
            "sourceType": "syslog",
            "lastReceivedTimestamp": "2026-05-22T10:00:00Z",
        }
        for i in range(3)
    ],
}

#: Synthetic content-pack listing.
_VRLI_CANARY_CONTENT_PACKS: dict[str, object] = {
    "contentPackMetadataList": [
        {"namespace": "com.vmware.nsx", "name": "NSX-T", "contentPackVersion": "1.0.0"},
        {"namespace": "com.vmware.vsan", "name": "vSAN", "contentPackVersion": "2.0.0"},
    ],
}

#: Synthetic alert listing.
_VRLI_CANARY_ALERTS: dict[str, object] = {
    "alerts": [
        {"name": "high-error-rate", "enabled": True, "hitCount": 10},
        {"name": "credential-fail-burst", "enabled": False, "hitCount": 0},
    ],
}

#: Path-template params for the ingested ops whose URLs carry the
#: ``{constraints}`` placeholder. The dispatcher's ``_substitute_path``
#: fills it; the respx routes are registered against the substituted
#: URLs (vRLI accepts an empty trailing path segment as "no extra
#: constraint", matching the wrapper's posture). Only aggregated-events
#: remains ingested here — the raw events query is the ``vrli.event.query``
#: typed op since #2295 (it takes the same ``constraints`` param).
VRLI_CONSTRAINT_OP_PARAMS: dict[str, dict[str, object]] = {
    "GET:/api/v2/aggregated-events/{constraints}": {"constraints": ""},
}


async def _insert_vrli_descriptors() -> None:
    """Seed the curated vRLI core ops + the typed events op + their groups.

    One :class:`OperationGroup` per entry in :data:`VRLI_CORE_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor` per
    entry in :data:`VRLI_CORE_OPS` (``is_enabled=True``,
    ``source_kind='ingested'``, ``handler_ref=None``), plus the
    ``vrli.event.query`` **typed** row (``source_kind='typed'``,
    ``handler_ref`` set) so a ``call_operation`` dispatch of the events query
    resolves to the bound method on the connector (#2295).

    Multiple ops can share the same group (e.g. the ingested
    ``aggregated-events`` op and the typed ``vrli.event.query`` op both map to
    ``vrli-events``); the helper coalesces shared group_keys into one inserted
    group row. This is the integration double the typed-registration trap warns
    about: without the typed row the events dispatch would 404 at lookup.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group in VRLI_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=VRLI_PRODUCT,
                version=VRLI_VERSION,
                impl_id=VRLI_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in VRLI_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=VRLI_PRODUCT,
                version=VRLI_VERSION,
                impl_id=VRLI_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"vRLI core op {op.op_id} (curated read).",
                description=f"vRLI core op {op.op_id} (curated read).",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:vcf-logs-9.0/openapi.yaml"],
            )
            session.add(descriptor)

        # The typed events op (#2295). ``method`` / ``path`` are NULL for a
        # typed row (the handler owns the transport call); the dispatcher
        # resolves ``handler_ref`` and binds it to the connector instance.
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=VRLI_PRODUCT,
                version=VRLI_VERSION,
                impl_id=VRLI_IMPL_ID,
                op_id=VRLI_EVENT_QUERY_OP.op_id,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref=VRLI_EVENT_QUERY_HANDLER_REF,
                group_id=group_ids[VRLI_EVENT_QUERY_OP.group_key],
                summary=VRLI_EVENT_QUERY_OP.summary,
                description=VRLI_EVENT_QUERY_OP.description,
                parameter_schema=VRLI_EVENT_QUERY_OP.parameter_schema,
                response_schema=VRLI_EVENT_QUERY_OP.response_schema,
                llm_instructions=VRLI_EVENT_QUERY_OP.llm_instructions,
                safety_level=VRLI_EVENT_QUERY_OP.safety_level,
                requires_approval=VRLI_EVENT_QUERY_OP.requires_approval,
                is_enabled=True,
                tags=list(VRLI_EVENT_QUERY_OP.tags),
            )
        )
        await session.commit()


async def _insert_vrli_typed_event_query_descriptor() -> None:
    """Seed only the ``vrli.event.query`` typed descriptor (reuses vrli-events).

    For tests that want the typed events op available on top of a setup that
    seeded the ingested groups some other way. Requires the ``vrli-events``
    :class:`OperationGroup` to exist already (``_insert_vrli_descriptors``
    creates it); look it up and attach the typed row.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_row = (
            await session.execute(
                select(OperationGroup).where(OperationGroup.group_key == "vrli-events")
            )
        ).scalar_one()
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=VRLI_PRODUCT,
                version=VRLI_VERSION,
                impl_id=VRLI_IMPL_ID,
                op_id=VRLI_EVENT_QUERY_OP.op_id,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref=VRLI_EVENT_QUERY_HANDLER_REF,
                group_id=group_row.id,
                summary=VRLI_EVENT_QUERY_OP.summary,
                description=VRLI_EVENT_QUERY_OP.description,
                parameter_schema=VRLI_EVENT_QUERY_OP.parameter_schema,
                response_schema=VRLI_EVENT_QUERY_OP.response_schema,
                llm_instructions=VRLI_EVENT_QUERY_OP.llm_instructions,
                safety_level=VRLI_EVENT_QUERY_OP.safety_level,
                requires_approval=VRLI_EVENT_QUERY_OP.requires_approval,
                is_enabled=True,
                tags=list(VRLI_EVENT_QUERY_OP.tags),
            )
        )
        await session.commit()


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` declaring every ``{var}`` as a path param.

    Returns the canonical OpenAPI-flavoured shape the G0.7 ingestion
    pipeline produces for path-templated ops: an object schema with each
    ``{name}`` placeholder declared as a property carrying
    ``x-meho-param-loc='path'``. Non-templated paths get the empty
    ``{"type": "object", "properties": {}}`` shape every other ingested
    op uses.
    """
    placeholders = _PATH_VAR_RE.findall(path)
    if not placeholders:
        return {"type": "object", "properties": {}}
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "x-meho-param-loc": "path"} for name in placeholders
        },
        "required": list(placeholders),
    }


async def _vrli_credentials_loader(
    _target: VcfLogsTargetLike, _operator: Operator
) -> dict[str, str]:
    """Stub credentials loader — bypasses the not-yet-wired Vault read.

    The respx ``POST /api/v2/sessions`` route accepts any pair, so the
    values are illustrative. Mirrors the same pattern
    :func:`tests.acceptance._nsx_canary_fixtures._nsx_session_loader`
    uses for NSX.
    """
    return {"username": "vrli-canary-svc", "password": "vrli-canary-pw"}


def _register_vrli_routes(mock: respx.MockRouter) -> None:
    """Register the vRLI session-establish + read-op routes on *mock*.

    The session-create route answers with a JSON body carrying
    ``{"sessionId": ..., "ttl": 1800}``; the connector's
    ``_extract_session_id`` reads ``sessionId`` from the body. Every
    subsequent route returns a pre-seeded JSON body matching the rough
    shape vRLI returns for that path family.

    The ingested ``aggregated-events`` op registers against the empty
    trailing-segment substitution (``/api/v2/aggregated-events`` — no
    trailing slash, because the dispatcher's ``_substitute_path`` collapses
    ``{constraints}`` → ``""`` and strips the leading ``/``). The typed
    ``vrli.event.query`` op builds ``/api/v2/events/`` in the handler for an
    empty constraint (the trailing slash is the ``_EVENTS_PATH_PREFIX``).
    """
    # Session establish.
    mock.post("/api/v2/sessions").respond(
        200,
        json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800},
    )
    # vrli.about — appliance identity probe.
    mock.get("/api/v2/version").respond(
        200,
        json={
            "version": "9.0.0",
            "releaseName": "VMware Aria Operations for Logs 9.0",
            "buildNumber": "21761695",
        },
    )
    # vrli.event.query (typed, #2295) — the handler builds ``/api/v2/events/``
    # for an empty constraint, so the URL on the wire is ``/api/v2/events/``
    # (with the slash). vRLI accepts the trailing-slash form as "no extra
    # constraint", matching the wrapper's posture.
    mock.get("/api/v2/events/").respond(200, json=VRLI_CANARY_EVENTS)
    # vrli.aggregated.query — same trailing-slash shape.
    mock.get("/api/v2/aggregated-events/").respond(200, json=_VRLI_CANARY_AGGREGATED)
    # vrli.field.list / vrli.host.list — appliance inventory.
    mock.get("/api/v2/fields").respond(200, json=_VRLI_CANARY_FIELDS)
    mock.get("/api/v2/hosts").respond(200, json=_VRLI_CANARY_HOSTS)
    # vrli.content.pack.list — installed content packs.
    mock.get("/api/v2/content/contentpack/list").respond(200, json=_VRLI_CANARY_CONTENT_PACKS)
    # vrli.alert.list — configured alert definitions.
    mock.get("/api/v2/alerts").respond(200, json=_VRLI_CANARY_ALERTS)


#: A non-curated reserved-expansion events op the #2003 canary seeds
#: directly. Its path uses RFC6570 reserved expansion ``{+constraints}``
#: so the slash-delimited constraint chain stays literal on the wire —
#: the exact vRLI constraint-query shape the curated empty-constraint op
#: cannot exercise. Kept off :data:`VRLI_CORE_OPS` so the curated 7-op
#: set (and every test keyed on its op_ids) is untouched.
VRLI_RESERVED_CONSTRAINT_OP_ID: str = "GET:/api/v2/events/{+constraints}"

#: A non-empty constraint carrying reserved structural chars: the
#: slash-delimited ``field/OP value`` chain vRLI's printQuery renders.
#: Under reserved expansion the slashes pass through literal, so the wire
#: path is ``/api/v2/events/text/CONTAINS%20error/hostname/CONTAINS%20vcsa``
#: (space still encoded; only the structural ``/`` differs from simple
#: expansion's ``%2F`` mangling).
VRLI_RESERVED_CONSTRAINT_VALUE: str = "text/CONTAINS error/hostname/CONTAINS vcsa"

#: The literal wire path the reserved-expansion op resolves to — slashes
#: preserved, space percent-encoded. The respx route registers against it.
VRLI_RESERVED_CONSTRAINT_WIRE_PATH: str = (
    "/api/v2/events/text/CONTAINS%20error/hostname/CONTAINS%20vcsa"
)


async def _insert_vrli_reserved_constraint_descriptor() -> None:
    """Seed one ``{+constraints}`` reserved-expansion events descriptor (#2003).

    Reuses the ``vrli-events`` group seeded by
    :func:`_insert_vrli_descriptors`; insert that first. The descriptor's
    ``parameter_schema`` declares ``constraints`` as a path param, so the
    dispatcher routes the caller's value into ``_substitute_path`` — which,
    seeing the ``+`` operator, keeps the slash-delimited constraint chain
    literal on the wire.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_row = (
            await session.execute(
                select(OperationGroup).where(OperationGroup.group_key == "vrli-events")
            )
        ).scalar_one()
        method, path = VRLI_RESERVED_CONSTRAINT_OP_ID.split(":", 1)
        descriptor = EndpointDescriptor(
            tenant_id=None,
            product=VRLI_PRODUCT,
            version=VRLI_VERSION,
            impl_id=VRLI_IMPL_ID,
            op_id=VRLI_RESERVED_CONSTRAINT_OP_ID,
            source_kind="ingested",
            method=method,
            path=path,
            handler_ref=None,
            group_id=group_row.id,
            summary="vRLI events query with reserved-expansion constraint (#2003).",
            description="vRLI events query with reserved-expansion constraint (#2003).",
            parameter_schema={
                "type": "object",
                "properties": {
                    "constraints": {"type": "string", "x-meho-param-loc": "path"},
                },
                "required": ["constraints"],
            },
            response_schema={"type": "object"},
            llm_instructions="Reserved-expansion constraint canary.",
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            tags=["spec:vcf-logs-9.0/openapi.yaml"],
        )
        session.add(descriptor)
        await session.commit()


def _register_vrli_reserved_constraint_route(mock: respx.MockRouter) -> Any:
    """Register the literal-slash wire route for the reserved-expansion op.

    Returns the respx route so a test can assert it was called — proof the
    wire URL kept ``/`` literal (a ``%2F``-mangled URL would miss this
    route and 404 against the catch-all).
    """
    return mock.get(VRLI_RESERVED_CONSTRAINT_WIRE_PATH).respond(200, json=VRLI_CANARY_EVENTS)


@dataclass(frozen=True)
class IngestedVrliCanary:
    """Bundle returned by :func:`ingested_vrli_canary`."""

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


@pytest.fixture
def vrli_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the vRLI dispatch tests act as.

    ``tenant_admin`` role so the dispatcher's tenant-scoped queries
    succeed against built-in (``tenant_id=None``) descriptor rows.
    """
    return Operator(
        sub="g36-vrli-acceptance",
        name="G3.6-T6 vRLI Acceptance",
        email=None,
        raw_jwt="<vrli-acceptance-raw-jwt>",
        tenant_id=VRLI_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def ingested_vrli_canary(
    pg_engine: Any,
    vrli_acceptance_operator: Operator,
) -> AsyncIterator[IngestedVrliCanary]:
    """Yield a dispatcher-ready vRLI setup over a respx-mocked appliance."""
    del pg_engine  # the fixture's side-effect is the env we need.

    await _insert_vrli_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=VRLI_CANARY_OPERATOR_TENANT,
            name=VRLI_TARGET_NAME,
            aliases=[],
            # ``Target.product`` binds via the resolver to the v2 registry
            # triple ``("vrli", "9.0", "vrli-rest")``. Since G0.26-T4
            # (#1798) aligned the connector, ``VcfLogsConnector.product``
            # EQUALS ``VRLI_PRODUCT`` (both ``"vrli"``) — the target, the
            # ingested rows, and the registration share one product
            # namespace (the v0.16.0 SEV-2 fix).
            product=VcfLogsConnector.product,
            host=VRLI_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="vrli/vrli-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=VRLI_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._vrli_canary_fixtures.ingested_vrli_canary",
        )
        session.add(target)
        await session.commit()

    registry = all_connectors_v2()
    registry_key = (VcfLogsConnector.product, VRLI_VERSION, VRLI_IMPL_ID)
    connector_cls = registry.get(registry_key)
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.vcf_logs as _vrli_pkg

        importlib.reload(_vrli_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get(registry_key)

    assert connector_cls is VcfLogsConnector, (
        f"expected VcfLogsConnector registered for "
        f"({VcfLogsConnector.product}, {VRLI_VERSION}, {VRLI_IMPL_ID}); got {connector_cls!r}"
    )

    instance = get_or_create_connector_instance(connector_cls)
    # Replace the CredentialsCache's loader callable so no Vault read
    # fires. The cache itself stays in place; the in-memory token cache
    # gets cleared too in case a prior test left stale state.
    instance._credentials._loader = _vrli_credentials_loader  # type: ignore[attr-defined]
    instance._session_tokens.clear()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vrli_routes(mock)
        try:
            yield IngestedVrliCanary(
                operator=vrli_acceptance_operator,
                connector_id=VRLI_CONNECTOR_ID,
                target_name=VRLI_TARGET_NAME,
                base_url=VRLI_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()
