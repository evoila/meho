# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end acceptance tests for shipped chassis substrates.

Unlike ``tests/integration/`` (which boots a testcontainers
PostgreSQL instance to verify dialect-specific behaviour), the
acceptance suite drives shipped service-layer entry points against
the unit-test SQLite default — same engine the production CLI / REST
/ MCP surfaces use in their own per-test fixtures. The test answers
"does the substrate produce the documented outcome when you feed it
the consumer's real input?" without paying the container-boot cost.

The first member is :mod:`test_g07_vsphere_canary` — the G0.7 (spec
ingestion pipeline) acceptance gate. It ingests the consumer's
checked-in vCenter OpenAPI spec, runs operator-review + enable, and
asserts the 10-query govc-parity benchmark.
"""
