"""The one check that a written reason for a change never holds a secret.

Two changes ask for a reason: an operation policy edit (also the removal of
an override) and a settings file apply. Both services call
:func:`reason_refusal` before they write anything, so the web console, REST,
the CLI and the MCP tool all get it. It uses the rules that refuse a secret in
a settings file (:func:`~meho_backplane.instance_settings.schema.contains_secret`);
it does not copy them.
"""

from __future__ import annotations

from typing import Final

__all__ = ["REASON_HOLDS_SECRET", "reason_refusal"]

#: The answer for a refused reason. It never repeats any part of the reason.
REASON_HOLDS_SECRET: Final[str] = (
    "The reason looks like a secret. A reason never holds a secret, so MEHO did not "
    "store it and wrote nothing. Write the reason again in plain words."
)


def reason_refusal(reason: str | None) -> str | None:
    """The refusal sentence when *reason* looks like a secret, else ``None``."""
    # Imported here: the settings schema imports back into this package.
    from meho_backplane.instance_settings.schema import contains_secret

    if reason and contains_secret(reason):
        return REASON_HOLDS_SECRET
    return None
