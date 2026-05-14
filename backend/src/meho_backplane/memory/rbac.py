# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-scope RBAC for the memory module.

G5.1-T1 (#421). The matrix codifies consumer-needs.md §G5 L143-148:

* **Writes** -- user-flavoured scopes are open to any authenticated
  operator (they're writing their own memory, the service binds the
  operator's ``sub`` server-side). ``tenant``-scoped writes require
  ``tenant_admin`` (shared guardrails are a privileged operation;
  letting any operator scribble on the tenant-shared corpus is the
  G5.2 promotion verb's job, gated behind a deliberate flow rather
  than a default-allow write). ``target``-scoped writes are open to
  any operator within the tenant -- the tenant boundary already
  isolates targets, and G0.3's per-target access policy (#224) will
  tighten this later when the policy layer lands. See
  "G0.3 follow-up" note below.
* **Reads** -- user-flavoured scopes require ``operator.sub``
  matches the stored ``user_sub``. Tenant + target scopes are open to
  any operator in the tenant; the tenant boundary is enforced by the
  ``documents.tenant_id`` column upstream, so RBAC here is purely
  about within-tenant visibility.

The resolver is intentionally stateless and pure -- every method takes
the operator (and the relevant context) and returns a verdict. No
database access, no shared state. This keeps RBAC decisions trivially
testable as a matrix and means a future per-target ACL check (when
G0.3's policy layer lands) plugs in cleanly without rewiring the
service.

G0.3 follow-up
--------------

Consumer-needs.md §G5 says ``target``-scoped reads should resolve via
G0.3's per-target access policy. That policy ships in
Initiative #224 (still in progress at filing time). For G5.1-T1 the
resolver treats target access as "operator is in the tenant the
target belongs to", which the upstream ``tenant_id`` filter already
enforces. When G0.3-T3's ``resolve_target`` arrives, the
:meth:`MemoryRbacResolver.can_read` /
:meth:`MemoryRbacResolver.can_write` branches for ``USER_TARGET`` /
``TARGET`` are the two call sites to tighten -- the resolver shape
already takes ``target_name`` precisely so this future call has the
information it needs.
"""

from __future__ import annotations

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.memory.schemas import (
    TARGET_SCOPED,
    USER_SCOPED,
    MemoryScope,
)

__all__ = ["MemoryRbacResolver", "PermissionDeniedError"]


class PermissionDeniedError(Exception):
    """The operator is not permitted to take the requested memory action.

    Raised by :meth:`MemoryService.remember` and
    :meth:`MemoryService.forget` when the RBAC matrix denies the
    operation. ``recall`` deliberately does **not** raise on read
    denial -- it returns ``None`` so a caller cannot distinguish
    "not-found" from "forbidden" (the info-leak avoidance the issue
    AC names).

    The HTTP route layer (T2 #422) catches this exception and
    translates it to a 403 response; the CLI verbs (T4 #424) render
    it as a non-zero exit + human-readable message.
    """

    def __init__(self, scope: MemoryScope, reason: str) -> None:
        self.scope = scope
        self.reason = reason
        super().__init__(f"permission denied for scope={scope.value}: {reason}")


class MemoryRbacResolver:
    """Resolve whether an operator can read or write a memory scope.

    Stateless; methods are pure functions of their arguments. Wired
    into :class:`MemoryService` as a constructor dep so tests can
    substitute a fake resolver. Production callers do not need to
    instantiate this directly -- the service builds one internally
    when not injected.
    """

    def can_write(
        self,
        operator: Operator,
        scope: MemoryScope,
        target_name: str | None,
    ) -> bool:
        """Return ``True`` when *operator* may write to *scope*.

        * ``USER`` / ``USER_TENANT`` / ``USER_TARGET`` -- any
          authenticated operator. ``USER_TARGET`` additionally needs
          ``target_name`` (the service-level guard catches the missing
          value before reaching here; the resolver still requires it
          for parity with read-side reasoning).
        * ``TENANT`` -- only ``tenant_admin``. Tenant-shared memory is
          a privileged surface; non-admin operators write to user-
          scoped first and promote via G5.2's ``meho promote`` verb.
        * ``TARGET`` -- any operator in the tenant. Tenant boundary
          isolates targets in v0.2; G0.3-T3's policy layer will
          tighten via ``target_name`` lookup.

        ``read_only`` operators are denied every write across every
        scope -- the role's name is load-bearing for the rest of the
        backplane and memory writes are no exception.
        """
        if operator.tenant_role is TenantRole.READ_ONLY:
            return False
        if scope in TARGET_SCOPED and target_name is None:
            # Service layer catches this earlier with a clearer error;
            # the resolver still guards so a direct caller (test, future
            # adapter) cannot bypass.
            return False
        if scope is MemoryScope.TENANT:
            return operator.tenant_role is TenantRole.TENANT_ADMIN
        return True

    def can_read(
        self,
        operator: Operator,
        scope: MemoryScope,
        user_sub: str | None,
        target_name: str | None,
    ) -> bool:
        """Return ``True`` when *operator* may read a memory in *scope*.

        Tenant scoping is enforced by the ``documents.tenant_id``
        column upstream of this call; the resolver only decides
        within-tenant visibility.

        * ``USER`` / ``USER_TENANT`` / ``USER_TARGET`` -- the stored
          ``user_sub`` must equal ``operator.sub``. Two operators in
          the same tenant cannot see each other's user-scoped memory.
          ``user_sub == None`` for a user-scoped row means the row is
          corrupt; the resolver denies rather than wildcard-allowing.
        * ``TENANT`` / ``TARGET`` -- any operator (including
          ``read_only``) in the tenant. Tenant-shared knowledge is
          the substrate's purpose; gating it on role would defeat the
          "team becomes the unit of memory" property from
          consumer-needs.md §G5 L131.
        """
        if scope in USER_SCOPED:
            if user_sub is None:
                return False
            return operator.sub == user_sub
        # TENANT / TARGET — tenant boundary enforced upstream.
        # target_name is accepted for forward-compat with G0.3's
        # per-target policy; the v0.2 resolver does not consult it.
        del target_name
        return True

    def visible_kinds(self, operator: Operator) -> list[str]:
        """Return the ``documents.kind`` values *operator* can ever read.

        Used by :meth:`MemoryService.list_memories` and
        :meth:`MemoryService.search_memories` to scope the SQL filter
        server-side -- the kind list narrows the candidate set
        *before* the per-row ``can_read`` post-filter on user_sub.
        Every authenticated operator can read all five kinds in
        principle (the per-row check is what gates user-scoped
        access); read-only operators see the same surface as
        operators do for reads. Returns the strings :class:`Document`
        rows actually carry so callers can plug straight into
        :func:`~meho_backplane.retrieval.retriever.retrieve`'s
        ``kind`` filter or an ``IN (...)`` SQL clause.
        """
        # Keep stable ordering matching MemoryScope iteration so tests
        # can pin the result list without a sort step.
        del operator
        return [f"memory-{scope.value}" for scope in MemoryScope]
