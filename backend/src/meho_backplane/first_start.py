# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""First-start checks: ``GET /first-start``.

Right after an install, MEHO checks its own site and returns a plain list.
Each line is green, or red with one sentence that names the fix. The five
checks are:

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
  PostgreSQL report. Red above 60 seconds of difference.
* **Reachability** - every system listed in the settings (the targets)
  answers the same fingerprint call that ``meho targets probe`` uses.
  Nothing is stored. The console page passes the signed-in person, so a
  system whose login is in Vault is checked with that person's token. The
  public list has no person: such a system is not contacted, and the line
  says how many systems could not be checked. That is not a red line.
* **Network** - the addresses the host of ``BACKPLANE_URL`` resolves to are
  not global internet addresses (``ipaddress`` ``is_global``). MEHO belongs
  on the management network. A global address gives a ``warn`` line, not a
  red one: the install goes on. Private, shared and local ranges give none.

The endpoint needs no login, like the bare ``/ready`` verdict, so the answer
holds the list only: no stack trace, no token, no exception text. A red
line names only the host from ``BACKPLANE_URL``. The reachability line shows
only counts of systems, never their names. The signed-in console page shows
the names of the systems of the signed-in tenant. The checks run again on
every call; calls that arrive while a run is in progress share that run.
The checks change nothing.

A line has one of four states: ``ok``, ``fail``, ``warn`` or ``skipped``. A check is
skipped when it cannot run or has no meaning (the certificate check when
``BACKPLANE_URL`` is plain http, or when the DNS name already failed; the
reachability check when some systems need a signed-in person and none
failed). The overall ``ok`` is true when no line is ``fail``; a ``warn`` line
does not make it false.
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
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import Target
from meho_backplane.settings import get_settings

__all__ = [
    "CERT_MIN_DAYS_LEFT",
    "CLOCK_MAX_SKEW_S",
    "FirstStartLine",
    "FirstStartResponse",
    "reset_public_run_for_testing",
    "router",
    "run_first_start_checks",
    "run_public_first_start_checks",
]

_log = structlog.get_logger(__name__)

#: A certificate with fewer days left than this is red.
CERT_MIN_DAYS_LEFT: int = 30
#: A clock source that differs by more than this many seconds is red.
CLOCK_MAX_SKEW_S: float = 60.0

_NETWORK_TIMEOUT_S: float = 5.0
_TARGET_TIMEOUT_S: float = 10.0
_MAX_NAMES_SHOWN: int = 10

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


async def _keycloak_time() -> datetime | None:
    """Keycloak's time, from the ``Date`` header of its discovery answer.

    ``None`` unless the discovery answer is a success: an error page from a
    proxy in front of Keycloak carries the proxy's time, not Keycloak's.
    """
    url = f"{get_settings().keycloak_discovery_base}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=_NETWORK_TIMEOUT_S) as client:
            response = await client.get(url)
        if not response.is_success:
            return None
        header = response.headers.get("date")
        return parsedate_to_datetime(header).astimezone(UTC) if header else None
    except (httpx.HTTPError, TypeError, ValueError):
        return None


async def _database_time() -> datetime | None:
    """PostgreSQL's ``now()``. ``None`` when it cannot be read."""
    from meho_backplane.db.engine import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            value = await asyncio.wait_for(session.scalar(text("SELECT now()")), _NETWORK_TIMEOUT_S)
    except Exception:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


async def check_clock(
    *,
    keycloak_time: Callable[[], Awaitable[datetime | None]] | None = None,
    database_time: Callable[[], Awaitable[datetime | None]] | None = None,
) -> FirstStartLine:
    """The backplane's time against the time Keycloak and PostgreSQL report."""
    sources = {
        "Keycloak": keycloak_time or _keycloak_time,
        "PostgreSQL": database_time or _database_time,
    }
    readings = await asyncio.gather(*(read() for read in sources.values()))
    problems: list[str] = []
    for (label, _read), reading in zip(sources.items(), readings, strict=True):
        if reading is None:
            problems.append(
                f"MEHO could not read the time from {label}. Check that MEHO can reach it."
            )
            continue
        skew = abs((datetime.now(UTC) - reading).total_seconds())
        if skew > CLOCK_MAX_SKEW_S:
            problems.append(
                f"The clock of MEHO is {int(skew)} seconds away from the clock of {label}. "
                "Set up time sync (NTP) on the server or the cluster nodes."
            )
    if problems:
        return _line("clock", "fail", " ".join(problems))
    return _line(
        "clock",
        "ok",
        f"The clock agrees with Keycloak and PostgreSQL within {int(CLOCK_MAX_SKEW_S)} seconds.",
    )


# --- Reachability -----------------------------------------------------------


