# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Certificate changes of an SRM appliance: the signing request and the certificates.

* ``srm.appliance.csr.create`` -- ``POST /appliance/certificates/actions/generate-csr``.
  The appliance makes a new key and a certificate signing request (CSR) and
  returns the request. The key stays on the appliance.
* ``srm.appliance.certificate.set`` -- one of three changes:

  - ``server_certificate``: install the signed certificate for the request,
    with its CA chain (``POST /appliance/certificates/server/actions/update``).
    The SSO admin user (from Vault) lets the appliance update its
    registration. Before the call MEHO checks that the certificate holds
    the same public key as the request, names the appliance's host name
    and is still valid; else it stops. It also looks for a running
    certificate task first. The vendor answers with a task: the answer
    says ``accepted`` and names the status read ``srm.appliance.task.get``.
  - ``add_ca_certificates`` / ``remove_ca_certificates``: add or remove
    trusted CA certificates (``POST /appliance/certificates/ca`` and
    ``.../ca/actions/remove``). These are done when the call returns.

No password is in an answer. The only PEM text in an answer is the signing
request that ``srm.appliance.csr.create`` returns (it holds no secret);
certificates are named by subject and SHA-256 fingerprint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from meho_backplane.connectors.srm.appliance import setup_api_for
from meho_backplane.connectors.srm.setup_api import (
    SetupApi,
    SetupApiError,
    Steps,
    find_running_task,
    parse_ref,
    task_view,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike

__all__ = [
    "CERTIFICATE_TASK_WORDS",
    "appliance_certificate_set",
    "appliance_csr_create",
    "check_signed_certificate",
]

#: Words in a task's type or description that mark a certificate task.
CERTIFICATE_TASK_WORDS: Final[tuple[str, ...]] = ("certificat",)
STATUS_READ_OP: Final = "srm.appliance.task.get"

_CSR_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "common_name",
    "organization",
    "organization_unit",
    "locality",
    "state",
    "country",
    "email",
)
_SERVER_STEPS: Final[tuple[str, ...]] = ("running_task", "check_certificate", "install")


def _load_cert(pem: Any, what: str) -> x509.Certificate:
    if not isinstance(pem, str) or "BEGIN CERTIFICATE" not in pem:
        raise ValueError(f"{what} must be a certificate in PEM format")
    try:
        return x509.load_pem_x509_certificate(pem.encode())
    except ValueError:
        raise ValueError(f"{what} is not a readable PEM certificate") from None


def _load_csr(pem: Any) -> x509.CertificateSigningRequest:
    if not isinstance(pem, str) or "CERTIFICATE REQUEST" not in pem:
        raise ValueError("csr_pem must be the request from srm.appliance.csr.create, in PEM format")
    try:
        return x509.load_pem_x509_csr(pem.encode())
    except ValueError:
        raise ValueError("csr_pem is not a readable certificate signing request") from None


def _spki(key: Any) -> bytes:
    return bytes(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def _dns_names(subject: x509.Name, extensions: x509.Extensions) -> list[str]:
    try:
        san = extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return [str(name) for name in san.get_values_for_type(x509.DNSName)]
    except x509.ExtensionNotFound:
        return [str(attr.value) for attr in subject.get_attributes_for_oid(NameOID.COMMON_NAME)]


def _name_matches(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):
        head, _, rest = host.partition(".")
        return bool(head) and rest == pattern[2:]
    return pattern == host


def _cert_row(cert: x509.Certificate) -> dict[str, Any]:
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "sha256_fingerprint": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
    }


def check_signed_certificate(cert_pem: Any, csr_pem: Any, appliance_host: str) -> dict[str, Any]:
    """Refuse a certificate that does not fit the request or the appliance.

    Returns the certificate's subject, issuer, end date and fingerprint.
    Raises :class:`ValueError` naming the first problem.
    """
    cert = _load_cert(cert_pem, "certificate_pem")
    csr = _load_csr(csr_pem)
    if _spki(cert.public_key()) != _spki(csr.public_key()):
        raise ValueError(
            "the certificate does not hold the public key of the request: it was not "
            "signed for this request, so the appliance has no key for it"
        )
    names = _dns_names(cert.subject, cert.extensions)
    if not any(_name_matches(name, appliance_host) for name in names):
        raise ValueError(
            f"the certificate does not name the appliance {appliance_host!r} "
            f"(it names: {', '.join(names) or 'nothing'})"
        )
    if cert.not_valid_after_utc <= datetime.now(UTC):
        raise ValueError("the certificate has expired")
    return _cert_row(cert)


def _csr_body(params: dict[str, Any]) -> dict[str, Any]:
    body = {key: params[key] for key in _CSR_TEXT_FIELDS if params.get(key)}
    for key, field in (("fqdns", "fqdn"), ("ips", "ip")):
        values = params.get(key) or []
        if values:
            body[field] = ",".join(str(value) for value in values)
    return body


