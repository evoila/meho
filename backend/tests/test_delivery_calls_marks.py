# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The seven delivery calls carry no ``hard_to_undo`` on the whole operation.

Whether a delivery call waits depends on the call itself, not on the whole
operation. So the stored rows of these seven calls have no ``hard_to_undo``.
Their ``changes_rights`` stays: on for the cluster apply, off for the rest.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

_CHANGES_RIGHTS = {
    "argocd.app.sync": False,
    "argocd.app.rollback": False,
    "argocd.app.set": False,
    "argocd.app.refresh": False,
    "k8s.apply": True,
    "k8s.job.create": False,
    "k8s.exec": False,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_the_stored_rows_of_the_delivery_calls_have_no_hard_to_undo() -> None:
    rows = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id in _CHANGES_RIGHTS
    ]
    assert {row.op_id for row in rows} == set(_CHANGES_RIGHTS)
    for row in rows:
        assert row.hard_to_undo is None, row.op_id
        assert bool(row.changes_rights) is _CHANGES_RIGHTS[row.op_id], row.op_id
