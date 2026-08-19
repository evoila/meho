# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Inbound external event ingest (#2881).

The JWT-less webhook path a registered ``event_source`` (#2880) delivers to.
The endpoint (:mod:`meho_backplane.api.v1.events_ingest`) resolves the source
by slug and reads the capped body; this package owns everything that follows:

* :mod:`~meho_backplane.events.ingest.config` -- per-source knobs off
  ``event_source.extras`` (body cap, rate limit, replay window, header names).
* :mod:`~meho_backplane.events.ingest.auth` -- constant-time per-strategy
  sender authentication against the Vault-custodied secret.
* :mod:`~meho_backplane.events.ingest.rate_limit` -- the per-source
  fixed-window Valkey limiter.
* :mod:`~meho_backplane.events.ingest.service` -- the orchestration:
  publish to ``event_outbox`` in the same transaction as a synchronous
  ``__ingest__`` audit row, then a fail-open broadcast.
"""

from meho_backplane.events.ingest.auth import IngestAuthError, verify_ingest_auth
from meho_backplane.events.ingest.config import IngestConfig, resolve_ingest_config
from meho_backplane.events.ingest.rate_limit import (
    IngestRateLimitError,
    enforce_ingest_rate_limit,
)
from meho_backplane.events.ingest.service import (
    INGEST_OPERATOR_SUB,
    IngestOutcome,
    IngestPayloadError,
    IngestVaultError,
    ResolvedSource,
    accept_ingest_event,
)

__all__ = [
    "INGEST_OPERATOR_SUB",
    "IngestAuthError",
    "IngestConfig",
    "IngestOutcome",
    "IngestPayloadError",
    "IngestRateLimitError",
    "IngestVaultError",
    "ResolvedSource",
    "accept_ingest_event",
    "enforce_ingest_rate_limit",
    "resolve_ingest_config",
    "verify_ingest_auth",
]
