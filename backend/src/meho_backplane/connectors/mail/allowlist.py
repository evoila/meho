# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recipient allowlist for the ``mail.*`` connector.

``mail.send`` gives the backplane (and, through the normal policy path,
an agent) an outward channel: it delivers operator-authored text to an
arbitrary mailbox. Unbounded, that is an exfiltration and spam primitive.
:attr:`~meho_backplane.settings.Settings.mail_recipient_allowlist`
(``MAIL_RECIPIENT_ALLOWLIST``) is the single floor that scopes it — the
same **inverted-default** shape as the ``net.*`` probe allowlist
(``connectors/net/allowlist.py``): the parsed set is the **whole
permitted recipient space**, and an **empty** value means **deny
everything**, so the connector is inert until an operator deliberately
opts recipients in. The floor applies to every caller of
:func:`~meho_backplane.connectors.mail.transport.send_email` — the
dispatch handler *and* the checks notifier (#2719) — so no code path
can mail an unlisted recipient.

Two entry shapes, comma-separated:

* a full address (``oncall@example.com``) — matches that mailbox only;
* a domain (``example.com``, leading ``@`` accepted) — matches every
  mailbox at that domain.

Matching is case-insensitive on the whole address. That folds the
local part too — RFC 5321 technically leaves local-part case to the
receiving host, but no practical mail system distinguishes
``OnCall@`` from ``oncall@``, and a case-sensitive floor would turn
an operator's capitalization habit into a silent refusal.

There is deliberately no pattern/glob dimension (#1177: one closed-set
config, no DSL) and no per-tenant allowlist — the SMTP block is
deployment-level (one MTA, one floor), per task #2717's scope.
"""

from __future__ import annotations

from meho_backplane.settings import get_settings

__all__ = [
    "RecipientNotAllowedError",
    "assert_recipient_allowed",
    "parse_recipient_allowlist",
]


class RecipientNotAllowedError(ValueError):
    """A recipient address is not inside ``MAIL_RECIPIENT_ALLOWLIST``.

    Subclasses :class:`ValueError`. The transport catches it and converts
    it into the connector's structured refusal (``{"sent": false,
    "reason": "not_in_recipient_allowlist"}``, dispatch ``status="ok"``)
    — a refused send is a normal result, never a ``connector_*`` error
    (the return-failures contract shared with ``net.*``).
    """


def _malformed_entry(entry: str) -> ValueError:
    """Build the fail-loud error for an allowlist token that cannot be used."""
    return ValueError(
        f"MAIL_RECIPIENT_ALLOWLIST entry {entry!r} is not a valid "
        "address or domain; fix the deployment configuration"
    )


def parse_recipient_allowlist() -> tuple[frozenset[str], frozenset[str]]:
    """Parse ``mail_recipient_allowlist`` into ``(addresses, domains)``.

    A token containing ``@`` in an interior position is a full address;
    a token without one (or with only a leading ``@``) is a domain. Both
    are lower-cased, and a trailing dot on a domain is stripped, so
    matching is case-insensitive and FQDN-dot-tolerant (mirrors the
    ``net.*`` hostname fold).

    A token that is not a well-formed address or domain raises
    :class:`ValueError` naming it — loud, because silently keeping an
    unusable entry misreports the permitted recipient space the operator
    explicitly opted in (same fail-fast posture as
    ``net.allowlist.parse_probe_allowlist``). Rejected: whitespace,
    more than one ``@``, an empty local part or domain part
    (``foo@``, ``@``), and a domain that folds away to nothing
    (``...``). The bare ``@`` case is the sharpest: it used to parse to
    ``domains={""}``, which matches no address yet makes the allowlist
    look non-empty — suppressing the "connector is inert" refusal
    message that would otherwise tell the operator exactly what is
    wrong.

    Reads the live :class:`~meho_backplane.settings.Settings` singleton
    per call; tests swap the value by mutating the env and clearing
    ``get_settings``'s cache, like every other settings-backed surface.
    """
    addresses: set[str] = set()
    domains: set[str] = set()
    for token in get_settings().mail_recipient_allowlist.split(","):
        entry = token.strip()
        if not entry:
            continue
        if any(ch.isspace() for ch in entry) or entry.count("@") > 1:
            raise _malformed_entry(entry)
        folded = entry.lower()
        if "@" in folded[1:]:
            local, _, domain = folded.partition("@")
            if not local or not domain:
                raise _malformed_entry(entry)
            addresses.add(folded)
            continue
        domain = folded.removeprefix("@").rstrip(".")
        if not domain:
            raise _malformed_entry(entry)
        domains.add(domain)
    return frozenset(addresses), frozenset(domains)


def assert_recipient_allowed(address: str) -> None:
    """Raise :class:`RecipientNotAllowedError` unless *address* is allowlisted.

    Called by the transport on **every** recipient of a send, *before*
    any SMTP connection opens. The decision is absolute membership in
    :func:`parse_recipient_allowlist`'s parsed set:

    * empty allowlist → always refuse (the connector is inert);
    * a listed full address → allowed verbatim (case-insensitive);
    * an address whose domain is listed → allowed.

    An address the floor cannot positively parse — empty, containing
    whitespace or control characters, or not exactly ``local@domain`` —
    is **refused**, not guessed at: only a parseable address can match
    the allowlist, which also keeps SMTP envelope injection (CR/LF in a
    recipient) structurally impossible past this gate. The message never
    echoes the parsed set (no recipient-space oracle).

    Raises:
        RecipientNotAllowedError: *address* is empty, malformed, not
            covered by the allowlist, or the allowlist is empty.
    """
    addresses, domains = parse_recipient_allowlist()
    if not addresses and not domains:
        raise RecipientNotAllowedError(
            "recipient refused: MAIL_RECIPIENT_ALLOWLIST is empty, so the "
            "mail.* connector is inert; add the address or domain to mail"
        )
    candidate = address.strip().lower()
    local, sep, domain = candidate.partition("@")
    if (
        not local
        or not sep
        or not domain
        or "@" in domain
        or any(ch.isspace() or not ch.isprintable() for ch in candidate)
    ):
        raise RecipientNotAllowedError(f"recipient refused: {address!r} is not a valid address")
    if candidate in addresses or domain.rstrip(".") in domains:
        return
    raise RecipientNotAllowedError(
        "recipient refused: address is not listed in MAIL_RECIPIENT_ALLOWLIST"
    )
