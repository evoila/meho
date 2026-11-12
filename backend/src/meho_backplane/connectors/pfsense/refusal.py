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

    The dispatcher cuts the error message to 256 characters, but keeps up to
    2048 characters of ``remediation`` in the error result and the audit
    record. So ``remediation`` carries the whole guidance, and a long list
    (every object that still uses an alias) is not lost.
    """

    def __init__(self, op_id: str, code: str, guidance: str) -> None:
        super().__init__(f"{op_id} refused ({code}): {guidance}")
        self.code = code
        self.guidance = guidance
        self.remediation = guidance
