# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The PostgreSQL image the database tests start (#4039).

It is the chart's own built-in PostgreSQL image (``corePostgres.image`` in
``deploy/charts/meho/values.yaml``, pinned by digest), so the tests run
against the PostgreSQL the chart ships. ``MEHO_TEST_PGVECTOR_IMAGE``
overrides it, for example to pull through a registry mirror; CI does that
with the same digest.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

import yaml

_CHART_VALUES = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho" / "values.yaml"


@cache
def chart_postgres_image() -> str:
    """``repository@digest`` of the chart's built-in PostgreSQL image."""
    image = yaml.safe_load(_CHART_VALUES.read_text())["corePostgres"]["image"]
    return f"{image['repository']}@{image['digest']}"


def pgvector_test_image() -> str:
    """The image to start: ``MEHO_TEST_PGVECTOR_IMAGE``, else the chart's."""
    return os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", chart_postgres_image())
