# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The conventions drift check: does MEHO hold the team rules as they are written?

The team rules are written once, in the team wiki. Its CI job renders them
into a settings file that holds only ``conventions`` and sends it here after
each merge. The check compares that file with the caller's tenant's
``operational`` conventions (the ones in the session start text) and reports:

* ``added`` -- a rule in the file that MEHO does not have;
* ``changed`` -- a rule an import of the file would change (only the fields
  that differ, before and after);
* ``only_in_meho`` -- an operational convention MEHO has and the file does not.

It reuses the settings import's checks (:func:`parse_settings_file`), its plan
for conventions and its diff (:func:`diff_specs`), so the answer is what
``POST /api/v1/settings/import?preview=true`` would show for the same file.
It reads the database and writes nothing. A person still imports the file.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.conventions.schemas import ConventionKind
from meho_backplane.instance_settings.diff import diff_specs
from meho_backplane.instance_settings.schema import SettingsTenant, parse_settings_file
from meho_backplane.instance_settings.service import (
    STORED_SECRET_LEAD,
    SettingsForbiddenError,
    _convention_block,
    _load_state,
    _plan_conventions,
    _TenantPlan,
    stored_secret_mistakes,
)

__all__ = ["ConventionsDrift", "check_conventions_drift"]

_OPERATIONAL = ConventionKind.OPERATIONAL.value


@dataclass
class ConventionsDrift:
    """What differs between the file and the tenant's operational conventions."""

    mistakes: list[str] = field(default_factory=list)
    added: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    only_in_meho: list[dict[str, Any]] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        """True only when the file was read and nothing differs."""
        return not (self.mistakes or self.added or self.changed or self.only_in_meho)


def _shape_mistakes(tenant: SettingsTenant) -> list[str]:
    """The drift check reads ``conventions`` only, and each must be operational."""
    loc = "spec.tenants[0]"
    mistakes = [
        f"`{loc}.{section}` is not read by the drift check; send only `conventions`."
        for section in ("policies", "targets")
        if section in tenant.model_fields_set
    ]
    if tenant.conventions is None:
        mistakes.append(f"`{loc}.conventions` is required (an empty list is fine).")
        return mistakes
    for i, entry in enumerate(tenant.conventions):
        if "kind" not in entry.model_fields_set or entry.kind is not ConventionKind.OPERATIONAL:
            mistakes.append(
                f"`{loc}.conventions[{i}].kind` must be `operational`: the drift check "
                f"compares the conventions in the session start text."
            )
    return mistakes


async def check_conventions_drift(
    session: AsyncSession,
    operator: Operator,
    content: bytes,
) -> ConventionsDrift:
    """Compare *content* with the caller's tenant's operational conventions.

    The file must hold exactly one tenant block: the caller's own tenant (its
    ``id`` may be left out). Raises :class:`SettingsForbiddenError` when the
    block names another tenant. Reads the database; writes nothing.
    """
    drift = ConventionsDrift()
    parsed = await asyncio.to_thread(parse_settings_file, content)
    drift.mistakes.extend(parsed.mistakes)
    model = parsed.model
    if model is None:
        return drift
    if len(model.spec.tenants) != 1:
        drift.mistakes.append("`spec.tenants` must hold exactly one tenant block.")
        return drift
    (tenant,) = model.spec.tenants
    tenant_id: uuid.UUID = operator.tenant_id
    if tenant.id is not None and tenant.id != tenant_id:
        raise SettingsForbiddenError(
            "The drift check compares the conventions of the caller's own tenant only; "
            "leave `id` out or use your own tenant id."
        )
    drift.mistakes.extend(_shape_mistakes(tenant))
    if drift.mistakes:
        return drift

    _, rows, _ = await _load_state(session, [tenant_id])
    current = {row.slug: _convention_block(row) for row in rows[tenant_id]}
    before = [block for block in current.values() if block["kind"] == _OPERATIONAL]
    stored = stored_secret_mistakes({str(tenant_id): {"conventions": before}})
    if stored:
        # The answer shows MEHO's text, so a stored secret stops the check as
        # it stops the preview.
        drift.mistakes.extend([STORED_SECRET_LEAD, *stored])
        return drift

    desired = _plan_conventions(
        "spec.tenants[0]",
        tenant.conventions or [],
        current,
        _TenantPlan(tenant_id=tenant_id, index=0, exists=True),
        drift.mistakes,
    )
    if drift.mistakes:
        return drift

    key = str(tenant_id)
    diff = diff_specs(
        {key: {"conventions": before}},
        {key: {"conventions": [b for b in desired if b["kind"] == _OPERATIONAL]}},
        in_file={key: {"conventions": {c.slug for c in tenant.conventions or []}}},
    )["conventions"]
    drift.added = [{"slug": e["key"], "value": e["value"]} for e in diff["added"]]
    drift.changed = [
        {"slug": e["key"], "before": e["before"], "after": e["after"]} for e in diff["changed"]
    ]
    drift.only_in_meho = [{"slug": e["key"]} for e in diff["not_in_file"]]
    return drift
