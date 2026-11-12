# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The floors: rules in the code that no policy file can loosen.

The policy evaluator (:mod:`meho_backplane.policy.evaluate`) checks the
floors first. A floor sets the least strict answer a call can get. A later
step can make the answer stricter, never looser. The one exception is
named on the floor itself: a delete grant made in the web console may run
a ``destructive`` call of a service login while the instance is in
sandbox mode (#4622).

The floors come from the code, never from a file. They are:

* **Role floor.** A person below the role an operation needs is denied
  (:mod:`meho_backplane.operations.role_floors`).
* **Destructive.** A ``destructive`` call always waits for a person.
* **Token calls.** A call made with a token (a person's token, an agent,
  a runner) runs at once only when it is ``safe``. A ``caution``,
  ``dangerous`` or ``destructive`` call waits.
* **No grant.** Some calls wait for a person every time: no standing grant
  and no run approval clears them. These are an operation that shows a
  secret, one that connects to an address the caller gives, one marked
  for per-call approval, and one a classification makes always wait.
  :func:`grant_may_clear` and :func:`run_approval_may_clear` say this.

Pure functions; no database and no settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from meho_backplane.operations.role_floors import role_floor_denial

if TYPE_CHECKING:
    from meho_backplane.policy.evaluate import Asked, Asker

__all__ = [
    "KNOWN_NON_SAFE",
    "NO_GRANT_MARKS",
    "FloorHit",
    "call_floors",
    "grant_may_clear",
    "run_approval_may_clear",
]

#: The risk levels above ``safe``. An unknown level is not in this set: the
#: shipped defaults handle it (a person waits, an agent is denied).
KNOWN_NON_SAFE: Final[frozenset[str]] = frozenset({"caution", "dangerous", "destructive"})

#: Operation marks (:attr:`Asked.op_class`) that no grant and no run approval clears.
NO_GRANT_MARKS: Final[frozenset[str]] = frozenset(
    {"credential_read", "per_call_approval", "classified_always_wait"}
)


@dataclass(frozen=True)
class FloorHit:
    """One floor that holds for a call: the least strict verdict and its rule.

    ``source`` is ``"role_floor"`` for the role floor and ``"floor"`` for
    the others. ``sandbox_delete_grant_clears`` is ``True`` only on the
    destructive floor of a service login: there a sandbox delete grant may
    still run the call.
    """

    verdict: Literal["wait", "deny"]
    rule_id: str
    text: str
    source: Literal["floor", "role_floor"] = "floor"
    sandbox_delete_grant_clears: bool = False


def _token_call(asker: Asker) -> bool:
    """True for a call made with a token: an agent, a runner, or a person outside the web."""
    if asker.kind in ("agent", "runner"):
        return True
    return asker.kind == "person" and asker.via == "token"


def call_floors(asker: Asker, asked: Asked) -> list[FloorHit]:
    """Return every floor that holds for *asker* calling *asked*, in order.

    A role floor denial comes first: nothing after it can change a deny.
    """
    hits: list[FloorHit] = []
    if asker.kind == "person":
        denial = role_floor_denial(asked.op_id, asker.tenant_role)
        if denial is not None:
            hits.append(FloorHit("deny", "role_floor", denial, source="role_floor"))
    if asked.risk == "destructive":
        hits.append(
            FloorHit(
                "wait",
                "destructive_waits",
                "a destructive call always waits for a person",
                sandbox_delete_grant_clears=asker.kind == "service",
            )
        )
    if _token_call(asker) and asked.risk in KNOWN_NON_SAFE:
        hits.append(
            FloorHit(
                "wait",
                "token_runs_only_safe",
                f"risk level {asked.risk}: a call made with a token runs only safe "
                "operations at once",
            )
        )
    return hits


def grant_may_clear(asked: Asked, *, sandbox: bool) -> bool:
    """True when a standing grant may clear a service login's wait on *asked*.

    A delete-shaped operation can only be cleared by a sandbox delete grant.
    """
    if asked.caller_address or asked.op_class & NO_GRANT_MARKS:
        return False
    return sandbox or "delete_shaped" not in asked.op_class


def run_approval_may_clear(asked: Asked) -> bool:
    """True when a run approval may clear a service login's wait on *asked*."""
    if asked.caller_address or asked.op_class & NO_GRANT_MARKS:
        return False
    return not asked.op_class & {"delete_shaped", "root_shell"}
