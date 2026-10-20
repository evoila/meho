# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Install-time programs of the built-in core.

Each submodule is the entrypoint of one Helm hook Job that the chart runs
from the backplane image, for example ``python -m meho_backplane.core.secrets``.
See docs/codebase/core-secrets.md.
"""
