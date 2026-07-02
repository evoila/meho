# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.service`.

Coverage matrix (G12.2-T2 / #1296 acceptance criteria):

* ``create_draft`` -- fresh slug -> version=1, status=draft; second draft
  on the same slug -> ``DuplicateDraftError``.
* ``update_or_fork`` -- mutates an existing draft in place (version
  unchanged, ``forked_from=None``); forks a new draft from a published
  version (version bumped, ``forked_from`` populated with the source's
  ``in_flight_run_count``). Only ``in_progress`` runs count.
* ``publish`` -- draft -> published; idempotent re-publish; typed errors
  on deprecated / missing.
* ``deprecate`` -- published -> deprecated; idempotent; typed errors on
  draft.
* ``list_templates`` -- status filter narrows; latest per slug regardless
  of status with no filter; tenant isolation.
* ``show_template`` -- resolves latest when version is ``None``; resolves
  a specific version; raises on missing.

The SQLite path is the load-bearing test driver: the conftest autouse
fixture applies the migrated schema (``runbook_templates`` +
``runbook_runs`` from migration 0034) to a per-test SQLite DB and points
``DATABASE_URL`` at it, so the service's ``get_sessionmaker()`` binds to
the same DB these tests seed ``runbook_runs`` rows into directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunbookRun
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DeprecateTemplateRequest,
    DiscardTemplateRequest,
    DraftTemplateRequest,
    EditTemplateRequest,
    ListTemplatesFilter,
    ManualStep,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import (
    DuplicateDraftError,
    RunbookTemplateService,
    TemplateNotDraftError,
    TemplateNotFoundError,
    TemplateNotPublishedError,
)
from meho_backplane.settings import get_settings

OPERATOR = "operator-sub-1"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars the :class:`Settings` model requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _body(title: str = "Drain node", *, step_body: str = "Run the drain.") -> RunbookTemplateBody:
    """Build a minimal valid template body with one manual step."""
    return RunbookTemplateBody(
        title=title,
        description="Procedure for draining a node.",
        target_kind="k8s-node",
        steps=[
            ManualStep(
                id="drain",
                title="Drain the node",
                body=step_body,
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Node drained?"),
            )
        ],
    )


async def _seed_run(
    tenant_id: uuid.UUID,
    slug: str,
    version: int,
    state: str,
) -> None:
    """Insert one ``runbook_runs`` row pinned to *(slug, version)* in *state*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            RunbookRun(
                tenant_id=tenant_id,
                template_slug=slug,
                template_version=version,
                assigned_to="op",
                target="host-1",
                params={},
                state=state,
                started_by="op",
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_first_version() -> None:
    """A fresh slug yields version=1, status=draft."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()

    resp = await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )

    assert resp.slug == "drain-node"
    assert resp.version == 1
    assert resp.status == "draft"


@pytest.mark.asyncio
async def test_create_draft_duplicate_raises() -> None:
    """A second create_draft on a slug that already has a version raises."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )

    with pytest.raises(DuplicateDraftError):
        await service.create_draft(
            tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
        )


@pytest.mark.asyncio
async def test_create_draft_invalid_slug_raises() -> None:
    """The service revalidates the slug at its boundary (defense in depth)."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()

    # Bypass the request model's pattern check by constructing without
    # validation so the service-layer guard is what fires.
    request = DraftTemplateRequest.model_construct(slug="Bad Slug", body=_body())
    with pytest.raises(InvalidKbSlugError):
        await service.create_draft(tenant_id, OPERATOR, request)


# ---------------------------------------------------------------------------
# update_or_fork
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_or_fork_mutates_draft_in_place() -> None:
    """A slug with a draft at v1: edit mutates it in place; version stays 1."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )
    show_before = await service.show_template(tenant_id, "drain-node")

    resp = await service.update_or_fork(
        tenant_id,
        "operator-2",
        EditTemplateRequest(
            slug="drain-node", body=_body(title="Drain node v2", step_body="Updated.")
        ),
    )

    assert resp.version == 1
    assert resp.status == "draft"
    assert resp.forked_from is None

    show_after = await service.show_template(tenant_id, "drain-node")
    assert show_after.title == "Drain node v2"
    assert show_after.edited_by == "operator-2"
    assert show_after.edited_at >= show_before.edited_at


@pytest.mark.asyncio
async def test_update_or_fork_forks_from_published() -> None:
    """A slug whose only version is published forks a new draft at v2."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))

    resp = await service.update_or_fork(
        tenant_id,
        OPERATOR,
        EditTemplateRequest(slug="drain-node", body=_body(title="Forked")),
    )

    assert resp.version == 2
    assert resp.status == "draft"
    assert resp.forked_from is not None
    assert resp.forked_from.slug == "drain-node"
    assert resp.forked_from.version == 1
    assert resp.forked_from.in_flight_run_count == 0


