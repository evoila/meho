# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central-side machinery for the remote-execution gateway (Initiative #2415).

This package holds **only** the central half of the push-only satellite
runner: the ingest + versioned assignment API (#2499) and, later, the
sibling long-poll / heartbeat surfaces (#2498/#2500/#2501). The runner
half — the tick loop, poll/report client, and on-disk spool — lives under
:mod:`meho_backplane.runner`; the two ends share one wire schema
(:mod:`meho_backplane.runner.wire`) by construction.

#2499 modules:

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
