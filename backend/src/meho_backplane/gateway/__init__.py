# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central-side machinery for the remote-execution gateway (Initiative #2415).

This package holds **only** the central half of the push-only satellite
runner. The runner half — the tick loop, poll/report client, and on-disk
spool — lives under :mod:`meho_backplane.runner`; the two ends share one
wire schema (:mod:`meho_backplane.runner.wire`) by construction.

#2498 (command plane): the durable ``gateway_command`` queue service,
re-exported below so consumers (the runner-facing routes in
:mod:`meho_backplane.api.v1.gateway` and #2500's capability-minting path)
import from the package root.

#2499 (assignment API) modules:

* :mod:`~meho_backplane.gateway.schemas` — the operator-facing authoring
  envelope (``PUT`` body / response) and the result-ingest accounting
  response.
* :mod:`~meho_backplane.gateway.errors` — typed PUT-time validation
  failures, each with a machine-readable ``error_code``.
* :mod:`~meho_backplane.gateway.repository` — DB access (assignment
  upsert/get, portable result-batch dedup).
* :mod:`~meho_backplane.gateway.assignment_service` — PUT-time validation
  and GET-time materialisation (live target descriptors + op
  handler_ref/safety_level) + the content digest.
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
