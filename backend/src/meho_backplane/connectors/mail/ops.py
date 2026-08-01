# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``mail.send`` typed op + registrar for the synthetic ``mail.*`` product.

``mail.*`` is a **synthetic** typed-op product on the ``net.*`` mould
(#2717): no vendor connector backs it, so the package calls neither
``register_connector`` nor ``register_connector_v2``. The op is
registered under the natural key
``(product="mail", version="1.x", impl_id="mail-smtp")``, so the wire
``connector_id`` is ``mail-smtp-1.x`` — which round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id` back to
``("mail", "1.x", "mail-smtp")``. The handler is a **module-level**
function, so the dispatcher resolves it with ``connector_instance=None``
and ``target=None`` — recipients are params, not a registered
``Target``.

The handler is a thin adapter over
:func:`~meho_backplane.connectors.mail.transport.send_email` — the
single shared transport the checks notifier (#2719) also imports — so
the recipient-allowlist floor, the unconfigured refusal, and the
return-failures contract live in exactly one place. The handler's
return dict carries the literal ``to``/``subject`` (never the body), so
the durable audit row's ``raw_payload`` answers "who was mailed about
what" without persisting message content.

``safety_level="caution"`` + ``requires_approval=False``: sending mail
is an outward-facing side effect, so operators (human/service
principals, default-allow) auto-run it while agent principals land on
the ``caution`` needs-approval default of the policy gate
(:mod:`meho_backplane.auth.permissions`) — and the recipient allowlist
is the hard floor either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.mail.transport import send_email
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "MAIL_SEND_PARAMETER_SCHEMA",
    "mail_send",
    "register_mail_typed_operations",
]

#: Every character :meth:`str.splitlines` treats as a line boundary,
#: excluded from ``subject`` so the schema layer — not the transport —
#: owns header-injection rejection.
#:
#: :class:`~email.message.EmailMessage` guards header assignment with
#: ``len(value.splitlines()) > 1`` and raises :class:`ValueError`, which
#: would escape :func:`~meho_backplane.connectors.mail.transport.send_email`
#: as a ``connector_error`` and break the return-failures contract.
#: ``splitlines`` breaks on far more than CR/LF (VT, FF, the three
#: information separators, NEL, and the Unicode line/paragraph
#: separators), so a ``[^\r\n]`` class would leave the hole half-open;
#: this class is the exact complement of the stdlib guard, so a
#: rejected subject is always ``invalid_params``.
_SINGLE_LINE_PATTERN: Final[str] = r"^[^\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]+$"

MAIL_SEND_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "string",
                "minLength": 3,
                "maxLength": 254,
                "pattern": r"^[^@\s]+@[^@\s]+$",
                "description": (
                    "Recipient email address. Must be covered by "
                    "MAIL_RECIPIENT_ALLOWLIST (full address or its "
                    "domain) or the whole send is refused before any "
                    "SMTP connection opens."
                ),
            },
            "description": "Recipient addresses; one refused recipient refuses the whole send.",
        },
        "subject": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "pattern": _SINGLE_LINE_PATTERN,
            "description": (
                "Subject line (plain text, single line). A line break of "
                "any kind is rejected as invalid_params — it would be a "
                "header-injection attempt."
            ),
        },
        "body": {
            "type": "string",
            "maxLength": 100_000,
            "description": "Plain-text message body (no HTML, no attachments).",
        },
    },
    "required": ["to", "subject", "body"],
    "additionalProperties": False,
}

_MAIL_SEND_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sent": {
            "type": "boolean",
            "description": "True iff the MTA accepted the message for delivery.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise a failure code: "
                "not_in_recipient_allowlist, smtp_unconfigured, "
                "smtp_connect_error, smtp_auth_requires_tls, "
                "smtp_auth_error, smtp_recipients_refused, smtp_error."
            ),
        },
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The requested recipients (audit-visible).",
        },
        "subject": {
            "type": "string",
            "description": "The subject line (audit-visible; the body is never persisted).",
        },
    },
    "required": ["sent", "reason", "to", "subject"],
    "additionalProperties": False,
}

_MAIL_SEND_WHEN_TO_USE = (
    "Send a plain-text email to operator-allowlisted recipients from the "
    "backplane's deployment-level SMTP block — e.g. to deliver an "
    "investigation finding, a maintenance notice, or an alert summary to "
    "an on-call mailbox. An outward-facing side effect: recipients must "
    "be inside MAIL_RECIPIENT_ALLOWLIST, and a refused or failed send is "
    "a normal result (sent=false with a reason), not an error."
)

_MAIL_SEND_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to deliver a short plain-text message to a human mailbox "
        "when the operator has asked for email delivery. Recipients must "
        "be allowlisted at the deployment level; do not retry a "
        "not_in_recipient_allowlist, smtp_unconfigured, or "
        "smtp_auth_requires_tls refusal — all three are deployment "
        "configuration, not transient state."
    ),
    "parameter_hints": {
        "to": "Required. 1-32 recipient addresses; every one must be allowlisted.",
        "subject": (
            "Required. Single-line subject (max 200 chars); a line break "
            "of any kind is rejected as invalid_params."
        ),
        "body": "Required. Plain-text body; no HTML or attachments.",
    },
    "output_shape": (
        "On success: {'sent': true, 'reason': null, 'to': [<str>], "
        "'subject': <str>}. On a refused or failed send: {'sent': false, "
        "'reason': '<not_in_recipient_allowlist|smtp_unconfigured|"
        "smtp_connect_error|smtp_auth_requires_tls|smtp_auth_error|"
        "smtp_recipients_refused|smtp_error>', 'to': [<str>], "
        "'subject': <str>} — still a "
        "successful (status=ok) op."
    ),
}


async def mail_send(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Send one plain-text email through the shared transport.

    Op-id: ``mail.send``. Synthetic typed op (no vendor connector,
    ``target`` is always ``None``). The dispatcher has validated the
    param schema, so ``to`` / ``subject`` / ``body`` are present and
    well-typed.

    Delegates to
    :func:`~meho_backplane.connectors.mail.transport.send_email` (the
    allowlist floor and refusal mapping live there) and adapts the
    :class:`~meho_backplane.connectors.mail.transport.MailSendResult`
    into the audit-visible payload: the returned dict carries the
    literal ``to``/``subject`` — the durable audit row's ``raw_payload``
    answer to "who was mailed" — and never the body.
    """
    to = [str(address) for address in params["to"]]
    subject = str(params["subject"])
    result = await send_email(to=to, subject=subject, body=str(params["body"]))
    return {
        "sent": result.sent,
        "reason": result.reason,
        "to": to,
        "subject": subject,
    }


async def register_mail_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``mail.*`` typed ops into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline. Registers
    ``mail.send`` (#2717) as ``caution`` + ``requires_approval=False``
    — operators auto-run, agents ride the policy gate's needs-approval
    default; the recipient allowlist is the hard floor either way.
    """
    await register_typed_operation(
        product="mail",
        version="1.x",
        impl_id="mail-smtp",
        op_id="mail.send",
        handler=mail_send,
        group_key="send",
        when_to_use=_MAIL_SEND_WHEN_TO_USE,
        summary="Send a plain-text email to allowlisted recipients via the deployment SMTP block.",
        description=(
            "Delivers a plain-text email through the deployment-level "
            "SMTP configuration (MAIL_SMTP_HOST/PORT, STARTTLS or "
            "implicit TLS on port 465, optional LOGIN auth that only "
            "ever runs on an encrypted channel). Every "
            "recipient must be inside MAIL_RECIPIENT_ALLOWLIST or the "
            "send is refused before any SMTP connection opens (empty "
            "allowlist ⇒ the connector is inert). An unconfigured SMTP "
            "block, a refused recipient, or a delivery failure returns "
            "sent=false with a stable reason code and status=ok — a "
            "failed send is the product, never a connector error. The "
            "message body is never persisted; the audit row records "
            "recipients and subject only."
        ),
        parameter_schema=MAIL_SEND_PARAMETER_SCHEMA,
        response_schema=_MAIL_SEND_RESPONSE_SCHEMA,
        tags=["mail", "smtp", "send", "notify", "write"],
        safety_level="caution",
        requires_approval=False,
        llm_instructions=_MAIL_SEND_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
