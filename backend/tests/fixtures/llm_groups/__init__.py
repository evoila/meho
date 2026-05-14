# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-curated test fixtures for the T3 LLM grouping pipeline.

Two sizes ship today:

* :mod:`small_corpus` -- 5 ops, 2 groups. Drives the happy-path test
  + the prompt-rendering snapshot.
* :mod:`medium_corpus` -- 50 ops, multiple group buckets, one
  ``"none"`` assignment. Drives the call-count assertion
  (``1 + ceil(50/50) = 2`` LLM calls) + the audit-payload shape.

Per the AI-engineering best-practices pack ("hand-curated ground truth
before LLM-judge"), the fixtures are checked into source control with
deterministic stub responses so the test suite is fully reproducible
without a real LLM. A future opt-in integration test runs the same
fixtures against a real Claude Haiku call to verify the prompt + schema
roundtrip on a live model -- but that test stays off the unit-test
gate (gated on ``ANTHROPIC_API_KEY``).
"""
