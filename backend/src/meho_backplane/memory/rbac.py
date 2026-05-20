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

from typing import Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.memory.schemas import (
    TARGET_SCOPED,
    USER_SCOPED,
    MemoryScope,
)

__all__ = [
    "InvalidPromotionStepError",
    "MemoryRbacResolver",
    "PermissionDeniedError",
    "assert_can_promote",
]

#: Module-level structlog logger. Mirrors :mod:`meho_backplane.auth.rbac` so
#: on-call telemetry can grep ``insufficient_promotion_authority`` and tell at
#: a glance whether a memory-promotion denial is an honest authz failure
#: (operator using a verb they shouldn't reach) or a policy drift.
_log = structlog.get_logger(__name__)


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


# ---------------------------------------------------------------------------
# Scope-aware promotion authority (G5.2-T3 #625)
# ---------------------------------------------------------------------------


class InvalidPromotionStepError(ValueError):
    """The requested ``(source_scope, target_scope)`` pair is not a legal widening.

    Distinct from :class:`PermissionDeniedError` because the failure mode is
    a *request* error, not an authz one -- the caller named a transition the
    ladder does not admit (non-widening, cross-ladder, or identity). The T4
    promote route maps this to **400**, while :class:`PermissionDeniedError`
    maps to 403. Keeping the two exception types separate keeps that mapping
    table at the route boundary readable.

    Inherits :class:`ValueError` because "you gave me an invalid combination
    of arguments" is the Python convention for an input-shape failure.
    """


#: The only legal scope transitions for memory promotion, encoded as an
#: ordered pair (source, target). Two parallel ladders:
#:
#: - **Operator ladder** -- ``user -> user-tenant -> tenant``. Personal
#:   notes broaden to per-tenant personal context, then to the tenant's
#:   shared corpus.
#: - **Target ladder** -- ``user -> user-target -> target``. Personal
#:   notes broaden to per-target personal context, then to the target's
#:   shared corpus.
#:
#: Cross-ladder transitions (e.g. ``user-tenant -> target``) are
#: deliberately absent: ``user-tenant`` is scoped to a tenant and
#: ``target`` is scoped to a target, and the relationship between them
#: is target-belongs-to-tenant, not the other way around -- promoting a
#: tenant-personal note to a target-shared one would silently widen
#: visibility along an axis the operator never authorised.
#:
#: Identity steps (``user -> user``) are also absent: promotion is
#: strictly broadening; same-scope rewrite is the ``forget`` + re-
#: ``remember`` flow, not promotion.
_PROMOTION_LADDER: Final[frozenset[tuple[MemoryScope, MemoryScope]]] = frozenset(
    {
        # Operator ladder
        (MemoryScope.USER, MemoryScope.USER_TENANT),
        (MemoryScope.USER_TENANT, MemoryScope.TENANT),
        # Target ladder
        (MemoryScope.USER, MemoryScope.USER_TARGET),
        (MemoryScope.USER_TARGET, MemoryScope.TARGET),
    }
)


