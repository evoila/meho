# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The first certificate of a fresh install, for the Ingress.

The ``secrets`` Job calls :func:`self_signed_tls` when no certificate Secret
exists yet. It makes a small CA and one server certificate for the Ingress
host. The CA private key is thrown away after signing, so the CA can never
sign a second certificate. The customer replaces the pair with their own
certificate later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

__all__ = ["VALIDITY_DAYS", "self_signed_tls"]

# Apple platforms refuse TLS server certificates valid for more than 825 days
# (https://support.apple.com/en-us/103769). The CA gets the same lifetime: its
# key is gone after signing, so a longer CA lifetime would buy nothing.
VALIDITY_DAYS = 825
_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MEHO install CA")])
# X.509 limits a common name to 64 characters; longer hosts live in the SAN only.
_MAX_COMMON_NAME = 64


def _key_usage(*, cert_sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not cert_sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=cert_sign,
        crl_sign=cert_sign,
        encipher_only=False,
        decipher_only=False,
    )


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def self_signed_tls(host: str, *, now: datetime | None = None) -> dict[str, str]:
    """Return ``tls.crt``, ``tls.key`` and ``ca.crt`` (PEM) for ``host``."""
    start = (now or datetime.now(UTC)) - timedelta(minutes=5)
    end = start + timedelta(days=VALIDITY_DAYS)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_key_id = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_CA_NAME)
        .issuer_name(_CA_NAME)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(cert_sign=True), critical=True)
        .add_extension(ca_key_id, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    # RFC 5280 4.2.1.6: with an empty subject the SAN extension is critical.
    long_host = len(host) > _MAX_COMMON_NAME
    subject = x509.Name([] if long_host else [x509.NameAttribute(NameOID.COMMON_NAME, host)])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(_CA_NAME)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=long_host)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(cert_sign=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_key_id),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return {"tls.crt": _pem(leaf_cert), "tls.key": leaf_key_pem, "ca.crt": _pem(ca_cert)}
