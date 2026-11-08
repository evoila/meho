# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run the REST target and topology writes as a person in the web interface.

A person's call made with a token waits for approval on these routes; only a
web session runs them at once. Tests that check what a write does (and not
who waits) call :func:`run_writes_at_once` on their app. It keeps the real
write gate (role, then a person) and marks the caller as a web session.
"""

from __future__ import annotations

from fastapi import FastAPI

from meho_backplane.api.v1 import targets as targets_api
from meho_backplane.api.v1 import topology as topology_api
from meho_backplane.auth.operator import Operator


def _targets_person(operator: Operator = targets_api._require_admin) -> Operator:
    return targets_api._require_admin_person(operator).model_copy(update={"web_session": True})


def _topology_person(operator: Operator = topology_api._require_admin) -> Operator:
    return topology_api._require_admin_person(operator).model_copy(update={"web_session": True})


def run_writes_at_once(app: FastAPI) -> FastAPI:
    """Mark the caller of the REST write routes of *app* as a web session."""
    app.dependency_overrides[targets_api._require_admin_person] = _targets_person
    app.dependency_overrides[topology_api._require_admin_person] = _topology_person
    return app
