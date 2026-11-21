# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The mark for an error whose message MEHO wrote itself.

This module has no imports, so any package can use the mark without a
circular import. :mod:`meho_backplane.connectors.base` re-exports it.
"""

from __future__ import annotations

__all__ = ["ConnectorOwnMessage"]


class ConnectorOwnMessage:
    """Mark an error whose message MEHO or the connector wrote itself.

    Add it as a base class of an error type when the message is a fixed
    sentence of the connector (for example "the account may not read
    licences"). The message must hold no secret and no text from the vendor
    system. For an operation that sends a secret, the dispatcher then shows
    this message instead of hiding it. Never put vendor text, a value from
    the call, or a secret into such a message.
    """
