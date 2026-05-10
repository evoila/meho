# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho-backplane — MEHO governance-layer backplane.

Public package marker. The HTTP entrypoint lives in
:mod:`meho_backplane.main`. Subsequent G2.1 Tasks (#19, #20) add health,
version, and observability surfaces; G2.2 / G2.3 add federation and
persistence on top of the same chassis.
"""

__version__ = "0.1.0-dev"
__all__ = ["__version__"]
