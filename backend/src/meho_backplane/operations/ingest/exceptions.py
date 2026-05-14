# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Domain exceptions for the G0.7 spec-ingestion pipeline.

Three families of failure modes share this module so the sibling
tasks (parser T1 #401, register-ingested T2 #403, review-queue T4
#402) agree on an import path:

Parser failures (T1 #401) — raised from
:func:`~meho_backplane.operations.ingest.parse_openapi`:

* :class:`InvalidSpecError` — the document is not a structurally
  valid OpenAPI spec or the local file referenced cannot be read.
* :class:`UnsupportedSpecError` — the document is valid OpenAPI but
  ships a flavour the parser doesn't ingest (Swagger 2.0, OpenAPI
  4.x, cross-document ``$ref``).
* :class:`InvalidSchemaError` — a referenced JSON Schema is
  structurally broken (dangling ``$ref``, component-path drill-down,
  non-list parameters).

Bulk-upsert failures (T2 #403) — raised from
:func:`~meho_backplane.operations.ingest.register_ingested_operations`:

* :class:`OpIdCollision` — the incoming spec carries an ``op_id`` a
  row already on the connector was ingested from a *different* spec
  source. The natural key
  ``(product, version, impl_id, op_id)`` is unique per row, so the
  helper refuses to silently overlay one spec's payload onto another
  spec's row; the operator picks the resolution (rename one op via
  ``custom_description`` at T4 review, or skip the conflicting spec).

Review-queue failures (T4 #402) — raised from
:class:`~meho_backplane.operations.ingest.ReviewService`:

* :class:`InvalidStateTransitionError` — the caller asked for a
  state transition the machine forbids (e.g. ``enabled → staged``).
  This is a programming bug at the call site, not an operator-
  recoverable condition, but the message is kept structured so the
  CLI / API layers (T5 / T6) can map it onto a 400 with a clear
  detail field.
* :class:`ConnectorNotFoundError` — the
  ``(product, version, impl_id)`` triple the caller targeted has no
  matching rows visible to the operator. Returned uniformly for
  three failure modes: (a) the connector genuinely does not exist,
  (b) the connector exists but belongs to a different tenant, or
  (c) the connector is built-in (``tenant_id IS NULL``) and the
  operator lacks the ``tenant_admin`` role required to manage
  built-ins. Conflating cross-tenant access denial with "not
  found" matches the v0.2 tenant-isolation discipline used
  elsewhere in the backplane: an operator must not be able to
  enumerate another tenant's connectors by probing for ``HTTP 404``
  vs ``HTTP 403`` boundaries.

The parser classes inherit from :class:`ValueError` so callers that
already catch parsing errors via ``except ValueError`` keep working;
:class:`OpIdCollision` inherits from :class:`ValueError` for the
same reason (ingestion call sites already wrap ``parse_openapi`` in
a ``except ValueError`` block). The review-queue classes inherit
from :class:`Exception` directly so callers can ``except`` them
precisely without catching unrelated runtime faults.
"""

from __future__ import annotations

from uuid import UUID

__all__ = [
    "ConnectorNotFoundError",
    "InvalidSchemaError",
    "InvalidSpecError",
    "InvalidStateTransitionError",
    "OpIdCollision",
    "UnsupportedSpecError",
]


class InvalidSpecError(ValueError):
    """The document is not a structurally valid OpenAPI spec.

    Raised when the root document lacks the ``paths`` key, isn't a
    mapping, or otherwise fails structural validation that does NOT
    depend on the OpenAPI version (those raise
    :exc:`UnsupportedSpecError`).
    """


class UnsupportedSpecError(ValueError):
    """The document is structurally valid but ships a flavour the parser doesn't ingest.

    Raised for Swagger 2.0, OpenAPI 4.x, cross-document ``$ref``, and
    similar known-unsupported cases. The exception message always
    names the offending shape so the operator can decide whether to
    file a v0.2.next request or pre-process the spec.
    """


class InvalidSchemaError(ValueError):
    """A referenced JSON Schema is structurally broken.

    Raised when a ``$ref`` points at a component that doesn't exist,
    when a path's parameter list isn't a list, or when the spec uses
    a structurally unsupported shape (component-path drill-down
    refs, for example).
    """


class OpIdCollision(ValueError):  # noqa: N818 -- name pinned by Task #403 contract
    """An incoming op_id collides with an existing row from a different spec.

    Raised by
    :func:`~meho_backplane.operations.ingest.register_ingested_operations`
    when the bulk upsert would overlay one spec's parsed operation
    onto a row another spec already populated under the same
    ``(product, version, impl_id, op_id)`` natural key.

    Attributes
    ----------
    incoming_spec_source:
        The ``spec_source`` value passed to the failing call (e.g.
        ``"vi-json.yaml"``).
    colliding_op_ids:
        The list of ``op_id`` values the helper refused to overlay,
        sorted alphabetically for stable error messages.
    existing_spec_sources:
        ``{op_id: spec_source}`` mapping of the spec-source tag
        recovered from the existing row's ``tags`` column for each
        colliding op-id. Empty string when the existing row has no
        ``spec:*`` tag (legacy row predating multi-spec merge).

    The exception is raised **before** any row is written, so a
    collision aborts the entire batch — partial ingestion under
    contention is not a supported state.
    """

    def __init__(
        self,
        *,
        incoming_spec_source: str,
        colliding_op_ids: list[str],
        existing_spec_sources: dict[str, str],
    ) -> None:
        self.incoming_spec_source = incoming_spec_source
        self.colliding_op_ids = sorted(colliding_op_ids)
        self.existing_spec_sources = existing_spec_sources
        joined = ", ".join(
            f"{op_id!r} (existing spec={existing_spec_sources.get(op_id, '') or '<untagged>'!r})"
            for op_id in self.colliding_op_ids
        )
        super().__init__(f"spec_source={incoming_spec_source!r} collides on op_id(s): {joined}")


class InvalidStateTransitionError(Exception):
    """Raised when a requested state transition is forbidden by the machine.

    Attributes
    ----------
    current_status:
        The ``review_status`` the row currently holds.
    requested_status:
        The ``review_status`` the caller asked to move to.
    group_key:
        The ``operation_group.group_key`` whose transition was
        rejected; ``None`` when the rejection covers the whole
        connector rather than a single group (e.g. an
        ``enable_connector`` attempt where at least one child group
        was already in an unsupported state).

    The exception message renders as
    ``"cannot transition '<current>' -> '<requested>' (group=<key>)"``
    so the CLI / API layers can surface a clear operator-facing
    detail without doing their own string assembly.
    """

    def __init__(
        self,
        *,
        current_status: str,
        requested_status: str,
        group_key: str | None = None,
    ) -> None:
        self.current_status = current_status
        self.requested_status = requested_status
        self.group_key = group_key
        suffix = f" (group={group_key})" if group_key is not None else ""
        super().__init__(
            f"cannot transition {current_status!r} -> {requested_status!r}{suffix}",
        )


class ConnectorNotFoundError(Exception):
    """Raised when the requested connector has no rows visible to the operator.

    Attributes
    ----------
    connector_id:
        The operator-facing connector identifier (e.g.
        ``"vmware-rest-9.0"``).
    tenant_id:
        The tenant scope the caller asked for. ``None`` indicates a
        built-in scope; a UUID indicates a tenant-curated scope.

    See the module docstring for the three failure modes this
    exception covers; the caller cannot tell them apart from the
    exception alone, which is intentional.
    """

    def __init__(
        self,
        *,
        connector_id: str,
        tenant_id: UUID | None,
    ) -> None:
        self.connector_id = connector_id
        self.tenant_id = tenant_id
        scope = "built-in" if tenant_id is None else f"tenant={tenant_id}"
        super().__init__(f"connector {connector_id!r} not found ({scope})")
