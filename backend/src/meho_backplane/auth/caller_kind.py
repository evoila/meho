# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who is calling: one function that names the kind of caller.

:func:`caller_kind` gives one of five kinds (:data:`CallerKind`):

* ``person`` -- a person in the web console (:attr:`Operator.web_session`).
  Only the web console counts as a person.
* ``personal_agent`` -- a person's token used by a tool: the command line,
  REST or MCP (:attr:`Operator.token_checked`). MEHO does not guess who or
  what is behind a token.
* ``service_agent`` -- an agent login, a service login, a hosted agent run
  (also one a person started), or a scheduled agent run (it uses the
  agent's own login).
* ``runner`` -- a runner login.
* ``system`` -- a login that MEHO's own code builds
  (:attr:`Operator.in_code_kind`), and a person's login with neither a web
  console session nor a checked token.

The function reads no database and no settings. It decides nothing today.
Other code that tells callers apart its own way
(:func:`~meho_backplane.policy.shadow.asker_of`,
:func:`~meho_backplane.auth.operator.is_human_principal`) stays as it is.
"""

from __future__ import annotations

from meho_backplane.auth.operator import CallerKind, Operator, PrincipalKind

__all__ = ["CALLER_KINDS", "CallerKind", "caller_kind", "token_login_kind"]

#: Every kind :func:`caller_kind` can give.
CALLER_KINDS: tuple[CallerKind, ...] = (
    "person",
    "personal_agent",
    "service_agent",
    "runner",
    "system",
)


def caller_kind(operator: Operator, actor_sub: str | None) -> CallerKind:
    """Return the kind of caller of *operator*.

    *actor_sub* is the agent of a hosted agent run that makes this call
    (:func:`~meho_backplane.auth.delegation.resolve_actor_sub`), or
    ``None``. The checks run in this order:

    1. A hosted agent run is a ``service_agent``, also when a person
       started it in the web console.
    2. A login with :attr:`~Operator.in_code_kind` has that kind.
    3. A person's login in the web console is a ``person``.
    4. A person's login with a checked token
       (:attr:`~Operator.token_checked`) is a ``personal_agent``.
    5. A person's login with neither is ``system``: MEHO's own code made
       it, so it never counts as a person or a person's tool.
    6. A runner login is a ``runner``; an agent or service login is a
       ``service_agent`` (:func:`token_login_kind`).
    """
    if actor_sub:
        return "service_agent"
    if operator.in_code_kind is not None:
        return operator.in_code_kind
    if operator.principal_kind is PrincipalKind.USER:
        if operator.web_session:
            return "person"
        return "personal_agent" if operator.token_checked else "system"
    return token_login_kind(operator.principal_kind)


def token_login_kind(principal_kind: PrincipalKind) -> CallerKind:
    """The kind of caller of a token login of *principal_kind*.

    A runner login is a ``runner``, an agent or service login a
    ``service_agent``, and a person's token login a ``personal_agent``.
    """
    if principal_kind is PrincipalKind.RUNNER:
        return "runner"
    if principal_kind in (PrincipalKind.AGENT, PrincipalKind.SERVICE):
        return "service_agent"
    return "personal_agent"
