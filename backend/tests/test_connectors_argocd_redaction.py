# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ArgoCD repository-credential redactor (#3501).

Pins the recursive redaction contract directly — no event loop, no HTTP —
so a regression in the scrubber surfaces as a fast, isolated failure. The
wired ``argocd.repo.list`` dispatch is proven in
``test_connectors_argocd_reads.py::test_repo_list_redacts_credentials``.
"""

from __future__ import annotations

from meho_backplane.connectors.argocd.redaction import REDACTED, redact_argocd_credentials


def _repository() -> dict[str, object]:
    """A ``Repository`` object carrying every credential field ArgoCD echoes."""
    return {
        "repo": "https://github.com/example/gitops",
        "type": "git",
        "name": "gitops",
        "project": "default",
        "username": "git-user",
        "password": "https-basic-password-must-not-leak",
        "sshPrivateKey": "-----BEGIN OPENSSH PRIVATE KEY-----AAAA-----END-----",
        "tlsClientCertData": "-----BEGIN CERTIFICATE-----not-secret-----END-----",
        "tlsClientCertKey": "-----BEGIN PRIVATE KEY-----must-not-leak-----END-----",
        "bearerToken": "bearer-token-must-not-leak",
        "githubAppId": "12345",
        "githubAppPrivateKey": "-----BEGIN RSA PRIVATE KEY-----must-not-leak-----END-----",
        "connectionState": {"status": "Successful", "message": ""},
    }


def test_redacts_all_named_credential_fields() -> None:
    out = redact_argocd_credentials(_repository())
    for field in ("password", "sshPrivateKey", "tlsClientCertKey", "bearerToken", "githubAppPrivateKey"):
        assert out[field] == REDACTED


def test_non_credential_fields_survive() -> None:
    out = redact_argocd_credentials(_repository())
    assert out["repo"] == "https://github.com/example/gitops"
    assert out["username"] == "git-user"
    assert out["githubAppId"] == "12345"
    assert out["connectionState"] == {"status": "Successful", "message": ""}
    # The cert data (public half) is not a secret and is kept.
    assert out["tlsClientCertData"].startswith("-----BEGIN CERTIFICATE-----")


def test_no_secret_bytes_survive_in_repository_list_envelope() -> None:
    envelope = {"metadata": {}, "items": [_repository(), _repository()]}
    blob = str(redact_argocd_credentials(envelope))
    for secret in (
        "https-basic-password-must-not-leak",
        "BEGIN OPENSSH PRIVATE KEY",
        "bearer-token-must-not-leak",
        "must-not-leak",
    ):
        assert secret not in blob


def test_matches_case_insensitively() -> None:
    out = redact_argocd_credentials({"Password": "x", "BearerToken": "y"})
    assert out["Password"] == REDACTED
    assert out["BearerToken"] == REDACTED


def test_input_is_not_mutated() -> None:
    original = _repository()
    redact_argocd_credentials(original)
    assert original["password"] == "https-basic-password-must-not-leak"


def test_scalars_and_lists_pass_through() -> None:
    assert redact_argocd_credentials("plain") == "plain"
    assert redact_argocd_credentials([1, 2, 3]) == [1, 2, 3]
