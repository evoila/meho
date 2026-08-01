# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.mail — SMTP send as a typed op (#2717, Initiative #2716).

A **synthetic** connector subpackage on the ``net.*`` mould: no vendor
connector backs it, so this package calls neither ``register_connector``
nor ``register_connector_v2``. The ``mail.send`` handler is a
module-level function the dispatcher routes to with
``connector_instance=None`` / ``target=None`` — recipients are params,
not a registered ``Target``.

Importing the package (the lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
pass walks ``connectors/<product>/`` and imports each subpackage)
queues the ``mail.send`` typed-op upsert onto the lifespan-driven
registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
so the ``endpoint_descriptor`` row lands before the first dispatch.

The package's second export surface is
:func:`~meho_backplane.connectors.mail.transport.send_email` — the
shared transport the checks notifier (#2719) imports directly, so
notification delivery and agent-initiated sends run the identical
allowlist floor (``MAIL_RECIPIENT_ALLOWLIST``, empty ⇒ inert) and
return-failures contract. See
:mod:`meho_backplane.connectors.mail.ops`,
:mod:`meho_backplane.connectors.mail.transport`, and
:mod:`meho_backplane.connectors.mail.allowlist`.
"""

from meho_backplane.connectors.mail.ops import register_mail_typed_operations
from meho_backplane.connectors.mail.transport import MailSendResult, send_email
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_typed_op_registrar(register_mail_typed_operations)

__all__ = [
    "MailSendResult",
    "register_mail_typed_operations",
    "send_email",
]
