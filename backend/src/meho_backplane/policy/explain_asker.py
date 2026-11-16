# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who asks ``meho policy explain``: the caller, or a login made up from login groups.

A made-up login (:func:`what_if_operator`) has the role and the flags its
groups give (:func:`~meho_backplane.auth.operator.group_grant`), in the
caller's tenant, and a ``sub`` that no grant names. So it has no standing
grant, no agent grant and no read scope. A question with groups also never
reads the grants of a hosted agent that acts for the caller
(:mod:`meho_backplane.policy.explain` unbinds it).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from meho_backplane.auth.operator import (
    Operator,
    PrincipalKind,
    RoleGroup,
    TenantRole,
    group_grant,
    is_human_principal,
)
from meho_backplane.policy.explain_models import ExplainAsker

__all__ = [
    "READ_ONLY_CANNOT_CALL",
    "WHAT_IF_SUB",
    "WHAT_IF_SUB_REFUSAL",
    "ExplainInputError",
    "Principal",
    "asker_view",
    "what_if_operator",
]

Principal = Literal["person", "agent", "service"]

#: The answer to groups that give only the ``read_only`` role.
READ_ONLY_CANNOT_CALL: Final[str] = (
    "These groups give the read_only role. A read_only login cannot call operations, "
    "so there is no call to explain."
)

#: The ``sub`` of a login made up for a question with groups. No grant names it:
#: the agent grants and the standing grants refuse it (:data:`WHAT_IF_SUB_REFUSAL`).
WHAT_IF_SUB: Final[str] = "policy-explain-what-if"

#: The answer to a grant that names :data:`WHAT_IF_SUB`.
WHAT_IF_SUB_REFUSAL: Final[str] = (
    f"principal_sub {WHAT_IF_SUB!r} is the made-up login of `meho policy explain`. "
    "No grant can name it."
)

_KIND: Final[dict[Principal, PrincipalKind]] = {
    "person": PrincipalKind.USER,
    "agent": PrincipalKind.AGENT,
    "service": PrincipalKind.SERVICE,
}

_ROLE_ORDER: Final[tuple[TenantRole, ...]] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)


class ExplainInputError(ValueError):
    """The question cannot be asked: one sentence for the caller."""


def _group_kind(name: str) -> RoleGroup:
    kind = name.strip().removeprefix("MEHO-")
    try:
        return RoleGroup(kind)
    except ValueError:
        known = ", ".join(group.value for group in RoleGroup)
        raise ExplainInputError(f"Unknown group {name!r}. Use one of: {known}.") from None


def what_if_operator(
    caller: Operator,
    *,
    groups: Sequence[str],
    principal: Principal,
    via: Literal["web", "token"] | None,
) -> Operator:
    """A login of kind *principal* with *groups* in the caller's tenant, and no grants.

    The role is the highest role the groups give. An agent acts with at
    most the ``operator`` role, as for a real agent token. ``via`` counts
    only for a person.
    """
    if not groups:
        raise ExplainInputError("Name at least one group, or ask for yourself.")
    grants = [group_grant(_group_kind(name)) for name in groups]
    role = max((grant.role for grant in grants), key=_ROLE_ORDER.index)
    if principal == "agent" and role is TenantRole.TENANT_ADMIN:
        role = TenantRole.OPERATOR
    if role is TenantRole.READ_ONLY:
        raise ExplainInputError(READ_ONLY_CANNOT_CALL)
    person = principal == "person"
    return Operator(
        sub=WHAT_IF_SUB,
        tenant_id=caller.tenant_id,
        tenant_role=role,
        principal_kind=_KIND[principal],
        client_id=None if person else WHAT_IF_SUB,
        approver=person and any(grant.approver for grant in grants),
        self_approver=person and any(grant.self_approval for grant in grants),
        web_session=person and via == "web",
        raw_jwt="",
    )


def asker_view(asker: Operator, *, me: bool, groups: Sequence[str]) -> ExplainAsker:
    web = asker.web_session and is_human_principal(asker)
    return ExplainAsker(
        me=me,
        kind=asker.principal_kind.value,
        role=asker.tenant_role.value,
        groups=[f"MEHO-{_group_kind(name).value}" for name in groups],
        via="web" if web else "token",
    )
