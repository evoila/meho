# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""First-start checks: ``GET /api/v1/first-start`` (signed in) and ``GET /first-start``.

Right after an install, MEHO checks its own site and returns a plain list.
Each line is green, or red with one sentence that names the fix. The list
is for people: it needs a person's login (not an agent, service or runner
login) with the admin role of the tenant, or a platform admin. Others get
403. ``GET /first-start`` needs no login and answers one fixed line that
says how to sign in, with ``ok`` false. It runs no check and contacts no
system, so it tells a caller nothing about the install. The five checks
are:

* **DNS name** - the host of ``BACKPLANE_URL`` resolves, and the address it
  points at answers as this MEHO. For that, MEHO calls its own
  ``GET /first-start/destination`` through ``BACKPLANE_URL`` with a random
  nonce and checks the proof in the answer. The proof is a keyed hash under
  a key of this install, so another server, or another MEHO, cannot give it.
  The key comes from ``PARAMS_HASH_KEY`` only. Without it, the destination
  route answers 404 and the DNS line is a ``warn`` that names the setting.
* **Certificate** - MEHO opens a TLS connection to ``BACKPLANE_URL`` and
  checks the chain, the name and the end date (at least 30 days left).
* **Clock** - the time of this process against the time Keycloak and
  PostgreSQL report. Red above 60 seconds of difference. Keycloak may answer
  without a ``Date`` header (the core's Keycloak does). Then its time is not
  compared, the line says so, and that alone does not make the line red.
* **Reachability** - every system listed in the settings (the targets)
  answers the same fingerprint call that ``meho targets probe`` uses.
  Nothing is stored. The list covers the systems of the signed-in tenant. A
  platform admin sees the systems of all tenants, and then the line names
  the tenant of each system. The checks run with the signed-in person, so
  a system whose login is in Vault is checked with that person's token. A
  system whose login is in Vault is not contacted when it belongs to
  another tenant: the line names the systems that could not be checked.
  That is not a red line. A login in Google Secret Manager is read with
  MEHO's own Google identity, or with the person's token when
  ``GSM_WIF_AUDIENCE`` is set, so such a system is checked in every
  tenant. At most ``_MAX_PROBES_AT_ONCE`` systems are contacted at the same
  time.
* **Network** - the addresses the host of ``BACKPLANE_URL`` resolves to are
  not global internet addresses (``ipaddress`` ``is_global``). MEHO belongs
  on the management network. A global address gives a ``warn`` line, not a
  red one: the install goes on. Private, shared and local ranges give none.

The answer holds the list only: no stack trace, no token, no exception
text. The reachability line names the systems that did not answer. The
checks change nothing. The list runs at most once at a time for each
tenant: calls that arrive during a run share it, and the result is kept
for ``_RESULT_HOLD_S`` seconds (as ``/ready`` keeps its result).

A line has one of four states: ``ok``, ``fail``, ``warn`` or ``skipped``. A check is
skipped when it cannot run or has no meaning (the certificate check when
``BACKPLANE_URL`` is plain http, or when the DNS name already failed; the
reachability check when some systems could not be checked and none
failed). The overall ``ok`` is true when no line is ``fail``; a ``warn`` line
does not make it false. The fixed answer without a login has ``ok`` false,
so a script never sees green when no check ran.
"""

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import secrets
import socket
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.db.models import Target, Tenant
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.settings import get_settings

__all__ = [
    "ADMIN_PERSON_REQUIRED",
    "CERT_MIN_DAYS_LEFT",
    "CLOCK_MAX_SKEW_S",
    "SIGN_IN_ANSWER",
    "FirstStartLine",
    "FirstStartResponse",
    "clear_first_start_cache",
    "first_start_list",
    "may_see_first_start",
    "reachability_scope",
    "router",
    "run_first_start_checks",
]

_log = structlog.get_logger(__name__)

#: A certificate with fewer days left than this is red.
CERT_MIN_DAYS_LEFT: int = 30
#: A clock source that differs by more than this many seconds is red.
CLOCK_MAX_SKEW_S: float = 60.0

_NETWORK_TIMEOUT_S: float = 5.0
_TARGET_TIMEOUT_S: float = 10.0
_MAX_NAMES_SHOWN: int = 10
#: At most this many systems are contacted at the same time.
_MAX_PROBES_AT_ONCE: int = 10
#: A finished list is kept this many seconds for later calls of the same tenant.
_RESULT_HOLD_S: float = 10.0

# The destination proof: a keyed hash of a random nonce under a key of this
# install. The label keeps the key apart from every other key made from the
# same secret.
_DESTINATION_KEY_LABEL: bytes = b"meho-first-start-destination-v1"
_NONCE_BYTES: int = 16
_NONCE_PATTERN: str = r"^[0-9a-f]{32}$"

# OpenSSL verify codes (X509_V_ERR_*) that get their own fix sentence.
_VERIFY_EXPIRED: int = 10
_VERIFY_HOSTNAME_MISMATCH: int = 62
_VERIFY_IP_MISMATCH: int = 64

LineStatus = Literal["ok", "fail", "warn", "skipped"]
ProbeOutcome = Literal["up", "down", "needs_person"]


class FirstStartLine(BaseModel):
    """One line of the first-start list."""

    name: str
    status: LineStatus
    message: str

    @property
    def ok(self) -> bool:
        """A ``warn`` line does not fail the list."""
        return self.status != "fail"


class FirstStartResponse(BaseModel):
    """The whole list. ``ok`` is true when no line is ``fail`` (``warn`` is fine)."""

    ok: bool
    checks: list[FirstStartLine]


class DestinationProof(BaseModel):
    """The answer of ``GET /first-start/destination``: the proof for the nonce."""

    proof: str


router = APIRouter(tags=["health"])


def _line(name: str, status: LineStatus, message: str) -> FirstStartLine:
    return FirstStartLine(name=name, status=status, message=message)


def _site_host_port(backplane_url: str) -> tuple[str, int, str] | None:
    """Return ``(host, port, scheme)`` of *backplane_url*, or ``None`` when unusable."""
    try:
        parts = urlsplit(backplane_url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host or parts.scheme not in {"http", "https"}:
        return None
    return host, port or (443 if parts.scheme == "https" else 80), parts.scheme


# --- DNS name ---------------------------------------------------------------


async def _resolve_host(host: str, port: int) -> list[str]:
    """Return the addresses *host* resolves to. Raises ``OSError`` when none."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), _NETWORK_TIMEOUT_S
    )
    return sorted({str(info[4][0]) for info in infos})