async def _probe_one(target: Target, operator: Operator | None) -> ProbeOutcome:
    """Whether the target answers the fingerprint call. Nothing is stored.

    A target whose login is in Vault (``secret_ref``) needs a person's token
    to read it. Without *operator* it is not contacted: the outcome is
    ``needs_person``, not ``down``.
    """
    from meho_backplane.connectors.resolver import resolve_connector_or_label

    if operator is None and target.secret_ref:
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


def _systems(what: str, names: list[str], total: int, *, show_names: bool) -> str:
    """``"These systems <what>: a, b."`` with names, or ``"N of M systems <what>."``."""
    if not show_names:
        return f"{len(names)} of {total} systems {what}."
    shown = ", ".join(names[:_MAX_NAMES_SHOWN])
    more = f" and {len(names) - _MAX_NAMES_SHOWN} more" if len(names) > _MAX_NAMES_SHOWN else ""
    return f"These systems {what}: {shown}{more}."


def _reachability_line(
    total: int, down: list[str], unchecked: list[str], *, show_names: bool
) -> FirstStartLine:
    """Build the reachability line from the systems that are down or unchecked."""
    unchecked_note = (
        _systems(
            "could not be checked without a signed-in person, because their login is in Vault",
            unchecked,
            total,
            show_names=show_names,
        )
        + " Sign in to the console to check them."
    )
    if down:
        message = (
            _systems("did not answer", down, total, show_names=show_names)
            + ("" if show_names else " Sign in to the console to see which ones.")
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


async def check_reachability(
    *,
    tenant_id: uuid.UUID | None = None,
    show_names: bool = False,
    operator: Operator | None = None,
    list_targets: Callable[[uuid.UUID | None], Awaitable[list[Target]]] | None = None,
    probe_one: Callable[[Target, Operator | None], Awaitable[ProbeOutcome]] | None = None,
) -> FirstStartLine:
    """Every system in the settings answers the same call as ``meho targets probe``.

    A red line names the systems only when *show_names* is true. *operator*
    is the signed-in person whose token reads a login from Vault; without
    one, such a system is not checked.
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
    outcomes = await asyncio.gather(*(probe(t, operator) for t in targets))
    paired = list(zip(targets, outcomes, strict=True))
    down = sorted(t.name for t, outcome in paired if outcome == "down")
    unchecked = sorted(t.name for t, outcome in paired if outcome == "needs_person")
    return _reachability_line(len(targets), down, unchecked, show_names=show_names)


# --- The list ---------------------------------------------------------------


async def run_first_start_checks(
    *,
    tenant_id: uuid.UUID | None = None,
    show_names: bool = False,
    operator: Operator | None = None,
) -> FirstStartResponse:
    """Run the checks and return the list. Changes nothing.

    The console page passes its tenant, ``show_names=True`` and the
    signed-in person. The public endpoint passes none of them, so it shows
    counts only, and it does not contact a system whose login is in Vault.
    """
    backplane_url = get_settings().backplane_url
    dns = await check_dns(backplane_url)
    certificate, network, clock, reachability = await asyncio.gather(
        check_certificate(backplane_url, dns_ok=dns.ok),
        check_network(backplane_url, dns_ok=dns.ok),
        check_clock(),
        check_reachability(tenant_id=tenant_id, show_names=show_names, operator=operator),
    )
    dns = await check_destination(backplane_url, dns, certificate)
    lines = [dns, certificate, network, clock, reachability]
    return FirstStartResponse(ok=all(line.ok for line in lines), checks=lines)


#: Seconds the public list keeps the result of its last run.
_PUBLIC_RESULT_HOLD_S: float = 30.0

_public_run: asyncio.Task[FirstStartResponse] | None = None
_public_run_ended_at: float = 0.0


def reset_public_run_for_testing() -> None:
    """Forget the public run and its kept result."""
    global _public_run, _public_run_ended_at
    _public_run = None
    _public_run_ended_at = 0.0


async def _public_run_once() -> FirstStartResponse:
    global _public_run_ended_at
    try:
        return await run_first_start_checks()
    finally:
        _public_run_ended_at = time.monotonic()


async def run_public_first_start_checks() -> FirstStartResponse:
    """The public list: counts only, from at most one run per hold time.

    Calls that arrive while a run is in progress share that run instead of
    starting one more. After the run ends, its result is kept for
    ``_PUBLIC_RESULT_HOLD_S`` seconds: calls in that time get it and start
    no run. The first call after that starts a new run. A run that ended
    with an error is not kept: the next call starts a new run.
    """
    global _public_run
    if _public_run is None or (
        _public_run.done()
        and (
            _public_run.cancelled()
            or _public_run.exception() is not None
            or time.monotonic() - _public_run_ended_at >= _PUBLIC_RESULT_HOLD_S
        )
    ):
        _public_run = asyncio.create_task(_public_run_once())
    return await asyncio.shield(_public_run)


@router.get("/first-start", response_model=FirstStartResponse)
async def first_start() -> FirstStartResponse:
    """The first-start list. No login: it shows the verdict lines only."""
    return await run_public_first_start_checks()


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