async def appliance_csr_create(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.csr.create``: a new key and request; return the request."""
    api = setup_api_for(connector, operator, target, params)
    answer = await api.call(
        "POST", "/appliance/certificates/actions/generate-csr", json=_csr_body(params)
    )
    pem = answer.get("value") if isinstance(answer, dict) else None
    csr = _load_csr(pem)
    try:
        sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_list = [str(name.value) for name in sans]
    except x509.ExtensionNotFound:
        san_list = []
    return {
        "outcome": "done",
        "csr_pem": pem,
        "subject": csr.subject.rfc4514_string(),
        "subject_alternative_names": san_list,
        "public_key_sha256": _sha256_hex(_spki(csr.public_key())),
    }


def _sha256_hex(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize().hex()


async def _ca_change(api: SetupApi, pems: Any, *, remove: bool) -> dict[str, Any]:
    if not isinstance(pems, list) or not pems:
        raise ValueError("give at least one CA certificate in PEM format")
    rows = [_cert_row(_load_cert(pem, "a CA certificate")) for pem in pems]
    path = "/appliance/certificates/ca/actions/remove" if remove else "/appliance/certificates/ca"
    await api.call("POST", path, json=[{"value": pem} for pem in pems])
    return {
        "outcome": "done",
        "change": "remove_ca_certificates" if remove else "add_ca_certificates",
        "certificates": rows,
        "task_id": None,
        "status_read": None,
    }


async def _install_server_certificate(
    connector: SrmConnector,
    operator: Operator,
    target: DrTargetLike,
    api: SetupApi,
    params: dict[str, Any],
) -> dict[str, Any]:
    spec = params["server_certificate"]
    sso_ref = parse_ref(params, "sso_secret_ref")
    steps = Steps(_SERVER_STEPS)
    answer: dict[str, Any] = {"outcome": "failed", "change": "server_certificate", "task_id": None}
    try:
        steps.start("running_task")
        running = await find_running_task(api, CERTIFICATE_TASK_WORDS)
        if running is not None:
            steps.done("running_task", found_task_id=running.get("id"))
            answer.update(outcome="already_running", task_id=running.get("id"))
            answer["task"] = task_view(running)
            return _with_steps(answer, steps)
        steps.done("running_task")
        steps.start("check_certificate")
        host = (await api.call("GET", "/appliance")).get("hostname")
        if not isinstance(host, str) or not host:
            raise SetupApiError("the appliance did not say its host name")
        try:
            answer["certificate"] = check_signed_certificate(
                spec.get("certificate_pem"), spec.get("csr_pem"), host
            )
        except ValueError as exc:
            steps.fail(str(exc))
            answer["outcome"] = "refused"
            return _with_steps(answer, steps)
        chain = spec.get("ca_chain_pem") or []
        for pem in chain:
            _load_cert(pem, "a ca_chain_pem entry")
        steps.done("check_certificate")
        steps.start("install")
        sso = await connector.setup_credentials_loader(target, operator, "sso_secret_ref", sso_ref)
        body = {
            "csr_format": {"certificate": spec["certificate_pem"], "ca_chain": chain},
            "credentials": {"admin_user": sso["username"], "admin_password": sso["password"]},
        }
        task = await api.call("POST", "/appliance/certificates/server/actions/update", json=body)
        steps.done("install")
        view = task_view(task)
        answer.update(outcome="accepted", task_id=view["task_id"], task=view)
    except (SetupApiError, ValueError) as exc:
        steps.fail(str(exc))
    return _with_steps(answer, steps)


def _with_steps(answer: dict[str, Any], steps: Steps) -> dict[str, Any]:
    answer["steps"] = steps.as_list()
    answer["status_read"] = (
        {"op_id": STATUS_READ_OP, "params": {"task_id": answer["task_id"]}}
        if answer.get("task_id")
        else None
    )
    return answer


async def appliance_certificate_set(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.certificate.set``: exactly one of the three changes."""
    given = [
        key
        for key in ("server_certificate", "add_ca_certificates", "remove_ca_certificates")
        if params.get(key) is not None
    ]
    if len(given) != 1:
        raise ValueError(
            "give exactly one of server_certificate, add_ca_certificates and remove_ca_certificates"
        )
    api = setup_api_for(connector, operator, target, params)
    if given[0] == "server_certificate":
        if not isinstance(params["server_certificate"], dict):
            raise ValueError("server_certificate must be an object")
        return await _install_server_certificate(connector, operator, target, api, params)
    return await _ca_change(api, params[given[0]], remove=given[0] == "remove_ca_certificates")
