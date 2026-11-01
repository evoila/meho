# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the vSphere connectors declare for their own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules. An ingested op id (``GET:/vcenter/...``) matches
on its own, so one entry covers the path on every ingested connector that
serves it.
"""

from typing import Final

from meho_backplane.op_classes import declare_op_classes

#: Generic vCenter REST reads whose answer holds credentials, as the
#: vendor ``vcenter.yaml`` writes the paths: proxy settings, content
#: libraries, report download tokens, deployment and migration specs,
#: guest customization specs, identity providers and broker clients, and
#: Supervisor image registries. The vCenter 8.0 vendor file names some
#: path parameters differently (``{library_id}``,
#: ``{container_image_registry}``), so those reads appear in both
#: spellings. Each path is declared twice, with and without the ``/api``
#: base, because the parser folds a relative ``servers.url`` into every
#: path.
_VCENTER_CREDENTIAL_READ_PATHS: Final[tuple[str, ...]] = (
    "/appliance/networking/proxy",
    "/appliance/networking/proxy/{protocol}",
    "/content/library/{libraryId}",
    "/content/local-library/{libraryId}",
    "/content/subscribed-library/{libraryId}",
    "/content/library/{library_id}",
    "/content/local-library/{library_id}",
    "/content/subscribed-library/{library_id}",
    "/esx/hcl/reports/{report}",
    "/vcenter/lcm/reports/{report}",
    "/vcenter/deployment/install",
    "/vcenter/deployment/upgrade",
    "/vcenter/deployment/migrate",
    "/vcenter/lcm/deployment/migration-upgrade",
    "/vcenter/guest/customization-specs/{name}",
    "/vcenter/identity/providers/{provider}",
    "/vcenter/identity/broker/tenants/{tenant}/admin-client",
    "/vcenter/identity/broker/tenants/operator-client",
    "/vcenter/namespace-management/supervisors/{supervisor}/container-image-registries",
    "/vcenter/namespace-management/supervisors/{supervisor}"
    "/container-image-registries/{containerImageRegistry}",
    "/vcenter/namespace-management/supervisors/{supervisor}"
    "/container-image-registries/{container_image_registry}",
)

declare_op_classes(
    credential_read=(
        # The catalog rows ``vmware`` / ``9.0`` and ``8.0`` raise these reads
        # to caution with approval; this class scrubs the answer and
        # collapses the broadcast.
        *(f"GET:{path}" for path in _VCENTER_CREDENTIAL_READ_PATHS),
        *(f"GET:/api{path}" for path in _VCENTER_CREDENTIAL_READ_PATHS),
        # The older-API (VI-JSON) host config (IPMI password) and precheck
        # remediation result (host profile passwords).
        "GET:/HostSystem/{moId}/config",
        "GET:/HostSystem/{moId}/precheckRemediationResult",
    ),
    credential_write=(
        # #2892 -- the GOSC customization-spec create composite. Its params
        # carry Windows sysprep credentials (``windows_admin_password`` /
        # ``windows_product_key`` / ``windows_domain_admin_password``). The
        # class makes it explicit instead of relying on the runtime key-name
        # scrub for a plain ``write``; the park-time preview echoes identity
        # fields only. The sibling ``vmware.composite.vm.customize`` carries
        # only a spec-name reference and is not declared.
        "vmware.composite.guest.customization_spec.create",
        # #3255 -- the governed in-guest program run. Its ``arguments``
        # string (a command line) and ``env`` values can hold credential
        # material that the key-name / Tier-1 scrub misses; a plain
        # ``write`` class (the ``.run`` suffix) would ship the command line
        # in full. The park-time preview echoes only program identity,
        # argument byte size and env-var names.
        "vmware.composite.vm.guest.program.run",
        # #3298 -- the governed in-guest file write. Its ``content`` param
        # is the file body; the scrub lets it through, so a plain ``write``
        # class would ship the whole file body on the feed. The park-time
        # preview echoes only path, byte size and overwrite intent.
        "vmware.composite.vm.guest.file.write",
        # #3717 -- the guest-ops READ composites that log into the guest.
        # Each resolves the guest OS credential from secret_ref and sends it
        # as the vim ``NamePasswordAuthentication`` block in the request
        # BODY (composites/_guest.py::_guest_auth). The secret is not an op
        # param and not a declared schema property, so neither the
        # classifier-coverage test nor the flight recorder's pattern nets can
        # see it; a ``read`` / ``other`` class would leave the flight
        # recorder recording the guest password. This class sets
        # body_recorded=false for their spans and collapses their broadcast.
        # ``vmware.composite.vm.guest.net.show`` is deliberately absent: it
        # reads Tools-reported VM state with no in-guest login.
        "vmware.composite.vm.guest.process.list",
        "vmware.composite.vm.guest.env.read",
        "vmware.composite.vm.guest.file.read",
        # #3495 -- the SUBSCRIBED content-library create composite. Its
        # ``password`` param (BASIC-auth to the publisher) is a secret-named
        # key, but ``username`` / ``ssl_thumbprint`` are not, so the whole
        # op collapses to aggregate-only rather than relying on per-key
        # scrubbing. The sibling reads and the sync write carry no credential
        # and are not declared.
        "vmware.composite.content_library.subscribed.create",
        # The vCenter appliance local-account password change. Its params
        # carry only a Vault path (``password_secret_ref``); the new password
        # is sent only in the request BODY. Its sibling
        # ``vmware.appliance.local_account.policy.set`` sends only expiry
        # numbers and flags, so it is not declared.
        "vmware.appliance.local_account.password.set",
        # The two steps of a new vCenter install that send a password (#4394):
        # the appliance import (the root password) and the setup start (the
        # SSO password). The params carry only Vault paths; the passwords go
        # only into the vCenter request bodies.
        "vmware.composite.vcenter.appliance.deploy",
        "vmware.composite.vcenter.install",
        # Adding an ESXi host to vCenter (#4349): the params carry only a
        # Vault path; the host password goes only into the ``AddHost_Task``
        # / ``AddStandaloneHost_Task`` request body.
        "vmware.composite.host.add",
    ),
    # A hand-written ``safe`` operation that is not a plain read: it makes
    # the target rescan a datastore. Without this class it would get
    # ``read`` from ``resolve_operation_op_class``.
    write=("vmware.composite.datastore.refresh",),
)