def assert_can_promote(
    operator: Operator,
    source_scope: MemoryScope,
    target_scope: MemoryScope,
    target_id: str | None = None,
) -> None:
    """Authorise *operator* to promote a memory from *source_scope* to *target_scope*.

    Standalone helper -- **not** a FastAPI ``Depends`` factory -- because the
    T4 promote route needs to load the operator, the source row, and the
    target before the authority check can run, and a route-level dependency
    cannot see those.

    The function returns ``None`` on success and raises on every failure
    mode. Three failure modes, three exception classes:

    * :class:`InvalidPromotionStepError` -- ``(source_scope, target_scope)`` is
      not in :data:`_PROMOTION_LADDER`. The route maps this to **400**
      (request shape is wrong; not an authz failure).
    * :class:`NotImplementedError` -- the requested promotion targets a
      per-``target`` ACL branch that G0.3 #224 has not shipped yet, and
      the operator is not a ``tenant_admin`` (the v0.2 fallback). The
      route surfaces this as a 5xx so the gap is loud and visible; T4
      may choose to map it instead to a 501 by the time it ships, but
      this helper does not assume that decision.
    * :class:`PermissionDeniedError` -- the ladder step is legal but the
      operator does not hold the required role for it. The route maps
      this to **403** with detail ``insufficient_promotion_authority``,
      mirroring the existing :class:`PermissionDeniedError` -> 403 path
      G5.1-T2's router uses for :class:`MemoryRbacResolver` denials.

    On a :class:`PermissionDeniedError` raise, a structured
    ``insufficient_promotion_authority`` log line is emitted with
    ``operator_sub`` / ``source_scope`` / ``target_scope`` fields,
    mirroring the shape :func:`meho_backplane.auth.rbac.require_role`
    uses for ``insufficient_role``. Telemetry can grep both event names
    to distinguish route-level role denials from promotion-step authz
    denials at a glance.

    Args:
        operator: The validated :class:`Operator` requesting the promotion.
        source_scope: The current scope of the memory row being promoted.
        target_scope: The desired (strictly broader) scope.
        target_id: The target's identifier when *target_scope* is
            :attr:`MemoryScope.USER_TARGET` or :attr:`MemoryScope.TARGET`.
            Required for the ``user -> user-target`` branch's per-target
            access check; accepted (but not consulted in v0.2) for the
            ``* -> target`` branch which currently falls back to
            ``tenant_admin``-only until G0.3 #224 ships per-target ACLs.

    Returns:
        ``None`` on success.

    Raises:
        InvalidPromotionStepError: The ``(source, target)`` pair is not in the
            promotion ladder.
        PermissionDeniedError: The pair is legal but *operator* lacks the
            authority for it.
        NotImplementedError: The pair would require per-target ACLs from
            G0.3 #224 which have not shipped; the v0.2
            ``tenant_admin``-only fallback was also not met.
    """
    if (source_scope, target_scope) not in _PROMOTION_LADDER:
        raise InvalidPromotionStepError(
            f"promotion {source_scope.value!r} -> {target_scope.value!r} is not a "
            f"legal widening (must be one of: user -> user-tenant -> tenant, or "
            f"user -> user-target -> target)"
        )

    if target_scope is MemoryScope.TENANT:
        # `* -> tenant` always requires tenant_admin. Promotion to a
        # tenant-shared scope is the most authority-sensitive verb in
        # the whole memory surface; consumer-needs.md §G5 is explicit
        # that tenant-scope memories are "real organisational state --
        # others depend on it".
        if operator.tenant_role is not TenantRole.TENANT_ADMIN:
            _log.warning(
                "insufficient_promotion_authority",
                operator_sub=operator.sub,
                source_scope=source_scope.value,
                target_scope=target_scope.value,
            )
            raise PermissionDeniedError(target_scope, "insufficient_promotion_authority")
        return

    if target_scope is MemoryScope.TARGET:
        # `* -> target` would require a per-target ACL check from G0.3
        # #224 (operator has explicit write-access to this target). That
        # ACL surface has not shipped; the v0.2 fallback is
        # tenant_admin-only. A non-admin caller hitting this branch
        # raises NotImplementedError rather than silently denying so
        # the gap stays loud -- consumer-needs.md §G5 names target-
        # shared memory as a privileged surface, and a silent 403 here
        # would mask the missing ACL plumbing.
        if operator.tenant_role is TenantRole.TENANT_ADMIN:
            return
        raise NotImplementedError(
            "per-target promotion ACL needs G0.3 #224; v0.2 falls back to "
            "tenant_admin-only and this operator does not hold that role"
        )

    if target_scope is MemoryScope.USER_TENANT:
        # `user -> user-tenant` is the lightest widening: the memory
        # remains personal (gated by user_sub at read time) but its
        # visibility is now scoped to one tenant rather than every
        # tenant the operator belongs to. Any operator (the role check
        # rejects read_only) in the source memory's tenant can take
        # this step. The tenant-boundary check (operator and source row
        # share tenant_id) lives in the T4 route, not here -- this
        # helper trusts the caller has resolved that already.
        if operator.tenant_role is TenantRole.READ_ONLY:
            _log.warning(
                "insufficient_promotion_authority",
                operator_sub=operator.sub,
                source_scope=source_scope.value,
                target_scope=target_scope.value,
            )
            raise PermissionDeniedError(target_scope, "insufficient_promotion_authority")
        return

    if target_scope is MemoryScope.USER_TARGET:
        # `user -> user-target` requires the operator to have access to
        # the named target. The targets-as-data substrate (G0.3 #224)
        # ships the access check; until that wire-up exists, this
        # helper enforces the "target_id supplied" precondition and
        # leaves the policy hook explicit. Read-only operators are
        # denied (same role-floor as user-tenant promotion). A
        # production caller without target_id is a bug -- the T4 route
        # passes the operator-supplied target_name from the request
        # body so the field is always present here.
        if operator.tenant_role is TenantRole.READ_ONLY:
            _log.warning(
                "insufficient_promotion_authority",
                operator_sub=operator.sub,
                source_scope=source_scope.value,
                target_scope=target_scope.value,
            )
            raise PermissionDeniedError(target_scope, "insufficient_promotion_authority")
        if target_id is None:
            raise InvalidPromotionStepError(
                "promotion to 'user-target' requires target_id (the target's "
                "name); pass it as the assert_can_promote(..., target_id=...) kwarg"
            )
        return

    # Defensive fallthrough -- every ladder-legal target_scope is
    # branched above. If the ladder constant grows and this function
    # isn't updated to match, fail loud rather than silently allow.
    raise NotImplementedError(
        f"promotion to {target_scope.value!r} is in the ladder but has no "
        f"authority branch in assert_can_promote; update both together"
    )