async def check_dns(backplane_url: str) -> FirstStartLine:
    """The host of ``BACKPLANE_URL`` resolves.

    :func:`check_destination` completes this line after the certificate
    check: the address the name points at must answer as this MEHO.
    """
    site = _site_host_port(backplane_url)
    if site is None:
        return _line(
            "dns",
            "fail",
            "BACKPLANE_URL is not set or is not a web address. "
            "Set it to the address people use to reach MEHO, for example https://meho.example.com.",
        )
    host, port, _scheme = site
    try:
        addresses = await _resolve_host(host, port)
    except (OSError, TimeoutError):
        addresses = []
    if not addresses:
        return _line(
            "dns",
            "fail",
            f"The host name {host} does not resolve. Add it to DNS, or fix BACKPLANE_URL.",
        )
    return _line("dns", "ok", f"The host name {host} resolves.")


def _destination_key() -> bytes | None:
    """The key of the destination proof, or ``None`` without ``PARAMS_HASH_KEY``."""
    from meho_backplane.operations._validate import InstallKeyMissingError, derive_install_key

    try:
        return derive_install_key(_DESTINATION_KEY_LABEL)
    except InstallKeyMissingError:
        return None


def _destination_proof(nonce: str) -> str | None:
    """The proof this install gives for *nonce*, or ``None`` without ``PARAMS_HASH_KEY``."""
    key = _destination_key()
    if key is None:
        return None
    return hmac.new(key, nonce.encode("ascii"), hashlib.sha256).hexdigest()


async def _fetch_destination_proof(backplane_url: str, nonce: str) -> str | None:
    """Ask the address behind ``BACKPLANE_URL`` for the proof of *nonce*.

    ``None`` when the address does not answer, answers with an error, or
    answers with something that is not a proof.
    """
    url = f"{backplane_url.strip().rstrip('/')}/first-start/destination"
    try:
        async with httpx.AsyncClient(timeout=_NETWORK_TIMEOUT_S, verify=_trust_context()) as client:
            response = await client.get(url, params={"nonce": nonce})
        if not response.is_success:
            return None
        proof = response.json().get("proof")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return proof if isinstance(proof, str) else None


