# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO-authored ExecutionProfile documents shipped as package data (#1964 T1 #1975).

A catalog row (:class:`~meho_backplane.operations.ingest.catalog.ConnectorSpecEntry`)
that pairs a shipped spec with a reviewed declarative auth/pagination
profile carries a ``profile_resource`` naming a ``.yaml`` file here. The
profile fills the one hand-coded slot
(:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`'s
``auth_headers``) so an ingested REST connector dispatches without bespoke
Python (Initiative #1965).

The directory lives inside the package tree, so hatch's ``packages`` glob
collects its data files into the wheel; ``backend/pyproject.toml``'s
``[tool.hatch.build.targets.wheel].artifacts`` lists the ``.yaml`` /
``.json`` globs to make that non-``.py`` inclusion explicit.
Every shipped profile is dry-run-parsed + scheme-validated at startup by
:func:`~meho_backplane.operations.ingest.catalog.validate_shipped_artifacts`
(the same :func:`~meho_backplane.connectors.profile.validate_execution_profile`
boot guard), so a malformed profile or a reserved/unknown auth scheme
crashes boot.

T1 (#1975) ships the mechanism plus ``_fixture_minimal.yaml`` — a tiny
valid profile exercising the boot-time validator. T2 (#1976) authors the
real per-product profiles.
"""
