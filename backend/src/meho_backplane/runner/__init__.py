# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Satellite-runner mode — the headless, push-only deploy of the backplane.

One codebase, two deploy modes. The central instance runs the FastAPI app
(``meho_backplane.main:app``); a **runner** runs ``python -m
meho_backplane.runner`` — no UI, no MCP, no local Postgres or Valkey, no
inbound listener. It is a dumb executor of centrally-authorized work: each
tick it polls central over client-initiated HTTP for its assignment,
executes the read-only (``safety_level == "safe"``) operations locally
against the same connector surface, and reports results back, with an
on-disk retry spool covering uplink outages.

Part of the Remote Execution Gateway (Initiative #2415). This package is
the runner chassis (#2497); the central endpoints it polls land in #2499
and the long-poll command plane in #2498.
"""

from __future__ import annotations
