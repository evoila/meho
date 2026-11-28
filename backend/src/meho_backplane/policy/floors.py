# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The floors: rules in the code that no policy file can loosen.

The policy evaluator (:mod:`meho_backplane.policy.evaluate`) checks the
floors first. A floor sets the least strict answer a call can get. A later
step can make the answer stricter, never looser. The one exception is
named on the floor itself: a delete grant made in the web console may run
a ``destructive`` call of a service login while the instance is in
sandbox mode (#4622).

The floors come from the code, never from a file. The two floors that are
fixed rules take their rule ids from :mod:`meho_backplane.policy.fixed_rules`
(``destructive_waits`` and ``credential_waits``). They are:

* **Reader asks for a change.** A login with the Reader role is denied a
  call that asks for a change (the mark ``asks_for_change``), whatever its
  kind (:func:`reader_change_floor`). It is checked before every other rule.
* **Role floor.** A person below the role an operation needs is denied
  (:mod:`meho_backplane.operations.role_floors`).
* **Destructive.** A ``destructive`` call always waits for a person.
* **Token calls.** A call made with a token (a person's token, an agent,
  a runner) runs at once only when it is ``safe``. A ``caution``,
  ``dangerous`` or ``destructive`` call waits.
* **Credentials.** A call of an agent or a service login that reads,
  writes or makes a credential waits for a person, also at level
  ``safe``, whatever its grants say.
* **No grant.** Some calls wait for a person every time: no standing grant
  and no run approval clears them. These are an operation that reads,
  writes or makes a credential, one that connects to an address the caller gives, one marked
  for per-call approval, and one a classification makes always wait.
  :func:`grant_may_clear` and :func:`run_approval_may_clear` say this. In
  sandbox mode a delete grant still clears an operation with the mark
  ``sandbox_delete_grant`` (today only ``k8s.delete``).

Pure functions; no database and no settings. One exception: with
``CUSTOM_POLICY`` on, :func:`call_floors` puts the token floor before the
fixed rules, so a fixed rule names a wait that both make (the rule on the
audit row, see ``docs/codebase/policy-evaluator.md``). With the switch off
the order is as before. The verdict is the same in both orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from meho_backplane.auth.operator import TenantRole
from meho_backplane.operations.role_floors import (
    READER_CHANGE_CODE,
    reader_change_refusal,
    role_floor_denial,
)
from meho_backplane.policy.fixed_rules import CREDENTIAL_WAITS, DESTRUCTIVE_WAITS
from meho_backplane.policy.switches import custom_policy_on_in_environment

if TYPE_CHECKING:
    from meho_backplane.policy.evaluate import Asked, Asker

__all__ = [
    "CREDENTIAL_WAITS",
    "DESTRUCTIVE_WAITS",
    "KNOWN_NON_SAFE",
    "NO_GRANT_MARKS",
    "FloorHit",
    "call_floors",
    "grant_may_clear",
    "reader_change_floor",
    "run_approval_may_clear",
]

#: The risk levels above ``safe``. An unknown level is not in this set: the
#: shipped defaults handle it (a person waits, an agent is denied).
KNOWN_NON_SAFE: Final[frozenset[str]] = frozenset({"caution", "dangerous", "destructive"})

#: Operation marks (:attr:`Asked.op_class`) that no grant and no run approval clears.
NO_GRANT_MARKS: Final[frozenset[str]] = frozenset(
    {"credential", "per_call_approval", "classified_always_wait"}
)


@dataclass(frozen=True)
class FloorHit:
    """One floor that holds for a call: the least strict verdict and its rule.

    ``source`` is ``"role_floor"`` for the role floor and ``"floor"`` for
    the others. ``sandbox_delete_grant_clears`` is ``True`` only on the
    destructive floor of a service login, and on the credential floor of a
    service login's call of an op with the mark ``sandbox_delete_grant``:
    there a sandbox delete grant may still run the call.
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


#: The mark of :attr:`Asked.op_class` for a call that asks for a change
#: (:func:`~meho_backplane.operations.role_floors.asks_for_change`).
ASKS_FOR_CHANGE: Final[str] = "asks_for_change"

#: The rule id of :func:`reader_change_floor`.
READER_ASKS_FOR_CHANGE: Final[str] = "reader_asks_for_change"


def reader_change_floor(asker: Asker, asked: Asked) -> FloorHit | None:
    """The deny of a Reader who asks for a change, or ``None``.

    The same rule and text as the policy gate
    (:func:`~meho_backplane.operations.role_floors.reader_change_denial`),
    for every kind of login.
    """
    if asker.tenant_role is not TenantRole.READ_ONLY:
        return None
    sentence = reader_change_refusal(asker.tenant_role, is_change=ASKS_FOR_CHANGE in asked.op_class)
    if sentence is None:
        return None
    text = f"{READER_CHANGE_CODE}: {sentence}"
    return FloorHit("deny", READER_ASKS_FOR_CHANGE, text, source="role_floor")


def call_floors(asker: Asker, asked: Asked) -> list[FloorHit]:
    """Return every floor that holds for *asker* calling *asked*, in order.

    A role floor denial comes first: nothing after it can change a deny.
    On a tie the later floor names the answer. With ``CUSTOM_POLICY`` on the
    token floor comes before the fixed rules, so the fixed rule names it.
    """
    hits: list[FloorHit] = []
    if asker.kind == "person":
        denial = role_floor_denial(asked.op_id, asker.tenant_role)
        if denial is not None:
            hits.append(FloorHit("deny", "role_floor", denial, source="role_floor"))
    fixed = _fixed_rule_floors(asker, asked)
    token = _token_floor(asker, asked)
    if custom_policy_on_in_environment():
        return [*hits, *token, *fixed]
    return [*hits, *fixed, *token]


def _fixed_rule_floors(asker: Asker, asked: Asked) -> list[FloorHit]:
    """The floors that are fixed rules: ``destructive_waits`` and ``credential_waits``."""
    hits: list[FloorHit] = []
    if asked.risk == "destructive":
        hits.append(
            FloorHit(
                "wait",
                DESTRUCTIVE_WAITS,
                "a destructive call always waits for a person",
                sandbox_delete_grant_clears=asker.kind == "service",
            )
        )
    if asker.kind in ("agent", "service") and CREDENTIAL_WAITS in asked.op_class:
        hits.append(
            FloorHit(
                "wait",
                CREDENTIAL_WAITS,
                "the operation reads, writes or makes a credential, so a person approves each call",
                sandbox_delete_grant_clears=(
                    asker.kind == "service" and "sandbox_delete_grant" in asked.op_class
                ),
            )
        )
    return hits


def _token_floor(asker: Asker, asked: Asked) -> list[FloorHit]:
    """``token_runs_only_safe`` when a call made with a token is above ``safe``."""
    if not (_token_call(asker) and asked.risk in KNOWN_NON_SAFE):
        return []
    text = f"risk level {asked.risk}: a call made with a token runs only safe operations at once"
    return [FloorHit("wait", "token_runs_only_safe", text)]


def grant_may_clear(asked: Asked, *, sandbox: bool) -> bool:
    """True when a standing grant may clear a service login's wait on *asked*.

    A delete-shaped operation can only be cleared by a sandbox delete grant.
    In sandbox mode that grant also clears a credential operation with the
    mark ``sandbox_delete_grant``.
    """
    marks = asked.op_class & NO_GRANT_MARKS
    if sandbox and "sandbox_delete_grant" in asked.op_class:
        marks -= {"credential"}
    if asked.caller_address or marks:
        return False
    return sandbox or "delete_shaped" not in asked.op_class


def run_approval_may_clear(asked: Asked) -> bool:
    """True when a run approval may clear a service login's wait on *asked*."""
    if asked.caller_address or asked.op_class & NO_GRANT_MARKS:
        return False
    return not asked.op_class & {"delete_shaped", "root_shell"}