@pytest.mark.asyncio
async def test_update_or_fork_in_flight_run_count() -> None:
    """Fork reports only in_progress runs pinned to the source version."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))

    # Three in-progress runs against v1 are counted.
    for _ in range(3):
        await _seed_run(tenant_id, "drain-node", 1, "in_progress")
    # Completed / abandoned runs against v1 are NOT counted.
    await _seed_run(tenant_id, "drain-node", 1, "completed")
    await _seed_run(tenant_id, "drain-node", 1, "abandoned")
    # An in-progress run against a different version is NOT counted.
    await _seed_run(tenant_id, "drain-node", 2, "in_progress")

    resp = await service.update_or_fork(
        tenant_id, OPERATOR, EditTemplateRequest(slug="drain-node", body=_body())
    )

    assert resp.forked_from is not None
    assert resp.forked_from.in_flight_run_count == 3


@pytest.mark.asyncio
async def test_update_or_fork_missing_slug_raises() -> None:
    """Editing a slug with no rows at all raises TemplateNotFoundError."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()

    with pytest.raises(TemplateNotFoundError):
        await service.update_or_fork(
            tenant_id, OPERATOR, EditTemplateRequest(slug="ghost", body=_body())
        )


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_valid() -> None:
    """A v1 draft publishes; a re-publish is an idempotent no-op."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )

    resp = await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))
    assert resp.status == "published"
    assert resp.version == 1

    again = await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))
    assert again.status == "published"


@pytest.mark.asyncio
async def test_publish_against_deprecated_raises() -> None:
    """Publishing a deprecated version raises TemplateNotDraftError."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))
    await service.deprecate(tenant_id, DeprecateTemplateRequest(slug="drain-node", version=1))

    with pytest.raises(TemplateNotDraftError):
        await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))


@pytest.mark.asyncio
async def test_publish_against_missing_raises() -> None:
    """Publishing a non-existent template raises TemplateNotFoundError."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()

    with pytest.raises(TemplateNotFoundError):
        await service.publish(tenant_id, PublishTemplateRequest(slug="ghost", version=1))


# ---------------------------------------------------------------------------
# deprecate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprecate_valid() -> None:
    """A published v1 deprecates; a re-deprecate is an idempotent no-op."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))

    resp = await service.deprecate(
        tenant_id, DeprecateTemplateRequest(slug="drain-node", version=1)
    )
    assert resp.status == "deprecated"

    again = await service.deprecate(
        tenant_id, DeprecateTemplateRequest(slug="drain-node", version=1)
    )
    assert again.status == "deprecated"


@pytest.mark.asyncio
async def test_deprecate_against_draft_raises() -> None:
    """Deprecating a still-draft version raises TemplateNotPublishedError."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )

    with pytest.raises(TemplateNotPublishedError):
        await service.deprecate(tenant_id, DeprecateTemplateRequest(slug="drain-node", version=1))


# ---------------------------------------------------------------------------
# list_templates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_templates_filters() -> None:
    """status filter returns only that status; no filter returns latest per slug."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    # alpha: v1 published, v2 draft (fork). beta: v1 draft only.
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="alpha", body=_body())
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="alpha", version=1))
    await service.update_or_fork(
        tenant_id, OPERATOR, EditTemplateRequest(slug="alpha", body=_body())
    )
    await service.create_draft(tenant_id, OPERATOR, DraftTemplateRequest(slug="beta", body=_body()))

    # No filter: latest per slug regardless of status -> alpha v2 (draft), beta v1.
    all_rows = await service.list_templates(tenant_id, ListTemplatesFilter())
    by_slug = {r.slug: r for r in all_rows}
    assert by_slug["alpha"].version == 2
    assert by_slug["alpha"].status == "draft"
    assert by_slug["beta"].version == 1

    # status='published': only alpha v1 (beta has no published version).
    published = await service.list_templates(tenant_id, ListTemplatesFilter(status="published"))
    assert [(r.slug, r.version) for r in published] == [("alpha", 1)]


