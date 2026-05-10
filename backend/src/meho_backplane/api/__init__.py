# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Versioned HTTP API surfaces.

Each subpackage here corresponds to a stable API version (``v1``, ``v2``,
…). Routes are mounted by :mod:`meho_backplane.main` via
``app.include_router``; this top-level package intentionally re-exports
nothing so that adding ``v2`` later does not implicitly leak ``v1``
symbols.
"""