async def check_destination(
    backplane_url: str, dns: FirstStartLine, certificate: FirstStartLine
) -> FirstStartLine:
    """Complete the DNS line: the address the name points at answers as this MEHO.

    Runs after the certificate check, so a bad certificate gives one red
    line (the certificate), not two. A red DNS line from the resolve step
    stays as it is. Without ``PARAMS_HASH_KEY`` there is no proof to compare,
    so the line is a ``warn`` and MEHO does not call the address.
    """
    site = _site_host_port(backplane_url)
    if site is None or not dns.ok:
        return dns
    host = site[0]
    if certificate.status == "fail":
        return _line(
            "dns",
            "ok",
            f"The host name {host} resolves. "
            "Where it points is checked once the certificate check is green.",
        )
    nonce = secrets.token_hex(_NONCE_BYTES)
    expected = _destination_proof(nonce)
    if expected is None:
        return _line(
            "dns",
            "warn",
            f"The host name {host} resolves. Where it points is not checked, because "
            "PARAMS_HASH_KEY is not set. Set it to at least 32 random characters "
            "(chart: paramsHashKey.secretName).",
        )
    proof = await _fetch_destination_proof(backplane_url, nonce)
    if proof is None or not hmac.compare_digest(proof, expected):
        return _line(
            "dns",
            "fail",
            f"The host name {host} resolves, but the address it points at does not answer "
            "as this MEHO. Point the name at the address MEHO is served on, or fix BACKPLANE_URL.",
        )
    return _line("dns", "ok", f"The host name {host} resolves and points at this MEHO.")


# --- Network ----------------------------------------------------------------


def _global_addresses(addresses: list[str]) -> list[str]:
    """The entries of *addresses* that are global internet addresses."""
    found: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        if address.is_global:
            found.append(str(address))
    return found


async def check_network(backplane_url: str, *, dns_ok: bool) -> FirstStartLine:
    """Warn when the host of ``BACKPLANE_URL`` resolves to a global internet address.

    MEHO runs on the management network, never on the internet. This is a
    warning, not a failure. Private, shared (100.64.0.0/10) and local ranges
    give no warning.
    """
    site = _site_host_port(backplane_url)
    if site is None or not dns_ok:
        return _line("network", "skipped", "Not checked until the DNS name check is green.")
    host, port, _scheme = site
    try:
        addresses = await _resolve_host(host, port)
    except (OSError, TimeoutError):
        return _line("network", "skipped", "Not checked until the DNS name check is green.")
    public = _global_addresses(addresses)
    if public:
        return _line(
            "network",
            "warn",
            f"The host name {host} points at a public internet address. "
            "MEHO belongs on the management network, never on the internet. "
            "Point the name at a private address, or close the public access "
            "with a firewall.",
        )
    return _line("network", "ok", f"The host name {host} points at a private address.")


# --- Certificate ------------------------------------------------------------


def _trust_context() -> ssl.SSLContext:
    """TLS client context with the system trust bundle (``SSL_CERT_FILE`` is honoured)."""
    return ssl.create_default_context()


async def _fetch_peer_certificate(host: str, port: int) -> dict[str, object]:
    """Open TLS to ``host:port`` with full verification; return the peer certificate.

    Raises :class:`ssl.SSLCertVerificationError` when the chain, the name or
    the dates do not verify, and ``OSError`` / ``TimeoutError`` when the
    connection fails.
    """
    _reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=_trust_context(), server_hostname=host),
        _NETWORK_TIMEOUT_S,
    )
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        cert = ssl_object.getpeercert() if ssl_object is not None else None
        return dict(cert) if cert else {}
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def _cert_verify_failure(host: str, exc: ssl.SSLCertVerificationError) -> str:
    if exc.verify_code == _VERIFY_EXPIRED:
        return f"The certificate of {host} has expired. Renew it and install the new one."
    if exc.verify_code in {_VERIFY_HOSTNAME_MISMATCH, _VERIFY_IP_MISMATCH}:
        return (
            f"The certificate does not match the host name {host}. "
            "Use a certificate that lists this name, or fix BACKPLANE_URL."
        )
    return (
        f"The certificate of {host} is not trusted. "
        "Install the full chain, and add your own CA to the trust bundle of MEHO."
    )