@pytest.mark.asyncio
async def test_list_templates_tenant_isolation() -> None:
    """Two tenants with the same slug each see only their own row."""
    service = RunbookTemplateService()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await service.create_draft(
        tenant_a, OPERATOR, DraftTemplateRequest(slug="shared", body=_body("A"))
    )
    await service.create_draft(
        tenant_b, OPERATOR, DraftTemplateRequest(slug="shared", body=_body("B"))
    )

    rows_a = await service.list_templates(tenant_a, ListTemplatesFilter())
    rows_b = await service.list_templates(tenant_b, ListTemplatesFilter())

    assert [r.slug for r in rows_a] == ["shared"]
    assert rows_a[0].title == "A"
    assert [r.slug for r in rows_b] == ["shared"]
    assert rows_b[0].title == "B"


# ---------------------------------------------------------------------------
# show_template
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_template_resolves_latest_when_version_none() -> None:
    """With version=None, show resolves the max version."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body())
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))
    await service.update_or_fork(
        tenant_id, OPERATOR, EditTemplateRequest(slug="drain-node", body=_body("v2"))
    )

    resp = await service.show_template(tenant_id, "drain-node")
    assert resp.version == 2
    assert resp.title == "v2"
    assert len(resp.steps) == 1


@pytest.mark.asyncio
async def test_show_template_specific_version() -> None:
    """A pinned version is returned even when a later version exists."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body("v1"))
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))
    await service.update_or_fork(
        tenant_id, OPERATOR, EditTemplateRequest(slug="drain-node", body=_body("v2"))
    )

    resp = await service.show_template(tenant_id, "drain-node", version=1)
    assert resp.version == 1
    assert resp.title == "v1"
    assert resp.status == "published"


@pytest.mark.asyncio
async def test_show_template_missing_raises() -> None:
    """Showing a non-existent slug raises TemplateNotFoundError."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()

    with pytest.raises(TemplateNotFoundError):
        await service.show_template(tenant_id, "ghost")


# ---------------------------------------------------------------------------
# discard (delete-for-drafts leg, #135)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_removes_draft() -> None:
    """A draft discards cleanly; a subsequent show 404s (AC1)."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body("v1"))
    )

    resp = await service.discard(tenant_id, DiscardTemplateRequest(slug="drain-node", version=1))
    assert resp.status == "discarded"
    assert resp.slug == "drain-node"
    assert resp.version == 1

    # The draft is gone: show now raises, and the slug re-drafts as v1.
    with pytest.raises(TemplateNotFoundError):
        await service.show_template(tenant_id, "drain-node", version=1)
    listed = await service.list_templates(tenant_id, ListTemplatesFilter())
    assert [t.slug for t in listed] == []


@pytest.mark.asyncio
async def test_discard_slug_is_reusable_after_discard() -> None:
    """Discarding v1 frees the slug: create_draft succeeds again at v1."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body("v1"))
    )
    await service.discard(tenant_id, DiscardTemplateRequest(slug="drain-node", version=1))

    # No DuplicateDraftError -- the slug has no rows left.
    again = await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body("v1-again"))
    )
    assert again.version == 1


@pytest.mark.asyncio
async def test_discard_published_raises_pointing_at_deprecate() -> None:
    """Discarding a published version raises TemplateNotDraftError naming deprecate (AC2)."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body("v1"))
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))

    with pytest.raises(TemplateNotDraftError, match="deprecate"):
        await service.discard(tenant_id, DiscardTemplateRequest(slug="drain-node", version=1))

    # The published row is untouched by the refused discard.
    assert (await service.show_template(tenant_id, "drain-node", version=1)).status == "published"


@pytest.mark.asyncio
async def test_discard_deprecated_raises() -> None:
    """Discarding a deprecated version is refused (retired, not discardable)."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()
    await service.create_draft(
        tenant_id, OPERATOR, DraftTemplateRequest(slug="drain-node", body=_body("v1"))
    )
    await service.publish(tenant_id, PublishTemplateRequest(slug="drain-node", version=1))
    await service.deprecate(tenant_id, DeprecateTemplateRequest(slug="drain-node", version=1))

    with pytest.raises(TemplateNotDraftError):
        await service.discard(tenant_id, DiscardTemplateRequest(slug="drain-node", version=1))


@pytest.mark.asyncio
async def test_discard_missing_raises() -> None:
    """Discarding a non-existent (slug, version) raises TemplateNotFoundError."""
    service = RunbookTemplateService()
    tenant_id = uuid.uuid4()

    with pytest.raises(TemplateNotFoundError):
        await service.discard(tenant_id, DiscardTemplateRequest(slug="ghost", version=1))
