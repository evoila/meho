# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Azure connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The link create asks Azure for a user delegation key (a secret, in an
    # XML answer the Azure trace mask cannot read) and signs a download link
    # (a secret too) that goes to Vault. As credential_mint the flight
    # recorder keeps no bodies of the run, the broadcast is aggregate-only
    # and vendor text is withheld.
    credential_mint=("azure.storage.blob.link.create",),
)
