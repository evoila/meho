# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the SDDC Manager connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules. An ingested op id (``GET:/v1/...``) matches on
its own, so one entry covers the path on every ingested connector that
serves it.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    credential_read=(
        # SDDC Manager is the system of record for nested-infra credentials;
        # the typed ``GET /v1/credentials`` read returns live account secrets
        # (#2306). Audit and broadcast collapse to aggregate-only, on top of
        # the op's approval gate and the connector-boundary secret scrub.
        "sddc.credential.list",
        # #4617 -- the ingested reads whose answer holds credentials. Same
        # posture as ``sddc.credential.list``: the catalog row ``sddc`` /
        # ``9.0`` raises them to caution with approval, and this class scrubs
        # the answer and collapses the broadcast.
        # The credential store and its password-rotation tasks.
        "GET:/v1/credentials",
        "GET:/v1/credentials/{id}",
        "GET:/v1/credentials/tasks",
        "GET:/v1/credentials/tasks/{id}",
        "GET:/v1/credentials/tasks/{id}/subtasks/{subtaskId}",
        "GET:/v1/credentials/tasks/{id}/resource-credentials",
        # Depot, backup and proxy settings carry account passwords.
        "GET:/v1/system/settings/depot",
        "GET:/v1/system/backup-configuration",
        "GET:/v1/system/backup-configuration/backup-locations",
        "GET:/v1/system/proxy-configuration",
        # Host prechecks echo the host password; VASA providers and their
        # users carry passwords; a management-component task spec carries
        # the admin and root passwords.
        "GET:/v1/hosts/prechecks/{id}",
        "GET:/v1/vasa-providers",
        "GET:/v1/vasa-providers/{id}",
        "GET:/v1/vasa-providers/{id}/users",
        "GET:/v1/vcf-management-components/tasks/{taskId}/spec",
        # Users carry an API key; identity providers carry the sync client
        # bearer token.
        "GET:/v1/users",
        "GET:/v1/users/ui",
        "GET:/v1/users/local/admin",
        "GET:/v1/identity-providers",
        "GET:/v1/identity-providers/{id}",
        # The VCF Installer bring-up spec carries the host, NSX, vCenter and
        # SDDC Manager passwords.
        "GET:/v1/sddcs/{id}/spec",
    ),
    # #3497 -- the host-commission write pair. Both take the same
    # ``{"spec": [<HostCommissionSpec>, ...]}`` params, and each spec item
    # declares a ``password`` (the ESXi host root password). ``.validate``
    # is not a write suffix (it would fall through to ``other``) and
    # ``.commission`` would classify plain ``write``; either would ship the
    # host passwords on the feed. The sibling domain writes carry their
    # passwords inside an open ``additionalProperties`` spec, which the
    # runtime scrub catches.
    credential_write=("sddc.host.validate", "sddc.host.commission"),
)