async def check_certificate(backplane_url: str, *, dns_ok: bool) -> FirstStartLine:
    """MEHO's own certificate, fetched through ``BACKPLANE_URL``."""
    site = _site_host_port(backplane_url)
    if site is None or not dns_ok:
        return _line("certificate", "skipped", "Not checked until the DNS name check is green.")
    host, port, scheme = site
    if scheme != "https":
        return _line(
            "certificate",
            "skipped",
            "BACKPLANE_URL uses http, so there is no certificate to check. "
            "Use https for a production site.",
        )
    try:
        cert = await _fetch_peer_certificate(host, port)
    except ssl.SSLCertVerificationError as exc:
        return _line("certificate", "fail", _cert_verify_failure(host, exc))
    except ssl.SSLError:
        return _line(
            "certificate",
            "fail",
            f"MEHO could not set up a secure connection to {host}. "
            "Check that the address serves TLS on this port.",
        )
    except (OSError, TimeoutError):
        return _line(
            "certificate",
            "fail",
            f"MEHO could not connect to {host} on port {port}. "
            "Check that MEHO can reach its own address, and fix BACKPLANE_URL if it is wrong.",
        )
    not_after = cert.get("notAfter")
    if not isinstance(not_after, str):
        return _line(
            "certificate",
            "fail",
            f"The certificate of {host} has no end date MEHO can read. "
            "Install a standard certificate.",
        )
    days_left = int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
    if days_left < CERT_MIN_DAYS_LEFT:
        return _line(
            "certificate",
            "fail",
            f"The certificate of {host} ends in {max(days_left, 0)} days. "
            f"Renew it so that it has more than {CERT_MIN_DAYS_LEFT} days left.",
        )
    return _line(
        "certificate", "ok", f"The certificate of {host} is valid for {days_left} more days."
    )


# --- Clock ------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class NoClockReading:
    """Why a time source gave no time.

    With ``red`` true the clock line is red, and ``message`` names the cause
    and the fix. With ``red`` false the source is left out of the comparison,
    and ``message`` says why. That alone does not make the line red.
    """

    message: str
    red: bool = True


ClockReading = datetime | NoClockReading
ClockReader = Callable[[], Awaitable[ClockReading]]

_DATABASE_UNREAD = NoClockReading(
    message="MEHO could not read the time from PostgreSQL. Check that MEHO can reach it."
)


def _keycloak_url_setting() -> str:
    """The setting that holds the Keycloak address MEHO reads the time from."""
    return (
        "KEYCLOAK_DISCOVERY_URL" if get_settings().keycloak_discovery_url else "KEYCLOAK_ISSUER_URL"
    )


def _http_date(value: str | None) -> datetime | None:
    """An HTTP ``Date`` value as an aware UTC time. ``None`` when absent or not a date."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    # A ``-0000`` zone gives a naive value. It means UTC (RFC 5322 section 3.3),
    # so it must not be read as the local time of this process.
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _keycloak_time() -> ClockReading:
    """Keycloak's time, from the ``Date`` header of its discovery answer.

    Only a success counts: an error page from a proxy in front of Keycloak
    carries the proxy's time, not Keycloak's. Keycloak itself can answer with
    no ``Date`` header. Then MEHO has no time that Keycloak gives, so Keycloak
    is left out of the comparison instead of making the line red.
    """
    url = f"{get_settings().keycloak_discovery_base}/.well-known/openid-configuration"
    setting = _keycloak_url_setting()
    try:
        async with httpx.AsyncClient(timeout=_NETWORK_TIMEOUT_S) as client:
            response = await client.get(url)
    except httpx.TransportError:
        return NoClockReading(
            message="MEHO got no answer from Keycloak, so it could not read the time of Keycloak. "
            f"Check that Keycloak is running and that MEHO can reach the address in {setting}."
        )
    except httpx.HTTPError:
        return NoClockReading(
            message="MEHO could not read the answer of Keycloak, so it could not read the time "
            f"of Keycloak. Check that the address in {setting} is the Keycloak realm."
        )
    if not response.is_success:
        return NoClockReading(
            message=f"Keycloak answered with HTTP {response.status_code}, so MEHO could not read "
            f"the time of Keycloak. Check that Keycloak is running and that the address in "
            f"{setting} is the Keycloak realm."
        )
    reading = _http_date(response.headers.get("date"))
    if reading is None:
        return NoClockReading(
            message="The time of Keycloak was not compared, because the answer of Keycloak "
            "has no Date header that MEHO can read.",
            red=False,
        )
    return reading


async def _database_time() -> ClockReading:
    """PostgreSQL's ``now()``."""
    from meho_backplane.db.engine import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            value = await asyncio.wait_for(session.scalar(text("SELECT now()")), _NETWORK_TIMEOUT_S)
    except Exception:
        return _DATABASE_UNREAD
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return _DATABASE_UNREAD


