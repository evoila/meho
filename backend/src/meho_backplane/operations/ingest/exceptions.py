# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Domain exceptions for the G0.7 ingest-review state machine.

The two exception classes here are the documented failure modes
:class:`~meho_backplane.operations.ingest.ReviewService` raises:

* :class:`InvalidStateTransition` — the caller asked for a
  state transition the machine forbids (e.g. ``enabled → staged``).
  This is a programming bug at the call site, not an operator-
  recoverable condition, but the message is kept structured so the
  CLI / API layers (T5 / T6) can map it onto a 400 with a clear
  detail field.

* :class:`ConnectorNotFound` — the
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

Both classes inherit from :class:`Exception` directly (not
:class:`RuntimeError` or any other stdlib subtype) so callers can
``except`` them precisely without catching unrelated runtime faults.
"""

from __future__ import annotations

from uuid import UUID

__all__ = ["ConnectorNotFoundError", "InvalidStateTransitionError"]


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
