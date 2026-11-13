# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Trust for ``net.http_probe``: use the CA pin of a registered target.

A service with a private or demo CA fails the default TLS check, so the
probe reads ``tls_error``. This module lets the probe trust the CA that
an operator already pinned on a registered target (``Target.tls_ca_pin``),
with certificate and host name checks still on.

Rules:

* The agent supplies no certificate. Only a pin that an operator
  registered on a target of the **same tenant** is used.
* A target matches when the probe host (or the host name in
  ``host_header``) equals the target's ``host`` or ``fqdn``, ignoring
  case. Soft-deleted targets and targets without a pin never match.
* The pin is used for the first host only. A redirect to another host
  is checked with the default trust again.
* The result says which trust was used (``default`` or ``pinned``) and
  names the target.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select

from meho_backplane.connectors.net.allowlist import (
    ProbeNotAllowedError,
    assert_probe_allowed,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target

__all__ = [
    "TRUST_RESPONSE_PROPERTIES",
    "TRUST_RESPONSE_REQUIRED",
    "ProbeTrust",
    "resolve_probe_trust",
]

TRUST_DEFAULT: Final[str] = "default"
TRUST_PINNED: Final[str] = "pinned"

#: Response-schema entries for the two new result keys.
TRUST_RESPONSE_PROPERTIES: Final[dict[str, Any]] = {
    "trust": {
        "type": "string",
        "enum": [TRUST_DEFAULT, TRUST_PINNED],
        "description": (
            "Which certificate trust the probe used for the first host: "
            "'pinned' when it used the CA pin of a registered target of "
            "this tenant, 'default' otherwise. A pin is never used after a "
            "redirect to another host."
        ),
    },
    "trust_target": {
        "type": ["string", "null"],
        "description": "Name of the target whose CA pin was used; null when trust is 'default'.",
    },
}
TRUST_RESPONSE_REQUIRED: Final[tuple[str, ...]] = ("trust", "trust_target")


@dataclass(frozen=True)
class ProbeTrust:
    """The trust choice for one probe call."""

    ca_pem: str | None = None
    target_name: str | None = None

    @property
    def pinned(self) -> bool:
        return self.ca_pem is not None

    def result_fields(self) -> dict[str, Any]:
        return {
            "trust": TRUST_PINNED if self.pinned else TRUST_DEFAULT,
            "trust_target": self.target_name,
        }

    def build_ssl_context(self) -> ssl.SSLContext:
        """Default trust plus the pinned CA, with verification still on.

        ``VERIFY_X509_STRICT`` is cleared for the same reason as in the
        connector HTTP adapter: private and demo CAs often lack an
        Authority Key Identifier. Chain and host name checks stay on.
        """
        assert self.ca_pem is not None
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        ctx.load_verify_locations(cadata=self.ca_pem)
        return ctx


def _hostname_of(host_header: str | None) -> str | None:
    """Host name part of a ``hostname[:port]`` value, or ``None``."""
    if not host_header:
        return None
    host, sep, port = host_header.rpartition(":")
    if sep and port.isdigit() and host:
        return host
    return host_header


async def resolve_probe_trust(tenant_id: UUID, params: dict[str, Any]) -> ProbeTrust:
    """Pick the trust for a probe: a target's CA pin, or the default.

    Returns the default trust (no DB read) for a plain-http or invalid
    URL, and for a host the allowlist refuses: the handler refuses it
    itself, and no registry lookup should run for it.
    """
    try:
        parsed = httpx.URL(str(params["url"]))
    except (httpx.InvalidURL, ValueError):
        return ProbeTrust()
    if parsed.scheme != "https" or not parsed.host:
        return ProbeTrust()
    try:
        assert_probe_allowed(parsed.host, tenant_id=tenant_id)
    except ProbeNotAllowedError:
        return ProbeTrust()

    names = {parsed.host.lower()}
    vhost = _hostname_of(str(params["host_header"]) if params.get("host_header") else None)
    if vhost:
        names.add(vhost.lower())

    stmt = (
        select(Target.name, Target.tls_ca_pin, Target.port)
        .where(
            Target.tenant_id == tenant_id,
            Target.deleted_at.is_(None),
            Target.tls_ca_pin.is_not(None),
            or_(func.lower(Target.host).in_(names), func.lower(Target.fqdn).in_(names)),
        )
        .order_by(Target.name)
    )
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).all()
    if not rows:
        return ProbeTrust()
    # Prefer the target registered with the probed port; else the first by name.
    chosen = next((r for r in rows if r.port is not None and r.port == parsed.port), rows[0])
    return ProbeTrust(ca_pem=chosen.tls_ca_pin, target_name=chosen.name)
