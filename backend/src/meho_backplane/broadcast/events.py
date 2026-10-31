# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# code-quality-allow: pre-existing legacy debt not introduced by this task.
# The module was already >1000 lines (the wire-shape contract + the full op
# sensitivity classifier + redaction rules) before this task; #2908 only adds
# two op-ids to the fixed `_CREDENTIAL_WRITE_OPS` frozenset. Splitting the
# classifier out of the schema module is a behaviour-preserving refactor out
# of scope for a two-line allowlist pin and would balloon the review surface.

"""Broadcast event schema + PII classifier (G6.1-T2).

The publish-on-write hook (T3, #309) builds one :class:`BroadcastEvent`
per audited operation and ``XADD``\\ s it to ``meho:feed:{tenant_id}``.
T4 (#310) reads back from the same stream and serves it via SSE; T6
(#312) wraps the stream in an MCP resource. This module ships the
wire-shape contract and the sensitivity classifier that every
downstream consumer relies on.

PII discipline lives in :func:`classify_op` + :func:`redact_payload`.
The classifier is policy-locked by decision #3 in
``docs/decisions/locked-decisions.md`` — credential reads and audit-query
responses broadcast aggregate-only by default; everything else
broadcasts in full. Per-op opt-in to flip a sensitive class to full
detail is a G6.3 surface; T2 ships the conservative default.

Why a classifier rather than a per-op annotation:

1. Sensitivity is mostly op-class-shaped. ``vault.kv.read`` is no more
   sensitive than ``vault.kv.list``; ``k8s.pod.list`` is no more
   sensitive than ``k8s.node.list``. A per-op flag would multiply
   the contract surface for no policy gain.
2. The classifier is one auditable function. A reviewer can read it in
   one sitting and verify policy compliance; scattered annotations on
   every op would require a registry walk.

Why aggregate-only-by-default for the sensitive classes:

* ``credential_read`` — even logging that ``vault.kv.read`` returned OK
  reveals which secret an operator touched. Path strings frequently
  carry environment names, target hostnames, or service identifiers
  that no SSE feed subscriber needs and that no Slack mirror channel
  should retain. Aggregate-only collapses every credential read into
  ``{op_class, result_status}`` — enough for "someone touched a
  credential at 14:23", not enough to reconstruct what.
* ``audit_query`` — the request filter is the most damaging thing to
  broadcast: it encodes whoever the querying operator was investigating
  and on what hunch. The response payload also carries the raw audit
  rows the query matched, which inherits every other op's sensitivity.
  Broadcasting only ``{op_class, result_status, row_count}`` keeps the
  team-coordination signal ("X just queried audit") without leaking
  the investigation target or the matched evidence.

References
----------

* Decision #3 — ``docs/decisions/locked-decisions.md``.
* MCP audit shape (G0.5-T5, the in-tree precedent for similar
  redaction discipline) —
  :func:`meho_backplane.mcp.audit.write_mcp_audit_row`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy

__all__ = [
    "BroadcastEvent",
    "classify_op",
    "is_secret_param_name",
    "redact_payload",
    "resolve_op_class",
    "resolve_operation_op_class",
    "scrub_broadcast_params",
    "scrub_secret_named_values",
]


#: Op-ids that classify as ``credential_read``. Extensible — every
#: future credential-access verb (e.g. ``vault.transit.decrypt``,
#: ``secretsmanager.get``, ``onepassword.read``) gets added here when
#: it lands. Membership is the canonical signal; ``vault.kv.``-prefix
#: matching would over-match a hypothetical future ``vault.kv.stats``
#: which doesn't read secret content.
_CREDENTIAL_READ_OPS: Final[frozenset[str]] = frozenset(
    {
        "vault.kv.read",
        "vault.kv.list",
        # SDDC Manager is the system of record for nested-infra credentials;
        # its typed ``GET /v1/credentials`` read returns live account secrets
        # (#2306). Classified credential_read so audit + broadcast payloads
        # collapse to aggregate-only, on top of the op's requires_approval
        # gate and the connector-boundary secret scrub.
        "sddc.credential.list",
        # The governed guest-cluster kubeconfig read (#3496). Reads a Secret's
        # data value on a k8s / vSphere-Supervisor target and stages it to a
        # tenant-scoped Vault secret_ref (returning only the ref, never the
        # value). Classified credential_read — same posture as the SDDC read —
        # so audit + broadcast collapse to aggregate-only on top of its
        # requires_approval gate; the result is value-free by construction.
        "k8s.secret.read_to_ref",
        # #5230 -- the two compare reads. The backplane reads the secret
        # values itself to compare them and returns only same/different, so
        # the answer is value-free by construction. The pin makes the flight
        # recorder drop the vendor bodies of these reads (they carry the
        # values) and collapses the broadcast to aggregate-only.
        "vault.kv.compare",
        "k8s.secret.compare_to_ref",
        # #4617 -- the ingested SDDC Manager reads whose answer holds
        # credentials. Same posture as ``sddc.credential.list``: the catalog
        # row ``sddc`` / ``9.0`` raises them to caution with approval, and
        # this entry scrubs the answer and collapses the broadcast. An op id
        # matches on its own, so one entry covers the path on every
        # ingested connector that serves it.
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
        # Generic vCenter reads whose answer holds credentials. The catalog
        # rows ``vmware`` / ``9.0`` and ``8.0`` raise them to caution with
        # approval; this entry scrubs the answer and collapses the broadcast.
        # Proxy settings, content libraries, report download tokens,
        # deployment and migration specs, guest customization specs, identity
        # providers and broker clients, and Supervisor image registries.
        "GET:/appliance/networking/proxy",
        "GET:/appliance/networking/proxy/{protocol}",
        "GET:/content/library/{libraryId}",
        "GET:/content/local-library/{libraryId}",
        "GET:/content/subscribed-library/{libraryId}",
        "GET:/esx/hcl/reports/{report}",
        "GET:/vcenter/lcm/reports/{report}",
        "GET:/vcenter/deployment/install",
        "GET:/vcenter/deployment/upgrade",
        "GET:/vcenter/deployment/migrate",
        "GET:/vcenter/lcm/deployment/migration-upgrade",
        "GET:/vcenter/guest/customization-specs/{name}",
        "GET:/vcenter/identity/providers/{provider}",
        "GET:/vcenter/identity/broker/tenants/{tenant}/admin-client",
        "GET:/vcenter/identity/broker/tenants/operator-client",
        "GET:/vcenter/namespace-management/supervisors/{supervisor}/container-image-registries",
        (
            "GET:/vcenter/namespace-management/supervisors/{supervisor}"
            "/container-image-registries/{containerImageRegistry}"
        ),
        # The older-API (VI-JSON) host config (IPMI password) and precheck
        # remediation result (host profile passwords).
        "GET:/HostSystem/{moId}/config",
        "GET:/HostSystem/{moId}/precheckRemediationResult",
        # #4617 -- a BIND config file can hold TSIG and rndc key secrets.
        # The answer is one text field, so the Tier-1 ``bind_key_secret``
        # rule does the redaction; this entry collapses the broadcast and
        # keeps the flight recorder from storing the body.
        "bind9.config.show",
    }
)

#: Op-ids that classify as ``credential_mint``. Like
#: :data:`_CREDENTIAL_READ_OPS`, the explicit allowlist comes before
#: the write-suffix check so ``harbor.robot.create`` (which ends with
#: ``.create``) isn't misclassified as plain ``write``. Every op that
#: returns a freshly-minted secret credential in its response payload
#: belongs here — the broadcast collapses to aggregate-only so the
#: secret never reaches the SSE stream or any Slack mirror channel.
_CREDENTIAL_MINT_OPS: Final[frozenset[str]] = frozenset(
    {
        "harbor.robot.create",
        # G11.7-T1 #1401 — Vault auth ops whose *response* carries a
        # freshly-minted secret. ``vault.token.create`` returns a client
        # token; ``vault.auth.approle.generate_secret_id`` returns a
        # SecretID. Both must collapse to aggregate-only so the minted
        # credential never reaches the SSE stream or a Slack mirror. The
        # ``OperationResult`` returned to the caller still carries it.
        "vault.token.create",
        "vault.auth.approle.generate_secret_id",
        # G-Node/RKE2-T2 #2429 — the RKE2 server-token rotate mints a new
        # join token server-side. The handler never returns the token value
        # (only a Vault pointer), so nothing sensitive is in the response —
        # but ``.rotate`` would otherwise classify ``other`` and broadcast
        # full params/detail. Pinning to ``credential_mint`` collapses the
        # broadcast to aggregate-only as defence-in-depth for the op class.
        "rke2.token.rotate",
        # #3890 — the VCF Automation API-token mint. It logs in as the org
        # user and mints a long-lived refresh token; the handler writes the
        # token straight to Vault and returns only a Vault pointer + SHA-256,
        # so nothing sensitive is in the response. The pin (``rke2.token.rotate``
        # precedent) collapses the broadcast to aggregate-only and makes the
        # flight recorder drop its bodies (the OAuth form carries the session
        # JWT as the ``assertion``; the token response carries the token).
        "vcfa.provider.api_token.create",
        # ``vault.token.create_and_store`` makes a Vault token and writes it
        # straight to a Vault KV path or a Kubernetes Secret; the handler
        # returns only the accessor, expiry, policies and destination. Same
        # posture as ``vcfa.provider.api_token.create``: aggregate-only
        # broadcast, and the flight recorder keeps no bodies (the token rides
        # in the Vault create response and in the write request).
        "vault.token.create_and_store",
        # The handoff export returns a one-time handoff token: not the value,
        # but it can fetch the value once. Aggregate-only keeps it off the feed.
        "secret.handoff.export",
    }
)

#: Op-ids that classify as ``credential_write`` (G11.7-T1 #1401). Unlike
#: :data:`_CREDENTIAL_MINT_OPS` (secret in the *response*), these ops
#: carry the secret in their *request params* — the broadcast publisher
#: ships ``params`` (request-side), so a plain ``write`` classification
#: would leak the written credential to every operator on the feed.
#: Collapsing them to aggregate-only keeps the team-coordination signal
#: ("someone wrote a credential at 14:23") without the secret material.
#: The explicit allowlist comes before the ``.write`` / ``.create`` /
#: ``.update`` suffix branch so these win over the plain ``write`` class.
#:
#: * ``vault.auth.userpass.write`` / ``vault.auth.userpass.update_password``
#:   — the userpass password is in ``params``.
#: * ``vault.kv.put`` — the KV-v2 secret ``data`` is in ``params`` (this
#:   op shipped pre-G11.7 classified as plain ``write``, which broadcast
#:   the written secret in full; reclassifying it here closes that latent
#:   leak — see ``docs/codebase/connectors-vault.md``).
#: * ``vault.kv.patch`` — same posture as ``vault.kv.put``: the merged
#:   fields ride in ``params`` (G3.15-T1 #1409). Without the explicit
#:   pin the ``.patch`` write-suffix would classify it plain ``write``
#:   and broadcast the partial secret in full.
#: * ``k8s.secret.create`` — the Secret ``data`` / ``stringData`` is in
#:   ``params``.
#: * ``k8s.job.create`` — the Job ``spec`` carries a pod template whose
#:   inline ``env`` entries can hold credential material in ``params``
#:   (G3.14-T1 #1403).
#: * ``vmware.composite.vm.guest.{process.list,env.read,file.read,
#:   file.write,program.run}`` (#3717) — the vSphere guest-ops composites
#:   that log into the guest. Unlike every other member here, their secret
#:   is **not** in ``params``: the guest OS password rides the downstream
#:   vim ``NamePasswordAuthentication`` block in the *request body*,
#:   resolved from ``secret_ref`` (``composites/_guest.py::_guest_auth``).
#:   The pin is what the flight recorder's
#:   :func:`~meho_backplane.redaction.flight_recorder.classify_body_exclusion`
#:   delegates to, so these spans never record that body; the broadcast
#:   collapse is defence-in-depth here (their params are non-secret). The
#:   sibling ``vmware.composite.vm.guest.net.show`` is deliberately absent —
#:   it reads Tools-reported VM state with no in-guest login and sends no
#:   credential.
_CREDENTIAL_WRITE_OPS: Final[frozenset[str]] = frozenset(
    {
        "vault.auth.userpass.write",
        "vault.auth.userpass.update_password",
        "vault.kv.put",
        "vault.kv.patch",
        "k8s.secret.create",
        # The values of an existing Secret ride in the same params shape as
        # k8s.secret.create (inline maps or store refs), so the same pin
        # keeps them off the approval row and the broadcast.
        "k8s.secret.update",
        # The handoff import carries the one-time handoff token in params.
        # The pin keeps it off the approval row (stored encrypted) and the feed.
        "secret.handoff.import",
        "k8s.job.create",
        # G3.13-T4 #1406 — Keycloak user-credential write ops. Both source
        # the password from Vault (it is never an inline param), so the
        # request params carry only a Vault *path*. The pin is
        # defence-in-depth: should a future param-shape change ever place
        # credential material in params, the broadcast collapses to
        # aggregate-only rather than shipping it on the feed.
        "keycloak.user.create",
        "keycloak.user.reset_password",
        # The AD password set. Same shape as the Keycloak pair: the password
        # comes from Vault at run time, and params carry only the
        # ``password_ref`` reference. The pin keeps even that off the feed and
        # stores the approval input encrypted, and the flight recorder keeps
        # no body for it.
        "msad.user.set-password",
        # G-Node/RKE2-T3 #2430 — the RKE2 node-config write op. Its ``patch``
        # param carries key -> value edits, and an RKE2 ``config.yaml`` body
        # holds ``token:`` / ``agent-token:`` join credentials, so a plain
        # ``write`` classification (the ``.update`` suffix) would broadcast a
        # written token in full. Pinning it collapses the broadcast to
        # aggregate-only. (The op result itself returns key NAMES only.)
        "rke2.node.config.update",
        # #2892 — the GOSC customization-spec create composite. Its params
        # carry Windows sysprep credentials (``windows_admin_password`` /
        # ``windows_product_key`` / ``windows_domain_admin_password``). The
        # ``password`` fields trip the key-name scrub, but ``product_key``
        # does not (``key`` is neither an anywhere- nor a final-position
        # secret token), so a plain ``write`` classification (the ``.create``
        # suffix) would broadcast the product key in full. Pinning it
        # collapses the whole params dict to aggregate-only (#1503); the
        # park-time preview additionally echoes identity fields only. The
        # sibling ``vmware.composite.vm.customize`` carries only a spec-name
        # reference (no secret) and is not pinned.
        "vmware.composite.guest.customization_spec.create",
        # #2908 — the Holodeck HoloRouter patch op. Its C2 patch wraps the
        # BroadcomBuildToken *read* and its fixed patch strings reference
        # ``$env:brcm_build_token`` by name, never a value; the op takes no
        # params today. The pin is defence-in-depth per the
        # ``rke2.node.config.update`` precedent: should a future param-shape
        # change ever place a token/credential in params, the broadcast
        # collapses to aggregate-only rather than shipping it on the feed. The
        # sibling ``holodeck.config.apply`` carries only non-secret config +
        # Vault *refs* (the vCenter/depot creds are read on the appliance from
        # its staged env, never a param), so it keeps its param-echo preview
        # and is deliberately NOT pinned.
        "holodeck.router.patch",
        # #3255 — the governed in-guest program-execution write. Its
        # ``arguments`` string (a command line) and ``env`` values can hold
        # credential material (``--token=…``, ``API_KEY=…``) in ``params``,
        # and neither is a secret-*named* key nor a recognisable secret
        # *shape*, so the key-name / Tier-1 scrub in ``scrub_broadcast_params``
        # would miss them — a plain ``write`` classification (the ``.run``
        # suffix) would ship the command line in full on the feed. Pinning it
        # collapses the whole params dict to aggregate-only, exactly as
        # ``k8s.job.create``'s inline-env case and the GOSC create above; the
        # park-time preview additionally echoes only program identity +
        # argument byte size + env-var names. Its sibling
        # ``guest.file.write`` (``content`` has the same characteristic) is
        # pinned just below (#3298).
        "vmware.composite.vm.guest.program.run",
        # #3298 — the governed in-guest file write, the sibling of
        # ``program.run`` above. Its ``content`` param is the file body to
        # write; ``content`` is neither a secret-*named* key nor a
        # recognisable secret *shape*, so the key-name / Tier-1 scrub in
        # ``scrub_broadcast_params`` lets it through — a plain ``write``
        # classification (the ``.write`` suffix) would ship the whole file
        # body on the feed. Pinning it collapses the params dict to
        # aggregate-only, exactly as ``program.run`` above and
        # ``rke2.node.config.update``; the park-time preview additionally
        # echoes only path + byte size + overwrite intent, never the content.
        # This was the last guest-ops write riding broadcast unredacted
        # (flagged as the unpinned sibling in the #3255 pin above).
        "vmware.composite.vm.guest.file.write",
        # #3717 — the guest-ops READ composites that log into the guest.
        # process.list / env.read / file.read each resolve the guest OS
        # credential from secret_ref and send it as the vim
        # ``NamePasswordAuthentication`` block in the request BODY
        # (composites/_guest.py::_guest_auth). Unlike the sibling writes above,
        # the secret is NOT an op param and NOT a declared schema property, so
        # neither the classifier-coverage lint nor the flight recorder's
        # shape/pattern nets can see it — a ``read`` / ``other`` classification
        # (``.list`` read-suffix / bare ``.read``) leaves the flight-recorder
        # vendor-call + typed spans recording the guest password in plaintext
        # (redaction/flight_recorder/families.py delegates body exclusion to
        # this classifier). Pinning them ``credential_write`` sets
        # body_recorded=false for their spans and collapses their broadcast to
        # aggregate-only. ``net.show`` is deliberately NOT here: it reads
        # Tools-reported VM state with no in-guest login, so it sends no
        # credential (composites/_guest.py::guest_net_show_composite).
        "vmware.composite.vm.guest.process.list",
        "vmware.composite.vm.guest.env.read",
        "vmware.composite.vm.guest.file.read",
        # #3361 — the governed linux-ssh config-file write. Its ``content``
        # param is the file body to write; ``content`` is neither a
        # secret-*named* key nor a recognisable secret *shape*, so the
        # key-name / Tier-1 scrub in ``scrub_broadcast_params`` lets it
        # through — a plain ``write`` classification (the ``.write`` suffix)
        # would ship the whole file body on the feed. Pinning it collapses the
        # params dict to aggregate-only; the park-time bespoke preview echoes
        # only path + byte size + backup path + validate command, never the
        # content (the ``vmware.composite.vm.guest.file.write`` mold above).
        "linux.file.write",
        # #3361 — the governed linux-ssh script-execution write, the sibling of
        # the guest ``program.run`` above. Its ``arguments`` string (a command
        # line) and ``env`` values can hold credential material
        # (``--token=…``, ``API_KEY=…``) in ``params``, and neither is a
        # secret-*named* key nor a recognisable secret *shape*, so the scrub
        # misses them — and ``.run`` is not even a ``.write`` suffix, so an
        # unpinned classification would fall through to ``other`` and ship the
        # command line in full. Pinning it collapses the params dict to
        # aggregate-only; the park-time bespoke preview echoes only program
        # identity + argument byte size + env-var NAMES, never any value.
        "linux.script.run",
        # #3497 — the governed SDDC Manager host-commission write pair. Both
        # take the same ``{"spec": [<HostCommissionSpec>, ...]}`` params, and
        # each spec item declares a ``password`` (the ESXi host root password
        # POSTed to /v1/hosts/validations resp. /v1/hosts). ``password`` is a
        # secret-*named* key the runtime ``scrub_broadcast_params`` key-scrub
        # would catch, but the classifier-coverage lint (meho-internal #151)
        # requires every op *declaring* a secret-shaped param to be statically
        # pinned to a ``credential_*`` class so its broadcast collapses to
        # aggregate-only, not merely field-redacted. ``.validate`` is not even
        # a ``.write`` suffix (it would fall through to ``other``) and
        # ``.commission`` would classify plain ``write`` — either would ship
        # the host passwords on the feed. Pinning both collapses the params
        # dict to aggregate-only. The sibling domain writes
        # (``sddc.domain.validate`` / ``sddc.domain.create``) carry their
        # passwords inside an open-``additionalProperties`` DomainCreationSpec
        # (no *declared* secret prop), so the lint does not flag them; the
        # runtime scrub catches their nested ``*password*`` keys and collapses
        # the broadcast, exactly the two-layer split that pins ``vault.kv.put``
        # statically while leaving its generic ``data`` container to the scrub.
        "sddc.host.validate",
        "sddc.host.commission",
        # #3495 — the SUBSCRIBED content-library create composite. Its
        # ``password`` param (BASIC-auth to the publisher) IS a secret-named
        # key the scrub would catch, but ``username`` / ``ssl_thumbprint`` are
        # not, and pinning the whole op collapses its params to aggregate-only
        # rather than relying on per-key scrubbing — matching the GOSC-create /
        # guest-ops precedent. The park-time bespoke preview echoes only the
        # non-secret identity fields (name / subscription URL / datastore /
        # auth method / sync mode). The sibling reads (status / items.list) and
        # the sync write carry no credential and are NOT pinned.
        "vmware.composite.content_library.subscribed.create",
        # #3890 — the VCF Automation local org-user create. Like
        # ``keycloak.user.create`` its params carry only a Vault *path*
        # (``password_secret_ref``); the password is read in-process and sent
        # in the ``POST /cloudapi/1.0.0/users`` request BODY. Pinning it sets
        # body_recorded=false for its flight-recorder spans and collapses the
        # broadcast to aggregate-only as defence-in-depth.
        "vcfa.provider.user.create",
        # #3890 — the API-token revoke logs in as the token's owner with the
        # password read from ``password_secret_ref`` (declared secret-shaped
        # params, so the classifier-coverage lint requires a pin; ``.revoke``
        # would otherwise fall through to ``other`` and broadcast in full).
        # Same posture as its ``vcfa.provider.api_token.create`` sibling.
        "vcfa.provider.api_token.revoke",
        # The vCenter appliance local-account password change. Like
        # ``keycloak.user.reset_password``, its params carry only a Vault
        # *path* (``password_secret_ref``); the new password is read
        # in-process and sent only in the ``PATCH
        # /api/appliance/local-accounts/{username}`` request BODY. The pin
        # sets body_recorded=false for its flight-recorder spans, collapses
        # the broadcast to aggregate-only, and keeps a waiting approval's
        # params in the encrypted handoff store. Its sibling
        # ``vmware.appliance.local_account.policy.set`` sends only expiry
        # numbers and flags, so it is NOT pinned.
        "vmware.appliance.local_account.password.set",
        # The VCF Installer proxy change and resource calculation. Like
        # ``vcfa.provider.user.create``, their params carry only Vault paths
        # (``password_secret_ref`` / ``password_refs``); the passwords are read
        # in-process and sent only in the request BODY. The pin keeps those
        # bodies out of traces and collapses the broadcast to aggregate-only.
        "installer.system.proxy.set",
        "installer.sddc.resources.calculate",
        # The two steps of a new vCenter install that send a password (#4394):
        # the appliance import (the root password, ``root_password_secret_ref``)
        # and the setup start (the SSO password,
        # ``sso_admin_password_secret_ref``). The params carry only Vault
        # paths; the passwords go only into the vCenter request bodies. The
        # pin keeps those bodies out of the flight recorder and the params out
        # of the broadcast.
        "vmware.composite.vcenter.appliance.deploy",
        "vmware.composite.vcenter.install",
        # The Keycloak login test. Its params carry only a Vault *path*
        # (``password_secret_ref``); the password is read in-process and sent
        # only in the token request BODY, and the token answer carries an
        # access and a refresh token. The pin sets body_recorded=false for its
        # flight-recorder spans, collapses the broadcast to aggregate-only and
        # keeps a waiting approval's params in the encrypted handoff store.
        "keycloak.user.check_login",
        # The three Cloud Builder changes that send a bring-up spec. Their
        # params carry only Vault paths (``secrets``); the passwords are read
        # in-process and go only into the Cloud Builder request BODY. The pin
        # keeps those bodies out of the flight recorder and the params out of
        # the broadcast.
        "installer.cb.validation.start",
        "installer.cb.sddc.start",
        "installer.cb.sddc.retry",
    }
)

#: Op-ids that classify as ``checks`` — the checks subsystem's own
#: state-change events: the Dashboard rollup edge (#2720) and the
#: evaluation-loop watchdog's stall/recovery pair (#2763). The checks
#: plane stopped publishing them to Broadcast in #3758 (checks keep their
#: own notification pipeline); the class stays so older entries still
#: classify the same way.
#:
#: An explicit allowlist, **not** a ``checks.`` prefix branch. The
#: ``/api/v1/checks/*`` gateway routes already own three shipped op-ids
#: under that prefix (``checks.assignment.put`` /
#: ``checks.assignment.get`` / ``checks.results.post``,
#: :data:`meho_backplane.api.v1.checks._CHECKS_OP_IDS`).
#:
#: What is *not* at risk is what those rows persist. All three routes
#: bind an explicit ``audit_op_class`` contextvar (``write`` / ``read`` /
#: ``write``), and
#: :func:`~meho_backplane.broadcast.overrides.resolve_broadcast_detail`
#: takes that ``op_class_override`` in preference to this function — the
#: same value the audit row stores in ``payload["op_class"]``, which is
#: what ``meho.audit.query`` filters on in SQL. So a prefix branch would
#: not move a single stored class, and no saved ``op_class=write`` query
#: would change its results.
#:
#: The hazard is on the **read** side. ``classify_op`` is re-run at render
#: time against the stored op-id by the audit drawer
#: (:mod:`meho_backplane.ui.routes.audit.routes`) and the broadcast event
#: drawer (:mod:`meho_backplane.ui.routes.broadcast.event`), where it
#: supplies the row's displayed class and badge and feeds
#: :func:`~meho_backplane.ui.routes.broadcast.aggregate_gate.is_aggregate_only`.
#: A prefix branch would therefore relabel every gateway row as
#: ``checks`` — retroactively, because that class is derived on read
#: rather than read back off the row — and would sweep in any future
#: non-transition ``checks.*`` op-id by construction, diluting the class
#: whose whole purpose is surfacing rare transition edges.
#:
#: The allowlist is the same idiom :data:`_CREDENTIAL_MINT_OPS` uses for
#: the same reason: an exact pin that beats a broader structural match.
_CHECK_EVENT_OPS: Final[frozenset[str]] = frozenset(
    {
        "checks.transition",
        "checks.scheduler_stalled",
        "checks.scheduler_recovered",
    }
)

#: Op-id suffixes that imply mutation. Append to this tuple when a new
#: write-shaped verb spelling lands. ``.put`` is the KV-v2 write verb
#: (``vault.kv.put`` — G3.3-T1 #545); without it a secret write would
#: fall through to ``other`` and broadcast its full params, leaking the
#: written secret payload to every operator. Order doesn't matter —
#: :meth:`str.endswith` accepts a tuple and short-circuits on the
#: first match.
_WRITE_SUFFIXES: Final[tuple[str, ...]] = (
    ".create",
    ".update",
    ".delete",
    ".patch",
    ".put",
    # ``.write`` is the Vault mutating-write verb spelling for the auth
    # and sys-policy surfaces (G3.15 #1410/#1411): ``vault.auth.approle.write``
    # creates an AppRole (no secret in params → plain ``write``),
    # ``vault.sys.policy.write`` writes a policy document. Without this
    # suffix they fall through to ``other`` and broadcast their full
    # params. The secret-bearing ``.write`` ops
    # (``vault.auth.userpass.write`` / ``vault.auth.userpass.update_password``)
    # are pinned in :data:`_CREDENTIAL_WRITE_OPS`, which ``classify_op``
    # consults BEFORE this suffix branch, so this addition never downgrades
    # a credential write to plain ``write``.
    ".write",
    # bind9 record-write verbs (G3.4-T3 #589). The bind9 connector
    # uses ``.add`` / ``.remove`` rather than ``.create`` / ``.delete``
    # to match the consumer wrapper's verb shape (``--add-a-record``
    # / ``--remove-record``). Without these suffixes ``classify_op``
    # would fall through to ``other`` and the broadcast classifier
    # would emit the full param dict (including the rdata) as a
    # ``other``-class event rather than redact under the ``write``
    # branch.
    ".add",
    ".remove",
    # keycloak role-mapping privilege-grant verb (G3.13-T4 #1406).
    # ``keycloak.role_mapping.assign`` is a mutating privilege grant; its
    # params (user id/username + role names) carry no secret, so the plain
    # ``write`` class (full detail) is correct — but without this suffix it
    # falls through to ``other``. Classifying it ``write`` keeps the feed's
    # mutation signal accurate.
    ".assign",
    # RKE2 node service-restart verb (G-Node/RKE2-T3 #2430).
    # ``rke2.node.service.restart`` is a disruptive mutation; its params
    # (a single allow-listed unit name) carry no secret, so the plain
    # ``write`` class (full detail) is correct — but without this suffix it
    # falls through to ``other`` and broadcasts the full param dict as an
    # unclassified event. Classifying it ``write`` keeps the mutation signal
    # accurate on the feed.
    ".restart",
)

#: Exact-match op-ids that classify ``write`` despite not carrying a
#: write-shaped verb suffix. The four ArgoCD write verbs (#2681) are pinned
#: here rather than added to :data:`_WRITE_SUFFIXES` because both spellings
#: that a suffix would need — dotted and its underscore mirror — collide:
#:
#: * ``argocd.app.set`` / ``.sync`` / ``.rollback`` are approval-gated
#:   mutations that fell through to ``other`` (their ``.delete`` /
#:   ``appproject.create`` / ``appproject.update`` siblings already
#:   classified ``write`` via existing suffixes). A ``.set`` / ``.sync`` /
#:   ``.rollback`` suffix would work for the dotted argocd op-ids, but
#:   :data:`_MEHO_FLAT_WRITE_SUFFIXES` is auto-derived from
#:   :data:`_WRITE_SUFFIXES` by ``.`` → ``_``, so it would ALSO add
#:   ``_set`` / ``_sync`` / ``_rollback`` to the flat MCP-tool arm and
#:   silently relabel ``meho_``-prefixed ops such as
#:   ``meho_broadcast_overrides_set`` from ``other`` to ``write``. ArgoCD
#:   op-ids are always dotted (``argocd.app.set``), never ``meho_``-flat,
#:   so an exact pin classifies them ``write`` with zero flat-mirror bleed.
#: * ``argocd.app.refresh`` is an HTTP GET
#:   (``GET /api/v1/applications/{name}?refresh=hard``) that forces ArgoCD
#:   to reconcile the live vs. desired state — an approval-gated action with
#:   a cluster-side side effect, so on the approval/audit surface it is a
#:   write even though the HTTP verb is a read. A ``.refresh`` suffix is
#:   shared with ``topology.refresh``, a genuine read (it emits only
#:   aggregate node/edge counts, explicitly stored ``op_class="read"``), and
#:   because :func:`classify_op` re-runs at render time on stored op-ids a
#:   blanket suffix would silently relabel every historical
#:   ``topology.refresh`` row as ``write`` in the console drawers.
#:
#: Same exact-pin idiom as :data:`_CHECK_EVENT_OPS` /
#: :data:`_CREDENTIAL_MINT_OPS`: a precise pin that beats a broader
#: structural match.
_WRITE_OPS: Final[frozenset[str]] = frozenset(
    {
        "argocd.app.set",
        "argocd.app.sync",
        "argocd.app.rollback",
        "argocd.app.refresh",
        # keycloak group-lifecycle writes (#3280) whose op-id suffix is not
        # one of _WRITE_SUFFIXES (``.update_attributes`` / ``.add`` /
        # ``.remove``): pin them ``write`` so a group mutation shows as a
        # mutation on the feed. Group attributes are not secret material, so
        # these stay plain ``write`` (never ``credential_write``);
        # ``keycloak.group.create`` already classifies write via its
        # ``.create`` suffix. Same exact-pin idiom as the argocd entries.
        "keycloak.group.update_attributes",
        "keycloak.group.member.add",
        "keycloak.group.member.remove",
        # pfSense user flag writes (#3937): ``.disable`` / ``.enable`` are not
        # write suffixes, and a suffix would also relabel the flat ``meho_``
        # mirror. The params carry only a user name, so plain ``write``.
        "pfsense.user.disable",
        "pfsense.user.enable",
        # keycloak per-user session logout (#3851): a mutation (it ends live
        # sessions) whose ``.logout`` suffix is not a write suffix. Its params
        # are a user id or username only, so plain ``write``.
        "keycloak.user.logout",
        # phpIPAM address writes: ``.reserve`` / ``.release`` are not write
        # suffixes. The params carry a subnet id, an address and a host name
        # only, so plain ``write``.
        "phpipam.address.reserve",
        "phpipam.address.release",
        # MEHO's own registry writes, run through the dispatcher on synthetic
        # connectors. None of their verbs is a write suffix, so without a pin
        # they showed as ``other``. Their params carry no secret material
        # (``targets.register`` takes a ``secret_ref`` path, never a secret).
        "targets.register",
        "topology.annotate",
        "topology.unannotate",
        "topology.bulk_import",
        "topology.create_node",
        "topology.delete_node",
        # Hand-written ``safe`` operations that are not plain reads: each
        # makes the target do work (rescan a datastore, save an etcd
        # snapshot, reload a template list, start an HCX diagnostics run).
        # Without a pin they would get ``read`` from
        # :func:`resolve_operation_op_class`, like every other ``safe``
        # hand-written operation.
        "vmware.composite.datastore.refresh",
        "rke2.etcd-snapshot.save",
        "vcfa.tenant.cloud_account.enumerate_images",
        "hcx.service_mesh.diagnose",
    }
)

#: Op-id suffixes that imply non-mutating read. ``.ls`` and ``.about``
#: are the CLI-shaped verbs (``meho vsphere ls``, ``meho meho about``)
#: that the connector layer maps to the same read class as ``.list`` /
#: ``.get`` / ``.info``. ``.health`` and ``.seal_status`` are the Vault
#: ``sys`` diagnostics verbs (G3.3-T2 #546): non-mutating cluster-state
#: reads with no secret content, so they broadcast at the same
#: ``read`` sensitivity as ``.list`` rather than falling through to
#: the full-detail ``other`` class. ``.versions`` is the KV-v2
#: version-metadata browse (``vault.kv.versions`` — G3.3-T1 #545): a
#: read of metadata only (no secret values), so it likewise classifies
#: ``read`` rather than ``credential_read``. ``.vulnerabilities`` is the
#: Harbor per-artifact CVE-list read (``harbor.artifact.vulnerabilities`` --
#: #2857): a non-mutating supply-chain read whose noun suffix (like
#: ``.health`` / ``.versions``) would otherwise fall through to the
#: full-detail ``other`` class rather than broadcast at the same ``read``
#: sensitivity as its ``harbor.artifact.info`` sibling. ``.summary`` is the
#: Harbor project storage-occupancy read (``harbor.project.summary`` — #2858):
#: a non-mutating quota/usage read whose noun suffix (like ``.health`` /
#: ``.versions``) would otherwise fall through to the full-detail
#: ``other`` class rather than broadcast at the same ``read`` sensitivity
#: as its ``harbor.project.info`` sibling. Adding it also reclassifies
#: any other ``*.summary`` metrics read from ``other`` to ``read`` —
#: the correct class for a non-mutating read; neither class is sensitive
#: (:data:`~meho_backplane.broadcast.overrides._SENSITIVE_OP_CLASSES`),
#: so the broadcast detail level is unchanged for it. ``.ha_status`` and
#: ``.autopilot_state`` are the Vault cluster-wide node reads
#: (``vault.sys.ha_status`` / ``vault.sys.raft.autopilot_state``): like
#: ``.health`` / ``.seal_status`` they carry no secret content and no
#: params, so they broadcast as ``read``.
_READ_SUFFIXES: Final[tuple[str, ...]] = (
    ".list",
    ".info",
    ".get",
    ".about",
    ".ls",
    ".health",
    ".seal_status",
    ".ha_status",
    ".autopilot_state",
    ".versions",
    ".vulnerabilities",
    ".summary",
)

#: Underscore-spelled mirrors of the dotted verb suffixes, applied only to
#: ``meho_``-prefixed op-ids (MEHO's own MCP tool names after the #2745
#: rename to the Anthropic tool-name pattern ``^[a-zA-Z0-9_-]{1,64}$``).
#: Derived from the dotted tuples rather than hand-listed so the two verb
#: policies can never drift apart.
_MEHO_FLAT_WRITE_SUFFIXES: Final[tuple[str, ...]] = tuple(
    suffix.replace(".", "_") for suffix in _WRITE_SUFFIXES
)
_MEHO_FLAT_READ_SUFFIXES: Final[tuple[str, ...]] = tuple(
    suffix.replace(".", "_") for suffix in _READ_SUFFIXES
)


class BroadcastEvent(BaseModel):
    """One broadcast event — exactly one per audited operation.

    The publish-on-write hook (T3) constructs an instance per audit-log
    write; ``XADD meho:feed:{tenant_id}`` carries it onto the per-tenant
    Valkey stream. SSE subscribers (T4) and MCP resource readers (T6)
    deserialise back to this shape.

    The model is **frozen** (``ConfigDict(frozen=True)``) — attribute
    reassignment (``event.payload = new_dict``) raises ``ValidationError``,
    mirroring the chassis
    :class:`~meho_backplane.auth.operator.Operator` pattern.
    ``frozen=True`` is **faux-immutability** in pydantic v2: nested
    mutable values like the ``payload`` dict CAN still be mutated in
    place — ``event.payload["k"] = "v"`` succeeds silently because it
    mutates the inner dict object rather than reassigning the
    attribute. Downstream consumers MUST NOT mutate ``payload``
    between construction and publish. The PII contract is enforced
    upstream by the publisher calling :func:`redact_payload` *before*
    construction, not by the model itself. A future tightening to
    ``Mapping[str, Any]`` + ``types.MappingProxyType`` would close
    the in-place-mutation door but requires a coordinated change to
    T3 / T4 / T6 deserialisation and is out of scope for T2.

    ``payload`` is **always** the redacted view per
    :func:`redact_payload` — callers MUST NOT pass raw params here.
    The class can't enforce this from the type system alone (any
    ``dict[str, Any]`` satisfies the annotation), so the contract is
    documented in the publisher's docstring (T3) and verified by
    integration tests that read back from the stream and assert
    forbidden keys are absent.
    """

    model_config = ConfigDict(frozen=True)

    #: G0.16-T6 Finding F (#1312) discriminator per
    #: ``docs/codebase/api-shape-conventions.md`` §6. Every entry on
    #: ``meho:feed:{tenant_id}`` carries a top-level ``kind`` field that
    #: consumers switch on:
    #:
    #: * ``"operation"`` -- audit-driven :class:`BroadcastEvent` (this
    #:   class), one per audited operation.
    #: * ``"agent_announcement"`` -- agent-authored
    #:   :class:`~meho_backplane.broadcast.agent_events.AgentAnnouncementEvent`,
    #:   published via ``meho_broadcast_announce``.
    #:
    #: Default value, not :class:`typing.Literal`-pinned, so the
    #: history-parser's switch-on-``kind`` covers two cases under a
    #: single read path: (a) post-migration entries XADD'd by the
    #: current publisher (``kind: "operation"`` written on the wire),
    #: and (b) pre-migration entries written before this field
    #: existed -- those entries lack ``kind`` on the wire, and the
    #: parser infers ``"operation"`` from the absence of
    #: ``"agent_announcement"``-shape fields. The historical
    #: ``event_kind`` field on the sibling agent-announcement class
    #: stays accepted on read as a backward-compatible alias.
    kind: str = "operation"

    event_id: UUID
    ts: datetime
    tenant_id: UUID
    principal_sub: str
    #: Best-effort from the JWT's ``name`` claim cached at audit-write
    #: time. Many JWTs ship without a ``name`` claim; the publisher
    #: must be able to omit this field without forcing every call site
    #: to pass an explicit ``None``. Same reasoning for ``target_name``.
    principal_name: str | None = None
    target_name: str | None = None
    op_id: str
    #: One of ``"read"`` / ``"write"`` / ``"credential_read"`` /
    #: ``"credential_mint"`` / ``"credential_write"`` / ``"audit_query"``
    #: / ``"approval"`` / ``"checks"`` / ``"other"``. Usually derived
    #: from :func:`classify_op` at publish time — but a route that binds
    #: the ``audit_op_class`` contextvar overrides it:
    #: :func:`~meho_backplane.broadcast.overrides.resolve_broadcast_detail`
    #: takes ``op_class_override`` in preference to :func:`classify_op`,
    #: and the same value is what the audit row persists under
    #: ``payload["op_class"]``.
    op_class: str
    #: One of ``"ok"`` / ``"error"`` / ``"denied"``. The handler
    #: produces it; the broadcast publisher does not re-classify.
    result_status: str
    #: FK to ``audit_log.id``. The broadcast event is downstream of the
    #: audit write — audit is the canonical record, broadcast is the
    #: real-time view. A subscriber that wants the full untruncated row
    #: queries audit_log by this id.
    audit_id: UUID
    #: Redacted view per :func:`redact_payload`. NEVER raw params for
    #: ``credential_read``, ``credential_mint``, ``credential_write``, or
    #: ``audit_query`` classes — the redaction contract is upstream of
    #: this field.
    payload: dict[str, Any] = Field(default_factory=dict)
    #: RFC 8693 actor (the acting agent) when the operation ran under a
    #: user-initiated agent delegation, ``None`` for a direct human
    #: request or an autonomous (self-authenticated) agent run. Projected
    #: at publish time from the same
    #: :func:`~meho_backplane.auth.delegation.resolve_actor_sub` the
    #: sibling ``audit_log`` writer reads, so a feed reader can tell a
    #: delegated agent's work (``actor_sub`` = agent, ``principal_sub`` =
    #: human) from the human's own. Server-derived and trusted — no
    #: untrusted-prose envelope applies.
    actor_sub: str | None = None
    #: Agent-run session id when the operation was dispatched from inside
    #: an agent loop (the run id doubles as the session id), ``None``
    #: otherwise. Groups every operation a single run produced —
    #: projected from :data:`~meho_backplane.operations._audit.agent_session_id_var`,
    #: the same contextvar the ``audit_log.agent_session_id`` column reads.
    agent_session_id: UUID | None = None
    #: External change-ticket reference (a GitHub issue, Jira key, CR id)
    #: correlating the operation to the out-of-band record that authorised
    #: it, ``None`` when unbound. Projected from
    #: :data:`~meho_backplane.operations._audit.work_ref_var`, the same
    #: contextvar the ``audit_log.work_ref`` column reads.
    work_ref: str | None = None


# code-quality-allow: flat order-significant op-id classifier; length is the
# explanatory match-order docstring, not branching. Pre-existing >100 (126 on
# main); #2406 only adds the `net.` prefix to an existing read tuple.
def classify_op(op_id: str) -> str:
    """Map an op-id to one of the sensitivity classes.

    Match order is policy-significant:

    1. ``credential_read`` — explicit allowlist first so a hypothetical
       future ``vault.kv.audit-list`` (audit metadata, not secret
       content) could opt out by being absent from
       :data:`_CREDENTIAL_READ_OPS` and falling through to the suffix
       check.
    2. ``credential_mint`` — explicit allowlist for ops that return a
       freshly-minted secret in their response payload (e.g.
       ``harbor.robot.create``, ``vault.token.create``). Checked before
       the ``.create`` suffix so the allowlist wins over the
       mutation-suffix branch.
    3. ``credential_write`` — explicit allowlist for write ops whose
       *request params* carry a secret (e.g.
       ``vault.auth.userpass.write``, ``vault.kv.put``,
       ``k8s.secret.create``). Checked before the write-suffix branch
       so the secret-bearing params collapse to aggregate-only instead
       of broadcasting in full under the plain ``write`` class
       (G11.7-T1 #1401).
    4. ``audit_query`` — every op-id with the ``audit.``,
       ``meho.audit.``, or ``meho_audit_`` prefix classifies as
       audit_query regardless of the verb suffix. The ``meho_audit_``
       arm catches the admin replay meta-tool (``meho_audit_replay``,
       G8.2-T6 #1014, renamed from ``meho.audit.replay`` in #2745):
       the MCP broadcast path classifies via ``classify_op(op_id)``
       with the tool name verbatim, so without this arm the replay
       tool would fall through to ``other`` and broadcast its full
       ``ReplayNode`` payload instead of the aggregate-only view. The
       dotted ``meho.audit.`` arm stays because this function is
       re-run at render time on *stored* op-ids — pre-#2745 rows keep
       their dotted identity forever.
    4b. ``approval`` — every op-id with the ``approval.`` prefix
       (``approval.pending`` / ``.approved`` / ``.rejected`` /
       ``.expired``, and the ``approval.*`` audit op-ids) classifies as
       the dedicated ``approval`` class, so an ``op_class=approval``
       audit filter finds the lifecycle rows rather than mixing them
       with every unclassified op under ``other``. The class is **not** sensitive
       (``approval.*`` payloads carry only ``connector_id`` / ``op_id`` /
       ``decision`` / the request id, never secret material), so it
       stays full-detail like ``other`` did -- see
       :data:`~meho_backplane.broadcast.overrides._SENSITIVE_OP_CLASSES`.
    4c. ``checks`` — the checks subsystem's own state-change events
       (:data:`_CHECK_EVENT_OPS`; ``checks.transition`` from #2720
       plus #2763's ``checks.scheduler_stalled`` /
       ``checks.scheduler_recovered`` watchdog pair; no longer published
       since #3758).
       Its own class for the same reason ``approval`` has one: a feed
       watcher filters ``op_class=checks`` server-side to follow
       Dashboard rollup edges without reading every unclassified op.
       Matched by **exact membership**, not by ``checks.`` prefix — the
       ``/api/v1/checks/*`` gateway routes already own op-ids under that
       prefix, and because this function is re-run at render time on the
       stored op-id, a prefix branch would relabel their rows (historical
       ones included) in the drawers; see :data:`_CHECK_EVENT_OPS` for
       the full rationale. Not sensitive (the payload is dashboard id /
       name / the two states), so it stays full-detail.
    5. ``read`` / ``write`` — HTTP-method-prefixed ingested op IDs
       (e.g. ``GET:/api/v2.0/systeminfo``). ``GET:`` and ``HEAD:``
       map to ``read``; ``POST:``, ``PUT:``, ``PATCH:``, ``DELETE:``
       map to ``write``. Checked before the dot-suffix branches since
       ingested ops carry no meho verb suffix. The ``net.`` product
       prefix (the synthetic network-diagnostics connector, #2406)
       rides the ``read`` arm here: every ``net.*`` op is a
       non-mutating probe, matched by prefix because its verbs are
       underscore-joined (``net.tcp_check``), so a dotted read suffix
       never matches.
    6. ``write`` — the exact-match :data:`_WRITE_OPS` pins (the four
       ArgoCD write verbs ``argocd.app.set`` / ``.sync`` / ``.rollback`` /
       ``.refresh``, pinned rather than made global suffixes to avoid a
       flat-mirror bleed onto ``meho_``-prefixed ops and a ``.refresh``
       collision with the read-class ``topology.refresh`` — see
       :data:`_WRITE_OPS`) OR the mutation suffixes (``.create`` /
       ``.update`` / ``.delete`` / ``.patch`` / ``.put`` / ``.write`` /
       ``.add`` / ``.remove`` / ``.assign`` / ``.restart``).
       The ``_CREDENTIAL_WRITE_OPS`` allowlist (step 3) runs first, so a
       secret-bearing op like ``vault.auth.userpass.write`` or
       ``rke2.node.config.update`` (``.update``-shaped, token-bearing
       patch) keeps its ``credential_write`` class.
    7. ``read`` — non-mutating verb suffixes (``.list`` / ``.info`` /
       ``.get`` / ``.about`` / ``.ls`` / ``.health`` / ``.seal_status``
       / ``.versions`` / ``.summary``). ``.read`` is deliberately **not** a read
       suffix: it would over-match the ``credential_read``-allowlisted
       ``vault.kv.read`` (the allowlist wins, but the exclusion keeps
       the policy single-sourced) and would reclassify the auth-config
       ``.read`` ops that intentionally broadcast as ``other``.
    6b/7b. ``write`` / ``read`` for MEHO's own flat MCP tool names —
       #2745 renamed the dotted ``meho.*`` MCP tools to
       underscore-only names (``meho.sensor.list`` →
       ``meho_sensor_list``) to satisfy the Anthropic tool-name
       pattern ``^[a-zA-Z0-9_-]{1,64}$``. The MCP broadcast path
       classifies on the tool name verbatim, so a scoped arm mirrors
       the dotted verb-suffix policy for ``meho_``-prefixed op-ids
       using underscore-spelled suffixes derived from the same tuples
       (:data:`_WRITE_SUFFIXES` / :data:`_READ_SUFFIXES` — single
       source, stays in lockstep). Scoped by prefix so no other flat
       op-id (``call_operation``, ``add_to_memory``, …) is
       reclassified, and terminal — a ``meho_`` name that matches no
       verb suffix is ``other`` without consulting the dotted arms
       (it cannot match them anyway; the early return just makes the
       scoping explicit).
    8. ``other`` — everything else. Falls through to full-detail
       broadcast per decision #3.

    Examples
    --------

    >>> classify_op("vault.kv.read")
    'credential_read'
    >>> classify_op("harbor.robot.create")
    'credential_mint'
    >>> classify_op("vault.token.create")
    'credential_mint'
    >>> classify_op("vault.auth.userpass.write")
    'credential_write'
    >>> classify_op("vault.kv.put")
    'credential_write'
    >>> classify_op("audit.query")
    'audit_query'
    >>> classify_op("meho_audit_replay")
    'audit_query'
    >>> classify_op("approval.pending")
    'approval'
    >>> classify_op("approval.approved")
    'approval'
    >>> classify_op("checks.transition")
    'checks'
    >>> classify_op("checks.assignment.put")
    'write'
    >>> classify_op("GET:/api/v2.0/systeminfo")
    'read'
    >>> classify_op("DELETE:/api/v2.0/projects/myproj/repositories/myrepo")
    'write'
    >>> classify_op("net.tcp_check")
    'read'
    >>> classify_op("k8s.pod.list")
    'read'
    >>> classify_op("k8s.namespace.create")
    'write'
    >>> classify_op("argocd.app.set")
    'write'
    >>> classify_op("argocd.app.sync")
    'write'
    >>> classify_op("argocd.app.rollback")
    'write'
    >>> classify_op("argocd.app.refresh")  # exact-match pin, not a .refresh suffix
    'write'
    >>> classify_op("topology.refresh")  # a read: .refresh is deliberately not global
    'other'
    >>> classify_op("targets.register")  # registry write, exact-match pin
    'write'
    >>> classify_op("some.unknown.op")
    'other'
    >>> classify_op("meho_audit_replay")
    'audit_query'
    >>> classify_op("meho_sensor_list")
    'read'
    >>> classify_op("meho_sensor_create")
    'write'
    >>> classify_op("meho_broadcast_watch")
    'other'
    >>> classify_op("meho.sensor.list")  # pre-#2745 stored rows re-render unchanged
    'read'
    """
    if op_id in _CREDENTIAL_READ_OPS:
        return "credential_read"
    if op_id in _CREDENTIAL_MINT_OPS:
        return "credential_mint"
    if op_id in _CREDENTIAL_WRITE_OPS:
        return "credential_write"
    if op_id.startswith(("audit.", "meho.audit.", "meho_audit_")):
        return "audit_query"
    # Approval-queue lifecycle events (approval.pending / .approved /
    # .rejected / .expired). Their own class so the approvals console
    # bell can filter the SSE bridge to op_class=approval (#1778);
    # not sensitive, so it stays full-detail like the `other` fall-through.
    if op_id.startswith("approval."):
        return "approval"
    # Checks-subsystem state-change events (checks.transition), so a feed
    # watcher can filter op_class=checks for Dashboard rollup edges (#2720).
    # Exact membership, not a `checks.` prefix: the /api/v1/checks/* gateway
    # routes own op-ids under that prefix and keep their existing classes.
    if op_id in _CHECK_EVENT_OPS:
        return "checks"
    # Ingested ops use HTTP-method prefixes (e.g. "GET:/api/v2.0/systeminfo");
    # GET/HEAD are safe reads by HTTP semantics. The synthetic net.* probes
    # (#2406) ride the same arm: every net.* op is a non-mutating
    # reachability probe, and it's matched by product prefix (not verb
    # suffix) because the verbs are underscore-joined (`net.tcp_check`), so
    # a dotted read suffix like `.check` never matches the `tcp_check`
    # segment. Checked after the explicit allowlists so credential_mint pins
    # still win.
    if op_id.startswith(("GET:", "HEAD:", "net.")):
        return "read"
    if op_id.startswith(("POST:", "PUT:", "PATCH:", "DELETE:")):
        return "write"
    # MEHO's own flat MCP tool names (#2745: dotted meho.* → meho_*, per the
    # Anthropic tool-name pattern). Mirrors the dotted verb-suffix policy with
    # underscore spellings derived from the same tuples; scoped to the meho_
    # prefix so no other flat op-id is reclassified, and terminal so the
    # scoping is explicit (a dotless name can't match the dotted arms anyway).
    if op_id.startswith("meho_"):
        if op_id.endswith(_MEHO_FLAT_WRITE_SUFFIXES):
            return "write"
        if op_id.endswith(_MEHO_FLAT_READ_SUFFIXES):
            return "read"
        return "other"
    if op_id in _WRITE_OPS or op_id.endswith(_WRITE_SUFFIXES):
        return "write"
    if op_id.endswith(_READ_SUFFIXES):
        return "read"
    return "other"


#: Classes :func:`resolve_op_class` never lets a declared class replace:
#: they decide that the broadcast shows only an aggregate (decision #3).
_SECRET_SHAPE_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write", "audit_query"}
)


def resolve_op_class(op_id: str, declared: str | None = None) -> str:
    """Return the one op class an audit row and its broadcast event both carry.

    Every audit writer (HTTP middleware, MCP handler, dispatcher) and every
    broadcast publisher calls this, so the two records of one call can never
    disagree on the class.

    * *declared* is the class the caller states for the op: the MCP tool's
      ``op_class``, or a route's ``audit_op_class``. It wins over the class
      :func:`classify_op` derives from the op id, because a tool name like
      ``list_targets`` carries no verb suffix the classifier knows.
    * The one exception: when :func:`classify_op` says the op touches
      credentials or the audit log, that class wins. A declared class can
      never turn an aggregate-only broadcast into a full one.
    * With no *declared* class, the derived class is the answer.

    >>> resolve_op_class("list_targets", "read")
    'read'
    >>> resolve_op_class("list_targets")
    'other'
    >>> resolve_op_class("vault.kv.read", "read")
    'credential_read'
    """
    derived = classify_op(op_id)
    if declared and derived not in _SECRET_SHAPE_CLASSES:
        return declared
    return derived


#: Source kinds of hand-written operations: typed operations and multi-step
#: (composite) operations. Ingested operations get their class from the
#: HTTP method in their op id instead.
_HAND_WRITTEN_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"typed", "composite"})

#: Risk levels of an operation that changes something.
_CHANGE_SAFETY_LEVELS: Final[frozenset[str]] = frozenset({"caution", "dangerous", "destructive"})


def resolve_operation_op_class(op_id: str, *, source_kind: str, safety_level: str | None) -> str:
    """Return the class of one run of a registered operation.

    The dispatcher stores this class on the audit row, and the announce
    gate, the announce reminder and the target activity warning use it.
    So all four always agree.

    A hand-written (typed or multi-step) operation states its risk level
    where it is registered. That risk level is the declared class that goes
    to :func:`resolve_op_class`. The last word of the op id is not enough:
    reads like ``vmware.host.usage`` and writes like
    ``vmware.composite.vm.destroy`` end in words :func:`classify_op` does
    not know.

    * Risk level ``caution`` or higher: the class is ``write``.
    * Risk level ``safe``: the class is ``read`` when :func:`classify_op`
      knows nothing better (``other``). A ``safe`` operation that does make
      the target do work is pinned in :data:`_WRITE_OPS`.
    * The credential and audit-query classes always win, as in
      :func:`resolve_op_class`.
    * Ingested operations, and a row with no risk level, keep
      ``classify_op(op_id)``.

    >>> resolve_operation_op_class("vmware.host.usage", source_kind="typed", safety_level="safe")
    'read'
    >>> resolve_operation_op_class(
    ...     "vmware.composite.vm.destroy", source_kind="composite", safety_level="destructive"
    ... )
    'write'
    >>> resolve_operation_op_class("vault.kv.read", source_kind="typed", safety_level="caution")
    'credential_read'
    >>> resolve_operation_op_class("POST:/vcenter/vm", source_kind="ingested", safety_level="safe")
    'write'
    """
    declared: str | None = None
    if source_kind in _HAND_WRITTEN_SOURCE_KINDS:
        if safety_level in _CHANGE_SAFETY_LEVELS:
            declared = "write"
        elif safety_level == "safe" and classify_op(op_id) == "other":
            declared = "read"
    return resolve_op_class(op_id, declared)


def _maybe_row_count(raw_params: dict[str, Any]) -> int | None:
    """Extract ``row_count`` from the publisher's combined params dict.

    The publish-on-write hook (T3) merges request params and response
    summary into one dict before calling :func:`redact_payload`. For
    ``audit_query`` ops the response carries a ``row_count`` field
    (per the G8 audit-query API in #334); for older or non-conforming
    callers it may be absent.

    Returns ``None`` rather than ``0`` when the field is missing so
    subscribers can distinguish "the query matched zero rows" from
    "the publisher didn't surface a count". Coerces to ``int`` defensively
    — a stringified count from a JSON round-trip would otherwise serialise
    back as a string.
    """
    raw = raw_params.get("row_count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def redact_payload(
    op_class: str,
    raw_params: dict[str, Any],
    result_status: str,
    *,
    detail: Literal["full", "aggregate"] | None = None,
) -> dict[str, Any]:
    """Return the broadcast-safe payload view for *op_class*.

    Three shapes -- selected by the *effective* detail rather than the
    op_class alone:

    * **Aggregate**, sensitive class (``audit_query``) →
      ``{op_class, result_status, row_count}``. The audit-query
      aggregate retains the response row-count -- a useful
      team-coordination signal -- but never the filter or matched rows.
    * **Aggregate**, any other class →
      ``{op_class, result_status}``. The same shape G6.1's
      ``credential_read`` default used; pulled out as the universal
      aggregate when a tenant rule downgrades a normally-full op
      (e.g. ``k8s.configmap.info`` scoped to ``kube-system``).
    * **Full** →
      ``{op_class, params=raw_params, result_status}``. Full request
      detail; nested objects pass through verbatim. Used by the
      everything-else default and also by the G6.3 ``request_override``
      branch that upgrades a sensitive class to full detail per
      operator opt-in.

    *detail* is the G6.3-T2 (#379) extension. When ``None`` (the
    pre-G6.3 default), the function falls back to decision #3 of
    ``docs/decisions/locked-decisions.md`` -- aggregate for sensitive
    classes (``credential_read`` / ``credential_mint`` /
    ``credential_write`` / ``audit_query``), full for everything else.
    When ``"aggregate"`` or ``"full"`` is passed
    explicitly, the resolver (:func:`compute_effective_broadcast_detail`)
    has already decided the effective detail and this function just
    renders it. Callers that don't go through the resolver keep
    the ``detail=None`` default and the existing per-class behaviour.

    Forward-compatibility note: the return shape is a plain ``dict``
    rather than a typed model because the downstream
    :attr:`BroadcastEvent.payload` field is already ``dict[str, Any]``.
    Promoting either side to a structured model would require a
    coordinated change to all of T3 / T4 / T6 and is out of scope.
    """
    if detail is None:
        effective_detail: Literal["full", "aggregate"] = (
            "aggregate"
            if op_class in {"credential_read", "credential_mint", "credential_write", "audit_query"}
            else "full"
        )
    else:
        effective_detail = detail

    if effective_detail == "aggregate":
        if op_class == "audit_query":
            return {
                "op_class": op_class,
                "result_status": result_status,
                "row_count": _maybe_row_count(raw_params),
            }
        return {"op_class": op_class, "result_status": result_status}

    return {
        "op_class": op_class,
        "params": raw_params,
        "result_status": result_status,
    }


# ---------------------------------------------------------------------------
# Tier-1 defence-in-depth for full-detail broadcasts (meho-internal #151)
# ---------------------------------------------------------------------------
#
# ``classify_op`` is allowlist-driven: whether a secret-bearing write op
# collapses to aggregate-only depends on a human having pinned it into
# :data:`_CREDENTIAL_WRITE_OPS` / :data:`_CREDENTIAL_MINT_OPS`. A newly
# ingested op, a mis-registered op, or a connector op added without a
# pin falls through to the ``write`` / ``other`` class and would ship
# its full request params to every co-tenant feed subscriber. The
# helpers below are the classification-independent second layer: the
# dispatch publisher runs every params dict through
# :func:`scrub_broadcast_params` before building the payload, and
# collapses the broadcast to aggregate-only whenever secret material
# is detected. The static classifier-coverage test
# (``tests/test_broadcast_classifier_coverage.py``) is the companion
# CI layer that forces registered secret-bearing write ops to get
# pinned; this runtime layer covers everything the static sweep cannot
# see.

#: Name-tokens that mark a param key as secret-bearing wherever they
#: appear in the normalised (snake-cased) token list: ``password`` /
#: ``user_password`` / ``passphrase`` / ``kubeconfig`` are secret
#: material in any position. ``key`` alone is deliberately NOT here
#: (``ssh_public_key`` / ``label_key`` would over-match) -- key-ish
#: names are covered by the pair combinations below.
_SECRET_NAME_TOKENS_ANYWHERE: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "apikey",
        "kubeconfig",
    }
)

#: Name-tokens that mark a key as secret-bearing only in **final**
#: position. ``token`` / ``secret`` name the credential itself when
#: final (``token``, ``session_token``, ``client_secret``,
#: ``credentials``) but an *attribute or reference* of it when
#: followed by more tokens (``token_policies`` / ``token_ttl`` on the
#: Vault AppRole write surface, ``secret_name`` / ``secret_path`` as
#: k8s/Vault references) -- those carry no secret material and must
#: not collapse a vetted full-detail write broadcast to aggregate.
_SECRET_NAME_TOKENS_FINAL: Final[frozenset[str]] = frozenset(
    {
        "secret",
        "secrets",
        "token",
        "credential",
        "credentials",
    }
)

#: Adjacent token pairs that mark a key as secret-bearing when the
#: positional rules above don't (``api_key``, ``private_key``,
#: ``secret_id`` -- the Vault AppRole SecretID is secret material
#: even though ``secret`` is non-final there; ``license_key`` -- a
#: product licence is a credential a vendor file may show in clear).
_SECRET_NAME_TOKEN_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("private", "key"),
        ("encryption", "key"),
        ("license", "key"),
        ("licence", "key"),
        ("secret", "id"),
        ("secret", "value"),
        ("secret", "data"),
    }
)

#: Replacement value for a secret-named param. Mirrors the Tier-1
#: engine's ``[REDACTED:<pattern>]`` shape so feed consumers see one
#: consistent redaction vocabulary.
_REDACTED_PARAM: Final[str] = "[REDACTED:param_name]"

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _is_secret_param_name(name: str) -> bool:
    """True when *name* is a secret-shaped param/field name.

    Normalises camelCase and kebab-case to snake_case tokens, then
    checks single tokens and adjacent pairs. The name is also checked
    once without the camelCase split, so an odd-cased whole word still
    matches as one token. Shared by the runtime scrub below and the
    classifier-coverage test's parameter-schema sweep so both layers
    agree on what "secret-bearing" means.
    """
    flat = name.replace("-", "_").replace(".", "_")
    return _tokens_are_secret(_CAMEL_BOUNDARY.sub("_", flat).lower()) or _tokens_are_secret(
        flat.lower()
    )


def _tokens_are_secret(normalised: str) -> bool:
    """Apply the token rules to an already-normalised snake_case name."""
    tokens = [token for token in normalised.split("_") if token]
    if not tokens:
        return False
    if any(token in _SECRET_NAME_TOKENS_ANYWHERE for token in tokens):
        return True
    if tokens[-1] in _SECRET_NAME_TOKENS_FINAL:
        return True
    return any(pair in _SECRET_NAME_TOKEN_PAIRS for pair in itertools.pairwise(tokens))


def is_secret_param_name(name: str) -> bool:
    """Public entry to :func:`_is_secret_param_name`.

    The approval-park params echo
    (:func:`~meho_backplane.operations._preview._redact_echoed_params`)
    uses it to decide which param values to mask, so the echo and the
    broadcast scrub cannot disagree on what a secret-named param is.
    """
    return _is_secret_param_name(name)


#: Each adjacent pair written as one word, for a name whose case is lost.
_SECRET_NAME_PAIRS_JOINED: Final[frozenset[str]] = frozenset(
    first + second for first, second in _SECRET_NAME_TOKEN_PAIRS
)


def is_secret_param_name_lowercased(name: str) -> bool:
    """True when *name* can be a secret-shaped name once its case is lost.

    An HTML parser lowercases element and attribute names, so
    ``licenseKey`` arrives as ``licensekey`` and the camelCase boundary
    that :func:`is_secret_param_name` splits on is gone. This check asks
    whether any placement of such boundaries makes the name secret: a
    token that contains a secret word anywhere, a last token that ends in
    a final-position word, or two pair words that touch, inside one
    token or across two. It matches more names than the exact rule
    (``passwordless`` counts); that replaces a value more, never less.
    """
    flat = name.replace("-", "_").replace(".", "_").lower()
    tokens = [token for token in flat.split("_") if token]
    if not tokens:
        return False
    if any(word in token for token in tokens for word in _SECRET_NAME_TOKENS_ANYWHERE):
        return True
    if any(tokens[-1].endswith(word) for word in _SECRET_NAME_TOKENS_FINAL):
        return True
    if any(pair in token for token in tokens for pair in _SECRET_NAME_PAIRS_JOINED):
        return True
    return any(
        left.endswith(first) and right.startswith(second)
        for left, right in itertools.pairwise(tokens)
        for first, second in _SECRET_NAME_TOKEN_PAIRS
    )


def _scrub_secret_named_keys(node: Any) -> tuple[Any, bool]:
    """Replace values of secret-named keys; report whether any were found.

    The Tier-1 engine matches *labelled* secret shapes inside string
    leaves (``password=...``, ``Bearer ...``); a dict entry like
    ``{"password": "hunter2"}`` carries no label in the leaf itself, so
    the engine alone would pass it through verbatim. This walk is the
    key-name-aware complement: any mapping entry whose key is
    secret-shaped has its whole value subtree replaced (a nested value
    under a ``credentials`` key is secret material regardless of its
    inner structure).

    Bare scalars (bool / int / float / ``None``) under a secret-shaped
    key pass through unflagged: secret material is a string or a blob,
    never ``bind_secret_id=true`` or ``secret_id_ttl=3600`` -- the
    Vault AppRole config attributes that would otherwise collapse a
    vetted full-detail write broadcast to aggregate on every dispatch.
    """
    if isinstance(node, Mapping):
        found = False
        scrubbed: dict[str, Any] = {}
        for key, value in node.items():
            if _is_secret_param_name(str(key)) and isinstance(value, (str, Mapping, Sequence)):
                scrubbed[str(key)] = _REDACTED_PARAM
                found = True
                continue
            child, child_found = _scrub_secret_named_keys(value)
            scrubbed[str(key)] = child
            found = found or child_found
        return scrubbed, found
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        items: list[Any] = []
        found = False
        for item in node:
            child, child_found = _scrub_secret_named_keys(item)
            items.append(child)
            found = found or child_found
        return items, found
    return node, False


def scrub_secret_named_values(node: Any) -> tuple[Any, bool]:
    """Key-name-aware secret scrub for a caller-bound dispatch response.

    Public entry to :func:`_scrub_secret_named_keys` (the same walk the
    broadcast params scrub uses). Returns ``(scrubbed, secret_detected)``:
    every mapping entry whose key is secret-shaped (``password``,
    ``client_secret``, ``sessionToken``, ...) has its value subtree
    replaced with :data:`_REDACTED_PARAM`.

    The connector-boundary redaction engine (``dispatcher.py`` step 7a)
    matches only *labelled* secret shapes inside string leaves, so a
    ``credential_read`` response like ``{"data": {"password": "..."}}``
    passes through it verbatim. The dispatcher runs this scrub on the
    caller-bound response of a ``credential_read``-classified op by
    default (#2467) so the raw value never reaches the ``call_operation``
    caller / agent transcript; ``params.reveal_secret=true`` opts out.
    The audit row keeps the raw result regardless (the raw payload is
    captured before this scrub runs), mirroring the #2172 secret-handler
    posture. Unlike :func:`scrub_broadcast_params` this does **not** run
    the Tier-1 engine pass -- the boundary middleware already applied it
    to the response, so this layer is only the structured-dict
    key-name complement.
    """
    return _scrub_secret_named_keys(node)


def scrub_broadcast_params(params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Tier-1-scrub a broadcast params dict; report secret detection.

    Returns ``(scrubbed_params, secret_detected)``. Two passes:

    1. Key-name scrub (:func:`_scrub_secret_named_keys`) -- replaces the
       value of every secret-named key (``password``, ``client_secret``,
       ``sessionToken``, ...).
    2. Tier-1 engine pass with the packaged default policy -- catches
       labelled secret shapes embedded in string values (``Bearer ...``,
       ``api_key=...``, ``Authorization:`` header dumps). The policy is
       pinned via :func:`~meho_backplane.redaction.resolver.get_default_policy`,
       deliberately bypassing the resolver's override table: an
       operator-registered shadow-mode or narrowed policy must not
       silently disable the broadcast floor.

    ``secret_detected`` is ``True`` when either pass found secret
    material; the dispatch publisher uses it to collapse the broadcast
    to aggregate-only, so a mis/unclassified secret-bearing op never
    ships even a redacted-shape params dict. Fail-closed: any exception
    reports ``({}, True)`` -- on this fail-open publish path an
    unredacted fallback would be the leak.
    """
    try:
        scrubbed, found = _scrub_secret_named_keys(params)
        result = redact(scrubbed, get_default_policy())
        found = found or bool(result.manifest)
        redacted = result.redacted
        if not isinstance(redacted, dict):
            return {}, True
        return redacted, found
    except Exception:  # fail-closed: never leak, never raise on the publish path
        return {}, True
