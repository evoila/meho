# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""First-start checks: ``GET /first-start``.

Right after an install, MEHO checks its own site and returns a plain list.
Each line is green, or red with one sentence that names the fix. The four
checks are:

* **DNS name** - the host of ``BACKPLANE_URL`` resolves.
* **Certificate** - MEHO opens a TLS connection to ``BACKPLANE_URL`` and
  checks the chain, the name and the end date (at least 30 days left).
* **Clock** - the time of this process against the time Keycloak and
  PostgreSQL report. Red above 60 seconds of difference.
* **Reachability** - every system listed in the settings (the targets)
  answers the same fingerprint call that ``meho targets probe`` uses.
  Nothing is stored.

The endpoint needs no login, like the bare ``/ready`` verdict, so the answer
holds the list only: no stack trace, no token, no exception text. A red
line names only the host from ``BACKPLANE_URL``. The reachability line shows
only a count of the systems that did not answer, never their names. The
signed-in console page shows the names of the systems of the signed-in
tenant. The public answer is kept for about 30 seconds, so many calls run
the checks once. The checks change nothing.

A line has one of three states: ``ok``, ``fail`` or ``skipped``. A check is
skipped when it cannot run or has no meaning (the certificate check when
``BACKPLANE_URL`` is plain http, or when the DNS name already failed). The
overall ``ok`` is true when no line is ``fail``.
"""

import asyncio
import contextlib
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
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, text

from meho_backplane.db.models import Target
from meho_backplane.settings import get_settings

__all__ = [
    "CERT_MIN_DAYS_LEFT",
    "CLOCK_MAX_SKEW_S",
    "FirstStartLine",
    "FirstStartResponse",
    "reset_public_cache_for_testing",
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
#: The public answer is reused for this many seconds.
_PUBLIC_CACHE_TTL_S: float = 30.0

# OpenSSL verify codes (X509_V_ERR_*) that get their own fix sentence.
_VERIFY_EXPIRED: int = 10
_VERIFY_HOSTNAME_MISMATCH: int = 62
_VERIFY_IP_MISMATCH: int = 64

LineStatus = Literal["ok", "fail", "skipped"]


class FirstStartLine(BaseModel):
    """One line of the first-start list."""

    name: str
    status: LineStatus
    message: str

    @property
    def ok(self) -> bool:
        return self.status != "fail"


class FirstStartResponse(BaseModel):
    """The whole list. ``ok`` is true when no line is ``fail``."""

    ok: bool
    checks: list[FirstStartLine]


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
    """The host of ``BACKPLANE_URL`` resolves."""
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
    """Keycloak's time, from the ``Date`` header of its discovery answer."""
    url = f"{get_settings().keycloak_discovery_base}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=_NETWORK_TIMEOUT_S) as client:
            response = await client.get(url)
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


async def _probe_one(target: Target) -> bool:
    """``True`` when the target answers the fingerprint call. Nothing is stored."""
    from meho_backplane.connectors.resolver import resolve_connector_or_label

    cls, label, _message = resolve_connector_or_label(target)
    if label is not None or cls is None:
        return False
    try:
        fingerprint = await asyncio.wait_for(
            cls().fingerprint(target, operator=None),
            _TARGET_TIMEOUT_S,
        )
    except Exception:
        return False
    return bool(fingerprint.reachable)


async def _live_targets(tenant_id: uuid.UUID | None = None) -> list[Target]:
    """Live targets. With *tenant_id*, only the targets of that tenant."""
    from meho_backplane.db.engine import get_sessionmaker

    query = select(Target).where(Target.deleted_at.is_(None))
    if tenant_id is not None:
        query = query.where(Target.tenant_id == tenant_id)
    async with get_sessionmaker()() as session:
        rows = await session.scalars(query)
        return list(rows)


async def check_reachability(
    *,
    tenant_id: uuid.UUID | None = None,
    show_names: bool = False,
    list_targets: Callable[[uuid.UUID | None], Awaitable[list[Target]]] | None = None,
    probe_one: Callable[[Target], Awaitable[bool]] | None = None,
) -> FirstStartLine:
    """Every system in the settings answers the same call as ``meho targets probe``.

    A red line names the systems only when *show_names* is true.
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
    answers = await asyncio.gather(*(probe(t) for t in targets))
    down = sorted(t.name for t, up in zip(targets, answers, strict=True) if not up)
    if down and not show_names:
        return _line(
            "reachability",
            "fail",
            f"{len(down)} of {len(targets)} systems did not answer. "
            "Sign in to the console to see which ones. "
            "Check that each one is switched on, that MEHO can reach its address, "
            "and that its login in MEHO is right.",
        )
    if down:
        shown = ", ".join(down[:_MAX_NAMES_SHOWN])
        more = f" and {len(down) - _MAX_NAMES_SHOWN} more" if len(down) > _MAX_NAMES_SHOWN else ""
        return _line(
            "reachability",
            "fail",
            f"These systems did not answer: {shown}{more}. "
            "Check that each one is switched on, that MEHO can reach its address, "
            "and that its login in MEHO is right.",
        )
    return _line("reachability", "ok", f"All {len(targets)} systems answered.")


# --- The list ---------------------------------------------------------------


async def run_first_start_checks(
    *, tenant_id: uuid.UUID | None = None, show_names: bool = False
) -> FirstStartResponse:
    """Run the four checks and return the list. Changes nothing.

    The console page passes its tenant and ``show_names=True``. The public
    endpoint passes neither, so it shows counts only.
    """
    backplane_url = get_settings().backplane_url
    dns = await check_dns(backplane_url)
    certificate, clock, reachability = await asyncio.gather(
        check_certificate(backplane_url, dns_ok=dns.ok),
        check_clock(),
        check_reachability(tenant_id=tenant_id, show_names=show_names),
    )
    lines = [dns, certificate, clock, reachability]
    return FirstStartResponse(ok=all(line.ok for line in lines), checks=lines)


_public_cache: tuple[float, FirstStartResponse] | None = None
_public_lock: asyncio.Lock | None = None


def reset_public_cache_for_testing() -> None:
    """Forget the kept public answer."""
    global _public_cache, _public_lock
    _public_cache = None
    _public_lock = None


async def run_public_first_start_checks() -> FirstStartResponse:
    """The public list: counts only, and reused for about 30 seconds.

    One run at a time: calls that arrive while the checks run wait for that run.
    """
    global _public_cache, _public_lock
    if _public_lock is None:
        _public_lock = asyncio.Lock()
    async with _public_lock:
        now = time.monotonic()
        if _public_cache is not None and now - _public_cache[0] < _PUBLIC_CACHE_TTL_S:
            return _public_cache[1]
        result = await run_first_start_checks()
        _public_cache = (time.monotonic(), result)
        return result


@router.get("/first-start", response_model=FirstStartResponse)
async def first_start() -> FirstStartResponse:
    """The first-start list. No login: it shows the verdict lines only."""
    return await run_public_first_start_checks()
