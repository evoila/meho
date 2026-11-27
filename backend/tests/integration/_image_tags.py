# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the major version from a container image reference."""

from __future__ import annotations

import re
from typing import NamedTuple

import pytest
from _pytest.mark.structures import ParameterSet


def major_of_image(image: str) -> str | None:
    """The major version in the tag of *image*, else ``None``.

    ``postgres:17-alpine`` gives ``17``. The registry address and the digest
    are not part of the tag, so ``registry.example:5000/mongo:8@sha256:...``
    gives ``8``, and ``registry.example:5000/mongo`` gives ``None``.
    """
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    _repository, sep, tag = name.partition(":")
    match = re.match(r"\d+", tag) if sep else None
    return match.group(0) if match else None


class ImageRun(NamedTuple):
    """One image a module runs against, with the public name of the run."""

    label: str
    image: str


def image_param(label: str, image: str) -> ParameterSet:
    """A fixture parameter that runs *image* under the fixed test id *label*.

    The test id and the skip and wait messages of the fixture name only
    *label*. CI names its images through the image proxy, and the test report
    that CI uploads holds every test id and every skip message.
    """
    return pytest.param(ImageRun(label, image), id=label)
