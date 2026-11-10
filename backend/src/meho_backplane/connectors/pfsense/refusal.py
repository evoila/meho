"""The error a pfSense write raises when it refuses a change (#5138).

A write that refuses and changes nothing must be a failed call. It raises
:class:`PfSenseRefusedError`, so the call, its audit record and its approval
all say "failed". An approver must never read "ran OK" for a change that
did not happen.

Answers that are safe to run twice (the wanted state is already true) are
not refusals. They stay normal results.
"""

from __future__ import annotations

__all__ = ["PfSenseRefusedError"]


class PfSenseRefusedError(RuntimeError):
    """A pfSense write was refused and changed nothing.

    ``code`` is the short, stable reason (``ambiguous``, ``referenced``,
    ``conflict``, ...). ``guidance`` says why and what to do.
    """

    def __init__(self, op_id: str, code: str, guidance: str) -> None:
        super().__init__(f"{op_id} refused ({code}): {guidance}")
        self.code = code
        self.guidance = guidance
