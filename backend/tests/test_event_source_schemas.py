# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema validation tests for the ``event_source`` registry (#2880)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from meho_backplane.db.models import EventSource as EventSourceORM
from meho_backplane.event_source.schemas import (
    EventSource,
    EventSourceCreate,
    EventSourceUpdate,
    project_event_source_to_summary,
)


def test_create_valid_minimal() -> None:
    body = EventSourceCreate(
        name="Prod Alertmanager",
        slug="prod-am",
        kind="alertmanager",
        auth_strategy="hmac-sha256",
    )
    assert body.slug == "prod-am"
    assert body.status.value == "active"
    assert body.secret is None


@pytest.mark.parametrize("bad_slug", ["UPPER", "has space", "-leading", "trailing-", "under_score"])
def test_create_rejects_bad_slug(bad_slug: str) -> None:
    with pytest.raises(ValidationError):
        EventSourceCreate(name="n", slug=bad_slug, kind="alertmanager", auth_strategy="hmac-sha256")


def test_create_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        EventSourceCreate(
            name="n",
            slug="s",
            kind="alertmanager",
            auth_strategy="hmac-sha256",
            tenant_id="00000000-0000-0000-0000-000000000000",  # forbidden extra
        )


def test_create_rejects_unknown_enum_values() -> None:
    with pytest.raises(ValidationError):
        EventSourceCreate(name="n", slug="s", kind="syslog", auth_strategy="hmac-sha256")


def test_secret_is_masked_and_absent_from_read_model() -> None:
    """``secret`` is a masked SecretStr on writes and absent from reads."""
    body = EventSourceCreate(
        name="n",
        slug="s",
        kind="grafana",
        auth_strategy="static-header",
        secret=SecretStr("super-secret-token"),
    )
    # Never exposes the raw value through repr / str.
    assert "super-secret-token" not in repr(body)
    assert "super-secret-token" not in str(body)
    assert body.secret is not None
    assert body.secret.get_secret_value() == "super-secret-token"
    # The read model has no secret field at all.
    assert "secret" not in EventSource.model_fields


def test_update_all_optional_and_immutable_name_slug() -> None:
    """Empty update is valid; ``name`` / ``slug`` are not patchable."""
    assert EventSourceUpdate().model_dump(exclude_unset=True) == {}
    with pytest.raises(ValidationError):
        EventSourceUpdate(name="new")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        EventSourceUpdate(slug="new")  # type: ignore[call-arg]


def test_project_to_summary_maps_columns() -> None:
    now = datetime.now(UTC)
    row = EventSourceORM(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="am",
        slug="am",
        kind="alertmanager",
        auth_strategy="basic",
        secret_ref="tenants/x/event-sources/am",
        status="paused",
        extras={"k": "v"},
        created_by_sub="admin-1",
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    summary = project_event_source_to_summary(row)
    assert summary.slug == "am"
    assert summary.status.value == "paused"
    assert summary.auth_strategy.value == "basic"
    assert summary.secret_ref == "tenants/x/event-sources/am"
    # The summary intentionally omits extras.
    assert "extras" not in summary.model_fields
