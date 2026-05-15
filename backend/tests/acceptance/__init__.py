# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end acceptance suite.

Each module here runs one MEHO substrate end-to-end against the
external surfaces it claims to support (real vendor spec corpora,
optional live targets). Acceptance tests guard the substrate-to-
substrate handshakes that unit + integration tests cannot prove by
themselves. They are environment-gated: when the external surface
is unavailable they ``skip-in-sandbox`` rather than fail, so CI
runs without the corpus stay green.
"""
