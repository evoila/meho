# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The image tag helper the database container tests use. Needs no Docker."""

from __future__ import annotations

import pytest

from tests.integration._image_tags import image_without_registry, major_of_image


@pytest.mark.parametrize(
    ("image", "major"),
    [
        ("postgres:17-alpine", "17"),
        ("mongo:8", "8"),
        ("registry.example:5000/postgres:18-alpine", "18"),
        ("registry.example:5000/library/mongo:7.0.14", "7"),
        ("mongo:8@sha256:0123abcd", "8"),
        ("registry.example:5000/mongo", None),
        ("registry.example:5000/mongo@sha256:0123abcd", None),
        ("postgres:latest", None),
        ("postgres", None),
    ],
)
def test_major_of_image(image: str, major: str | None) -> None:
    assert major_of_image(image) == major


@pytest.mark.parametrize(
    ("image", "name"),
    [
        ("registry.example:5000/library/mongo:7", "mongo:7"),
        ("mongo:7", "mongo:7"),
        ("registry.example:5000/mongo@sha256:0123abcd", "mongo@sha256:0123abcd"),
    ],
)
def test_image_without_registry(image: str, name: str) -> None:
    assert image_without_registry(image) == name
