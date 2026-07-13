# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.net — network-diagnostics probes (Initiative #2405).

A **synthetic** connector subpackage in the ``secret.*`` mold: no vendor
connector backs it, so this package calls neither ``register_connector``
nor ``register_connector_v2``. The ``net.*`` handlers are module-level
functions the dispatcher routes to with ``connector_instance=None`` /
``target=None`` — the probe destination is a param, not a registered
``Target``.

Importing the package (the lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors` pass
walks ``connectors/<product>/`` and imports each subpackage) queues the
``net.tcp_check`` typed-op upsert onto the lifespan-driven registrar list
via :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
so the ``endpoint_descriptor`` row lands before the first dispatch.

This is the keystone (#2406) of the ``net.*`` family: ``net.tcp_check``
plus the sibling ops (T2-T4) reuse — the dedicated probe allowlist
(``MEHO_NETDIAG_PROBE_ALLOWLIST``, empty ⇒ inert), the audit-visible
host:port, and the return-failures contract (a failed probe is
``status="ok"`` with ``connected=false``, never a ``connector_*``
error). ``net.tls_inspect`` (T2, #2407) is the first sibling: it reuses
all three foundations to report the full presented TLS certificate chain
on an unverified handshake. Each op ships its own module + registrar and
queues it here, so siblings extend this package without contending for a
single registrar function. See :mod:`meho_backplane.connectors.net.ops`,
:mod:`meho_backplane.connectors.net.tls`, and
:mod:`meho_backplane.connectors.net.allowlist`.
"""

from meho_backplane.connectors.net.http_probe import register_net_http_probe_operations
from meho_backplane.connectors.net.icmp import register_net_icmp_operations
from meho_backplane.connectors.net.ops import register_net_typed_operations
from meho_backplane.connectors.net.tls import register_net_tls_inspect_operation
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue each net.* typed-op upsert onto the lifespan-driven registrar
# list (run after the connector eager-import pass). Each sibling op has
# its own registrar so the family extends without editing a shared
# function body (net.tcp_check #2406, net.tls_inspect #2407,
# net.http_probe #2408, net.ping/trace/path_mtu #2411, …).
register_typed_op_registrar(register_net_typed_operations)
register_typed_op_registrar(register_net_tls_inspect_operation)
register_typed_op_registrar(register_net_http_probe_operations)
register_typed_op_registrar(register_net_icmp_operations)

__all__ = [
    "register_net_http_probe_operations",
    "register_net_icmp_operations",
    "register_net_tls_inspect_operation",
    "register_net_typed_operations",
]
