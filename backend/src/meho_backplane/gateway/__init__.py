# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central side of the remote-execution gateway command plane (#2415).

Task #2498. Re-exports the durable ``gateway_command`` queue service so
consumers (the runner-facing routes in :mod:`meho_backplane.api.v1.gateway`
and #2500's capability-minting path) import from the package root.
"""

from __future__ import annotations

from meho_backplane.gateway.queue import (
    GATEWAY_LONGPOLL_DEFAULT_WAIT_SECONDS,
    GATEWAY_LONGPOLL_MAX_WAIT_SECONDS,
    GatewayCommandNotDeliveredError,
    GatewayCommandNotFoundError,
    claim_next_command,
    clamp_longpoll_wait,
    enqueue_command,
    record_result,
)

__all__ = [
    "GATEWAY_LONGPOLL_DEFAULT_WAIT_SECONDS",
    "GATEWAY_LONGPOLL_MAX_WAIT_SECONDS",
    "GatewayCommandNotDeliveredError",
    "GatewayCommandNotFoundError",
    "claim_next_command",
    "clamp_longpoll_wait",
    "enqueue_command",
    "record_result",
]
