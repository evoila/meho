# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Harbor typed write ``harbor.artifact.copy`` -- copy an image inside Harbor, with approval.

An agent copies an image from one Harbor project into another (for example from
a proxy-cache project into a private project) through MEHO, so the copy waits
for approval and lands in the audit log. Harbor does the copy inside the
registry, so no image data is uploaded.

The operation is safe to run twice. Before it copies, the handler reads the
source to get its digest, then reads the target by that digest. When the target
already holds that digest (under any tag, or none), it reports ``copied: false``
and changes nothing. The copy names the source by that digest, so a tag that
moves in the meantime cannot change what is copied. Harbor copies the tags of
the source image along with it. A failed read raises; it never looks like
"not there" unless Harbor answered ``404``.

The handler body lives here and
:meth:`~meho_backplane.connectors.harbor.connector.HarborConnector.artifact_copy`
is a thin shim, so the dispatcher can find the callable from its stored
``module.ClassName.method`` path. The registrar is queued in the package
``__init__`` next to the other Harbor registrars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.harbor._paths import (
    _ARTIFACT_PATH,
    _ARTIFACTS_PATH,
    encode_segment,
    fill_path,
)
from meho_backplane.connectors.harbor.ops import _HARBOR_ARTIFACTS_WHEN_TO_USE

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.connectors.harbor.session import HarborTargetLike
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["harbor_artifact_copy_impl", "register_harbor_artifact_copy_operation"]

_ARTIFACT_COPY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Target Harbor project that receives the image.",
        },
        "repository": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Repository name inside the target project, without the project.",
        },
        "from": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[^/\\s]+/[^\\s@]+(@[^\\s@]+|:[^\\s@:/]+)$",
            "description": (
                "The source image in the same Harbor: <project>/<repository>@<digest> "
                "or <project>/<repository>:<tag>."
            ),
        },
    },
    "required": ["project", "repository", "from"],
    "additionalProperties": False,
    "examples": [
        {
            "project": "<target-project>",
            "repository": "<repository>",
            "from": "<source-project>/<repository>@<digest>",
        },
        {
            "project": "<target-project>",
            "repository": "<repository>",
            "from": "<source-project>/<repository>:<tag>",
        },
    ],
}

_ARTIFACT_COPY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "repository": {"type": "string"},
        "from": {"type": "string", "description": "The source image as it was asked for."},
        "digest": {
            "type": "string",
            "description": "Digest of the source image. This is the image that is copied.",
        },
        "copied": {
            "type": "boolean",
            "description": (
                "True when this call copied the image. False when the target already held it."
            ),
        },
        "read_back": {
            "type": "string",
            "description": "The operation that reads the image back.",
        },
    },
    "required": ["project", "repository", "from", "digest", "copied", "read_back"],
}

_ARTIFACT_COPY_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Copy an image from one Harbor project into another project of the same Harbor, "
        "for example from a proxy-cache project into a private project. Use this instead "
        "of copying the image outside MEHO."
    ),
    "preconditions": (
        "The source must be in the same Harbor. Check it with harbor.artifact.list. "
        "The call waits for a second person to approve."
    ),
    "parameter_hints": {
        "project": "The target project. It must already exist.",
        "repository": "The repository name in the target project.",
        "from": (
            "<project>/<repository>@<digest>, or <project>/<repository>:<tag>. A tag is looked "
            "up once, and the image it points to is copied with all its tags."
        ),
    },
    "output_shape": (
        "{project, repository, from, digest, copied, read_back}. digest is the image that "
        "was copied. copied is true when this call copied the image. When the target already "
        "holds that digest, under any tag, copied is false and nothing changed."
    ),
    "next_step": (
        "Call harbor.artifact.list for the target project and repository and check that "
        "the same digest is there."
    ),
}


def _split_from(source: str) -> tuple[str, str, str, bool]:
    """Split ``<project>/<repository>@<digest>`` or ``...:<tag>``.

    Returns ``(project, repository, reference, is_digest)``.
    """
    if "@" in source:
        name, reference = source.rsplit("@", 1)
        is_digest = True
    else:
        name, _, reference = source.rpartition(":")
        is_digest = False
    project, _, repository = name.partition("/")
    if not project or not repository or not reference:
        raise ValueError(f"from must be <project>/<repository>@<digest> or :<tag>, got {source!r}")
    return project, repository, reference, is_digest


def _artifact_path(project: str, repository: str, reference: str) -> str:
    return fill_path(
        _ARTIFACT_PATH,
        {
            "project_name": encode_segment(project),
            "repository_name": encode_segment(repository),
            "reference": encode_segment(reference),
        },
    )


async def _read_digest(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    path: str,
) -> str | None:
    """The digest of the artifact at *path*, or ``None`` when Harbor answers 404."""
    try:
        artifact = await connector._get_json(target, path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.NOT_FOUND:
            return None
        raise
    digest = artifact.get("digest") if isinstance(artifact, dict) else None
    if not isinstance(digest, str) or not digest:
        raise ValueError(f"Harbor answered without a digest for {path}")
    return digest


async def harbor_artifact_copy_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Copy the image with ``POST .../artifacts?from=...``; a held image is a no-op."""
    project = str(params["project"])
    repository = str(params["repository"])
    source = str(params["from"])
    src_project, src_repository, reference, is_digest = _split_from(source)

    if is_digest:
        digest = reference
    else:
        src_path = _artifact_path(src_project, src_repository, reference)
        found = await _read_digest(connector, operator, target, src_path)
        if found is None:
            raise LookupError(f"source image {source} was not found")
        digest = found

    # Check and copy by digest: a tag can move between the reads and the copy,
    # and the target can hold the image under another tag. Harbor copies the
    # tags of the source image along with it.
    held = await _read_digest(
        connector, operator, target, _artifact_path(project, repository, digest)
    )
    copied = held is None
    if copied:
        path = fill_path(
            _ARTIFACTS_PATH,
            {
                "project_name": encode_segment(project),
                "repository_name": encode_segment(repository),
            },
        )
        pinned = f"{src_project}/{src_repository}@{digest}"
        await connector._post_json(target, path, operator=operator, params={"from": pinned})
    return {
        "project": project,
        "repository": repository,
        "from": source,
        "digest": digest,
        "copied": copied,
        "read_back": "harbor.artifact.list",
    }


async def register_harbor_artifact_copy_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``harbor.artifact.copy`` into ``endpoint_descriptor``.

    Called once per process from the FastAPI lifespan through
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    ``embedding_service`` is the test seam the other Harbor registrars have.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.artifact.copy",
        handler=HarborConnector.artifact_copy,
        summary="Copy an image into a Harbor project inside Harbor, after approval.",
        description=(
            "Copies an image from another project of the same Harbor, inside the registry. "
            "Nothing is uploaded. Safe to run twice: when the target already holds the "
            "image, copied is false. Waits for approval. Read it back with harbor.artifact.list."
        ),
        parameter_schema=_ARTIFACT_COPY_PARAMETER_SCHEMA,
        response_schema=_ARTIFACT_COPY_RESPONSE_SCHEMA,
        group_key="harbor-artifacts",
        when_to_use=_HARBOR_ARTIFACTS_WHEN_TO_USE,
        tags=["write", "harbor", "artifact"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_ARTIFACT_COPY_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
