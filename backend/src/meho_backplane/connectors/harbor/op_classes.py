# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Harbor connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The robot create returns the freshly-minted robot secret in its
    # response. Declared before the ``.create`` suffix would make it plain
    # ``write``, so the broadcast collapses to aggregate-only and the secret
    # never reaches the SSE stream or a Slack mirror channel.
    credential_mint=("harbor.robot.create",),
)
