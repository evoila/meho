# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI route handlers for the operator console (stub).

Empty package for chassis Task #863. T5 (#866) lands the
``APIRouter`` instance + dashboard view (``GET /ui/``) + the five
surface stub routes (``GET /ui/broadcast``, ``/ui/knowledge``,
``/ui/topology``, ``/ui/connectors``, ``/ui/memory``) that
G10.1-G10.5 fill in. T4 (#865) lands the ``/ui/auth/*`` flow.

This package is imported by name from
:mod:`meho_backplane.main` once T5 wires the router; until then the
import surface stays intentionally empty so a stray
``from meho_backplane.ui.routes import router`` fails loudly rather
than silently importing nothing.
"""