async def check_clock(
    *,
    keycloak_time: ClockReader | None = None,
    database_time: ClockReader | None = None,
) -> FirstStartLine:
    """The backplane's time against the time Keycloak and PostgreSQL report.

    A source that gives no time makes the line red, unless its
    :class:`NoClockReading` says to leave it out. A source that is left out
    is named in the message, also on a green line.
    """
    sources = {
        "Keycloak": keycloak_time or _keycloak_time,
        "PostgreSQL": database_time or _database_time,
    }
    readings = await asyncio.gather(*(read() for read in sources.values()))
    compared: list[str] = []
    problems: list[str] = []
    notes: list[str] = []
    for label, reading in zip(sources, readings, strict=True):
        if isinstance(reading, NoClockReading):
            if reading.red:
                problems.append(reading.message)
            else:
                notes.append(reading.message)
            continue
        compared.append(label)
        skew = abs((datetime.now(UTC) - reading).total_seconds())
        if skew > CLOCK_MAX_SKEW_S:
            problems.append(
                f"The clock of MEHO is {int(skew)} seconds away from the clock of {label}. "
                "Set up time sync (NTP) on the server or the cluster nodes."
            )
    if problems:
        return _line("clock", "fail", " ".join([*problems, *notes]))
    if not compared:
        return _line("clock", "skipped", " ".join(notes))
    agreed = (
        f"The clock agrees with {' and '.join(compared)} within {int(CLOCK_MAX_SKEW_S)} seconds."
    )
    return _line("clock", "ok", " ".join([agreed, *notes]))


# --- Reachability -----------------------------------------------------------


def _login_needs_person(target: Target, operator: Operator | None) -> bool:
    """Whether MEHO leaves *target* unchecked because of where its login is.

    A login in Google Secret Manager (``gsm:``, or a ref without a scheme
    when ``CREDENTIAL_BACKEND`` is ``gsm``) is read in one of two ways:
    with MEHO's own Google identity, or, when ``GSM_WIF_AUDIENCE`` is set,
    with the caller's token (exchanged at Google for a short-lived one). In
    both setups the read does not depend on the tenant, so the system is
    checked in every tenant. A login in Vault, or in any other store, is
    read only for a person (not a machine login) signed in to the target's
    tenant.
    """
    from meho_backplane.connectors._shared.credential_backend import parse_credential_scheme

    ref = (target.secret_ref or "").strip()
    if not ref:
        return False
    if operator is None:
        return True
    if (parse_credential_scheme(ref) or get_settings().credential_backend) == "gsm":
        return False
    return not is_human_principal(operator) or target.tenant_id != operator.tenant_id


async def _probe_one(target: Target, operator: Operator | None) -> ProbeOutcome:
    """Whether the target answers the fingerprint call. Nothing is stored.

    A target whose login MEHO may not read for *operator* is not contacted:
    the outcome is ``needs_person``, not ``down`` (see :func:`_login_needs_person`).
    """
    from meho_backplane.connectors.resolver import resolve_connector_or_label

    if _login_needs_person(target, operator):
        return "needs_person"
    cls, label, _message = resolve_connector_or_label(target, for_probe=True)
    if label is not None or cls is None:
        return "down"
    try:
        fingerprint = await asyncio.wait_for(
            cls().fingerprint(target, operator=operator),
            _TARGET_TIMEOUT_S,
        )
    except Exception:
        return "down"
    return "up" if fingerprint.reachable else "down"


async def _live_targets(tenant_id: uuid.UUID | None = None) -> list[Target]:
    """Live targets. With *tenant_id*, only the targets of that tenant."""
    from meho_backplane.db.engine import get_sessionmaker

    query = select(Target).where(Target.deleted_at.is_(None))
    if tenant_id is not None:
        query = query.where(Target.tenant_id == tenant_id)
    async with get_sessionmaker()() as session:
        rows = await session.scalars(query)
        return list(rows)


