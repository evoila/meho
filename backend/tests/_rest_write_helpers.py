# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run the REST target and topology writes as a person in the web interface.

A person's call made with a token waits for approval on these routes; only a
web session runs them at once. Tests that check what a write does (and not
who waits) call :func:`run_writes_at_once` on their app. It keeps the real
write gate (role, then a person) and marks the caller as a web session.

A settings import that adds or changes targets applies only for a web
session too; :func:`settings_import_at_once` does the same for the import
route of the shared app, for the length of a ``with`` block.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Depends, FastAPI

from meho_backplane.api.v1 import instance_settings as settings_api
from meho_backplane.api.v1 import targets as targets_api
from meho_backplane.api.v1 import topology as topology_api
from meho_backplane.auth.operator import Operator
from meho_backplane.middleware import verify_jwt_and_bind


def _targets_person(operator: Operator = targets_api._require_admin) -> Operator:
    return targets_api._require_admin_person(operator).model_copy(update={"web_session": True})


def _topology_person(operator: Operator = topology_api._require_admin) -> Operator:
    return topology_api._require_admin_person(operator).model_copy(update={"web_session": True})


def run_writes_at_once(app: FastAPI) -> FastAPI:
    """Mark the caller of the REST write routes of *app* as a web session."""
    app.dependency_overrides[targets_api._require_admin_person] = _targets_person
    app.dependency_overrides[topology_api._require_admin_person] = _topology_person
    return app


def _settings_person(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    person = settings_api._require_human_settings_admin(operator)
    return person.model_copy(update={"web_session": True})


@contextmanager
def settings_import_at_once(app: FastAPI) -> Iterator[None]:
    """Mark the caller of the settings import of *app* as a web session, then undo it."""
    gate = settings_api._require_human_settings_admin
    app.dependency_overrides[gate] = _settings_person
    try:
        yield
    finally:
        app.dependency_overrides.pop(gate, None)
