# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Holodeck connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # #2908 -- the HoloRouter patch op. Its patch wraps the BroadcomBuildToken
    # *read* and its fixed patch strings reference ``$env:brcm_build_token``
    # by name, never a value; the op takes no params today. The class is
    # defence-in-depth (the ``rke2.node.config.update`` precedent): should a
    # future param-shape change ever place a token in params, the broadcast
    # collapses to aggregate-only rather than shipping it on the feed. The
    # sibling ``holodeck.config.apply`` carries only non-secret config and
    # Vault *refs*, so it keeps its param-echo preview and is not declared.
    credential_write=("holodeck.router.patch",),
)
