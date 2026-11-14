# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the major version from a container image reference."""

from __future__ import annotations

import re


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
