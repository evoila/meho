# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The marks the Proxmox connector declares for its own operations.

See :mod:`meho_backplane.op_classes`.
"""

from meho_backplane.op_classes import declare_credential_capable

# The raw write can reach any path of the Proxmox API, also the ones that
# set a password or make an API token. So every call waits for a person,
# and no standing grant, agent grant or run approval covers it.
declare_credential_capable(("proxmox.api.write",))
