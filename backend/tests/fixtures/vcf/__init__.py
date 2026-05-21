# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded HTTP fixtures for the four VCF management-plane connectors.

Each consumer's per-connector subdirectory holds JSON-serialised
``RecordedResponse`` files (one per endpoint + scenario) replayed by the
E2E tests in G3.6-T15 / T18 / T21 / T24 (#837/#838/#839/#840).

The refresh tooling lives in :mod:`refresh` — an operator-run script that
hits a live appliance, records responses, redacts secrets, and writes them
into ``backend/tests/fixtures/vcf/<connector>/`` for replay.

See ``docs/cross-repo/vcf-fixture-refresh.md`` for the operator recipe.
"""