async def _tenant_slugs() -> dict[uuid.UUID, str]:
    """The name (slug) of each tenant. Empty when the table cannot be read."""
    from meho_backplane.db.engine import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            rows = await session.execute(select(Tenant.id, Tenant.slug))
            return dict(rows.tuples().all())
    except Exception:
        _log.warning("first_start_tenants_unreadable", exc_info=True)
        return {}


def _label(target: Target, slugs: dict[uuid.UUID, str] | None) -> str:
    """The name of *target*; with *slugs* (a list of all tenants), also its tenant."""
    if slugs is None:
        return target.name
    return f"{target.name} (tenant {slugs.get(target.tenant_id, str(target.tenant_id))})"


def _systems(what: str, names: list[str]) -> str:
    """``"These systems <what>: a, b."``, with at most ``_MAX_NAMES_SHOWN`` names."""
    shown = ", ".join(names[:_MAX_NAMES_SHOWN])
    more = f" and {len(names) - _MAX_NAMES_SHOWN} more" if len(names) > _MAX_NAMES_SHOWN else ""
    return f"These systems {what}: {shown}{more}."


def _reachability_line(total: int, down: list[str], unchecked: list[str]) -> FirstStartLine:
    """Build the reachability line from the systems that are down or unchecked."""
    unchecked_note = (
        _systems(
            "could not be checked, because their login is in Vault and MEHO reads it "
            "only for a person signed in to their tenant",
            unchecked,
        )
        + " Sign in to that tenant to check them."
    )
    if down:
        message = (
            _systems("did not answer", down)
            + " Check that each one is switched on, that MEHO can reach its address, "
            "and that its login in MEHO is right."
        )
        if unchecked:
            message = f"{message} {unchecked_note}"
        return _line("reachability", "fail", message)
    if unchecked:
        answered = total - len(unchecked)
        return _line(
            "reachability", "skipped", f"{answered} of {total} systems answered. {unchecked_note}"
        )
    return _line("reachability", "ok", f"All {total} systems answered.")


def may_see_first_start(operator: Operator) -> bool:
    """Whether *operator* may read the first-start list.

    Only a person (not an agent, service or runner login) with the admin
    role of the tenant, or a platform admin.
    """
    is_admin = operator.tenant_role is TenantRole.TENANT_ADMIN or operator.platform_admin
    return is_admin and is_human_principal(operator)


def reachability_scope(operator: Operator) -> uuid.UUID | None:
    """The tenant whose systems the list checks; ``None`` means all tenants.

    A platform admin who is a person sees all tenants. Everyone else,
    machine logins too, sees the systems of their own tenant only.
    """
    if operator.platform_admin and is_human_principal(operator):
        return None
    return operator.tenant_id


async def check_reachability(
    *,
    operator: Operator | None = None,
    tenant_id: uuid.UUID | None = None,
    list_targets: Callable[[uuid.UUID | None], Awaitable[list[Target]]] | None = None,
    probe_one: Callable[[Target, Operator | None], Awaitable[ProbeOutcome]] | None = None,
    tenant_slugs: Callable[[], Awaitable[dict[uuid.UUID, str]]] | None = None,
) -> FirstStartLine:
    """Every system of *tenant_id* answers the same call as ``meho targets probe``.

    *tenant_id* ``None`` means the systems of all tenants; then each name in
    the line carries the tenant of the system. *operator* is the signed-in
    person whose token reads a login from Vault.
    """
    try:
        targets = await (list_targets or _live_targets)(tenant_id)
    except Exception:
        _log.warning("first_start_targets_unreadable", exc_info=True)
        return _line(
            "reachability",
            "fail",
            "MEHO could not read the list of systems from its database. Check the database check.",
        )
    if not targets:
        return _line(
            "reachability", "ok", "No systems are added yet, so there is nothing to reach."
        )
    probe = probe_one or _probe_one
    limit = asyncio.Semaphore(_MAX_PROBES_AT_ONCE)

    async def probe_limited(target: Target) -> ProbeOutcome:
        async with limit:
            return await probe(target, operator)

    outcomes = await asyncio.gather(*(probe_limited(t) for t in targets))
    slugs = await (tenant_slugs or _tenant_slugs)() if tenant_id is None else None
    paired = list(zip(targets, outcomes, strict=True))
    down = sorted(_label(t, slugs) for t, outcome in paired if outcome == "down")
    unchecked = sorted(_label(t, slugs) for t, outcome in paired if outcome == "needs_person")
    return _reachability_line(len(targets), down, unchecked)


# --- The list ---------------------------------------------------------------


async def run_first_start_checks(
    operator: Operator | None, *, tenant_id: uuid.UUID | None
) -> FirstStartResponse:
    """Run the checks and return the list. Changes nothing.

    *operator* is the signed-in person; *tenant_id* is the tenant whose
    systems are checked (``None``: all tenants, see :func:`reachability_scope`).
    """
    backplane_url = get_settings().backplane_url
    dns = await check_dns(backplane_url)
    certificate, network, clock, reachability = await asyncio.gather(
        check_certificate(backplane_url, dns_ok=dns.ok),
        check_network(backplane_url, dns_ok=dns.ok),
        check_clock(),
        check_reachability(operator=operator, tenant_id=tenant_id),
    )
    dns = await check_destination(backplane_url, dns, certificate)
    lines = [dns, certificate, network, clock, reachability]
    return FirstStartResponse(ok=all(line.ok for line in lines), checks=lines)


_ListKey = tuple[uuid.UUID | None, uuid.UUID]

#: Finished lists: key -> (monotonic time, list). See :func:`first_start_list`.
_results: dict[_ListKey, tuple[float, FirstStartResponse]] = {}
_locks: dict[_ListKey, asyncio.Lock] = {}


def clear_first_start_cache() -> None:
    """Drop the kept lists (for tests)."""
    _results.clear()
    _locks.clear()


def _fresh(key: _ListKey) -> FirstStartResponse | None:
    kept = _results.get(key)
    if kept is not None and time.monotonic() - kept[0] < _RESULT_HOLD_S:
        return kept[1]
    return None


async def first_start_list(operator: Operator) -> FirstStartResponse:
    """The first-start list for *operator*, run at most once at a time per tenant.

    The key is the tenant whose systems are checked and the tenant of the
    caller (a platform admin's list of all tenants reads the Vault logins
    of their own tenant). Calls that arrive during a run wait for it and
    get its list; a finished list is kept for ``_RESULT_HOLD_S`` seconds.
    """
    scope = reachability_scope(operator)
    key: _ListKey = (scope, operator.tenant_id)
    kept = _fresh(key)
    if kept is not None:
        return kept
    async with _locks.setdefault(key, asyncio.Lock()):
        kept = _fresh(key)
        if kept is not None:
            return kept
        result = await run_first_start_checks(operator, tenant_id=scope)
        _results[key] = (time.monotonic(), result)
        return result


#: The whole answer of ``GET /first-start``: the way to sign in, nothing else.
#: ``ok`` is false: no check ran, so a script must not read it as green.
SIGN_IN_ANSWER = FirstStartResponse(
    ok=False,
    checks=[
        FirstStartLine(
            name="sign-in",
            status="skipped",
            message="Sign in to see the first-start checks: open /ui/first-start in the console, "
            "or run meho login and then meho status --first-start.",
        )
    ],
)

#: The 403 text for a login that may not read the list.
ADMIN_PERSON_REQUIRED = (
    "first_start_needs_admin: the first-start list needs a person's login with the "
    "admin role of the tenant, or a platform admin"
)


def _require_admin_person(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    if not may_see_first_start(operator):
        raise HTTPException(status_code=403, detail=ADMIN_PERSON_REQUIRED)
    return operator


_require_admin = Depends(_require_admin_person)


@router.get("/first-start", response_model=FirstStartResponse)
async def first_start() -> FirstStartResponse:
    """No login: one fixed line that says how to sign in. Runs no check."""
    return SIGN_IN_ANSWER


@router.get("/api/v1/first-start", response_model=FirstStartResponse)
async def first_start_signed_in(operator: Operator = _require_admin) -> FirstStartResponse:
    """The first-start list for a person who is a tenant admin or a platform admin."""
    return await first_start_list(operator)


@router.get("/first-start/destination", response_model=DestinationProof)
async def first_start_destination(
    nonce: str = Query(pattern=_NONCE_PATTERN, description="32 hex characters"),
) -> DestinationProof:
    """The proof that this address serves this MEHO. Used by the DNS check only.

    404 when ``PARAMS_HASH_KEY`` is not set: the proof is made only with that key.
    """
    proof = _destination_proof(nonce)
    if proof is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return DestinationProof(proof=proof)
