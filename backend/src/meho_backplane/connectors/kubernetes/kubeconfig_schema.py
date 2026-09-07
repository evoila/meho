# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Passive-only kubeconfig schema for the Kubernetes connector.

A kubeconfig the connector loads comes from a per-target credential
secret (Vault KV-v2 / GCP Secret Manager). Target creation is
tenant-admin gated, so the secret is *tenant-controlled* — but a
tenant's right to manage its own credentials must not translate into
code execution or arbitrary file reads inside the shared backplane
process. ``kubernetes_asyncio``'s kubeconfig loader honours the full
client-side kubeconfig feature set, several parts of which reach an
out-of-band sink when the document is loaded:

* an ``exec`` credential provider spawns a **subprocess** with the
  configured command/args (``KubeConfigLoader.load_from_exec_plugin``);
* a legacy ``auth-provider`` block (``gcp`` / ``oidc`` / vendor
  plugins) performs a **network token fetch/refresh**
  (``load_gcp_token`` / ``_load_oid_token``);
* a file-path reference — cluster ``certificate-authority``, user
  ``client-certificate`` / ``client-key`` / ``tokenFile`` — makes the
  loader **read that local path** off the backplane's own filesystem
  (``FileOrData`` falls back to the ``<key>`` file path whenever the
  inline ``<key>-data`` field is absent).

This module enforces a minimal server-side schema *before* the parsed
mapping can reach that loader. It rejects the three sink classes above
with a specific, fail-closed error, explicitly validates the cluster
endpoint / proxy / TLS options, and returns a **fresh** mapping built
from only the accepted fields — the untrusted input mapping is never
handed to the library. Only inline authentication survives: a bearer
``token``, HTTP-basic ``username``/``password``, and inline
``client-certificate-data`` / ``client-key-data`` / cluster
``certificate-authority-data``.

No executable-provider facility is retained: ``exec`` is rejected
unconditionally, with no parameter or elevation that re-enables it. A
deployment that genuinely needs exec-plugin credentials must run that
behind a separate platform-admin-managed, isolated facility (out of
scope here); this module never spawns a subprocess.

Verified against ``kubernetes_asyncio`` 36.1.0
(``config.kube_config.KubeConfigLoader`` /
``config.kube_config.FileOrData``). See
:doc:`docs/codebase/connectors-kubernetes.md` (Passive-only kubeconfig
schema) and the Kubernetes upstream guidance that untrusted kubeconfig
can cause code execution or file exposure
(https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "UnsupportedKubeconfigError",
    "enforce_passive_kubeconfig",
]


class UnsupportedKubeconfigError(ValueError):
    """A kubeconfig requested an active / out-of-band credential mechanism.

    Raised when a parsed kubeconfig carries an ``exec`` provider, a
    legacy ``auth-provider`` block, a local-file credential/CA
    reference, or a structurally invalid endpoint/proxy/TLS field.
    Subclasses :exc:`ValueError` so it flows through the loaders'
    documented ``ValueError`` contract (raised today by
    :func:`~meho_backplane.connectors.kubernetes.kubeconfig.parse_kubeconfig_yaml`)
    and the dispatcher's catch-all maps it to a ``connector_error``
    result. The message names only the offending *section / field* —
    never a credential value, server endpoint, or certificate — so it
    is safe to log.
    """


#: Cluster ``cluster`` block: file-path reference that makes the library
#: read local disk. Rejected even when the inline ``-data`` sibling is
#: also present (no ambiguity in a passive config). The accepted cluster
#: fields — ``server`` / ``certificate-authority-data`` /
#: ``insecure-skip-tls-verify`` / ``tls-server-name`` / ``proxy-url`` —
#: are each handled and validated explicitly in :func:`_clean_cluster_entry`.
_REJECTED_CLUSTER_KEYS = frozenset({"certificate-authority"})

#: User ``user`` block: inline authentication fields the connector
#: accepts, in the order they are copied into the fresh config.
_INLINE_USER_FIELDS: tuple[str, ...] = (
    "token",
    "username",
    "password",
    "client-certificate-data",
    "client-key-data",
)

#: User ``user`` block: subprocess (``exec``), legacy provider
#: (``auth-provider``) and local-file credential sinks. Any presence is
#: a hard rejection.
_REJECTED_USER_KEYS = frozenset(
    {
        "exec",
        "auth-provider",
        "tokenFile",
        "client-certificate",
        "client-key",
    }
)

#: Context ``context`` block: the only fields the loader needs to bind a
#: context to its cluster/user.
_CONTEXT_FIELDS: tuple[str, ...] = ("cluster", "user", "namespace")

#: URL schemes an API ``server`` endpoint may use.
_ALLOWED_SERVER_SCHEMES = frozenset({"http", "https"})

#: URL schemes a cluster ``proxy-url`` may use.
_ALLOWED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


def enforce_passive_kubeconfig(config: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed kubeconfig and rebuild it as a passive-only config.

    ``config`` is the mapping produced by
    :func:`~meho_backplane.connectors.kubernetes.kubeconfig.parse_kubeconfig_yaml`.
    Returns a fresh mapping in the shape
    ``kubernetes_asyncio.config.new_client_from_config_dict`` /
    ``load_kube_config_from_dict`` accept, containing only fields that
    were validated here — the input mapping is never returned or handed
    to the library.

    Raises
    ------
    UnsupportedKubeconfigError
        Any ``exec`` provider, legacy ``auth-provider`` block, local-file
        credential/CA reference, or a structurally invalid
        endpoint/proxy/TLS/context field.
    """
    if not isinstance(config, dict):
        raise UnsupportedKubeconfigError(
            f"kubeconfig must be a mapping, got {type(config).__name__}"
        )

    clean: dict[str, Any] = {}

    for key in ("apiVersion", "kind"):
        if key in config:
            value = config[key]
            if not isinstance(value, str):
                raise UnsupportedKubeconfigError(f"kubeconfig {key!r} must be a string")
            clean[key] = value

    if "current-context" in config:
        current = config["current-context"]
        if not isinstance(current, str):
            raise UnsupportedKubeconfigError("kubeconfig 'current-context' must be a string")
        clean["current-context"] = current

    clusters = config.get("clusters")
    if clusters is not None:
        clean["clusters"] = [
            _clean_cluster_entry(entry, index)
            for index, entry in enumerate(_require_list(clusters, "clusters"))
        ]

    users = config.get("users")
    if users is not None:
        clean["users"] = [
            _clean_user_entry(entry, index)
            for index, entry in enumerate(_require_list(users, "users"))
        ]

    contexts = config.get("contexts")
    if contexts is not None:
        clean["contexts"] = [
            _clean_context_entry(entry, index)
            for index, entry in enumerate(_require_list(contexts, "contexts"))
        ]

    return clean


def _require_list(value: Any, section: str) -> list[Any]:
    if not isinstance(value, list):
        raise UnsupportedKubeconfigError(
            f"kubeconfig {section!r} must be a list, got {type(value).__name__}"
        )
    return value


def _entry_label(entry: dict[str, Any], section: str, index: int) -> str:
    """A value-free label for error messages: the entry ``name`` or index.

    ``name`` is operator-chosen metadata (never a credential), so it is
    safe to surface; the fallback index avoids leaking anything when a
    malformed entry has no usable name.
    """
    name = entry.get("name")
    if isinstance(name, str) and name:
        return f"{section} entry {name!r}"
    return f"{section} entry #{index}"


def _copy_name(entry: dict[str, Any], out: dict[str, Any], label: str) -> None:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise UnsupportedKubeconfigError(f"kubeconfig {label} is missing a string 'name'")
    out["name"] = name


def _require_str(value: Any, label: str, field: str) -> str:
    if not isinstance(value, str):
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} field {field!r} must be a string, got {type(value).__name__}"
        )
    return value


def _clean_cluster_entry(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise UnsupportedKubeconfigError(f"kubeconfig clusters entry #{index} must be a mapping")
    label = _entry_label(entry, "clusters", index)
    inner = entry.get("cluster")
    if not isinstance(inner, dict):
        raise UnsupportedKubeconfigError(f"kubeconfig {label} is missing a 'cluster' mapping")

    rejected = sorted(_REJECTED_CLUSTER_KEYS & inner.keys())
    if rejected:
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} references a local file via {rejected!r}; "
            "only inline 'certificate-authority-data' is permitted"
        )

    clean_inner: dict[str, Any] = {"server": _validate_server(inner.get("server"), label)}

    if "certificate-authority-data" in inner:
        clean_inner["certificate-authority-data"] = _require_str(
            inner["certificate-authority-data"], label, "certificate-authority-data"
        )
    if "insecure-skip-tls-verify" in inner:
        flag = inner["insecure-skip-tls-verify"]
        if not isinstance(flag, bool):
            raise UnsupportedKubeconfigError(
                f"kubeconfig {label} 'insecure-skip-tls-verify' must be a boolean"
            )
        clean_inner["insecure-skip-tls-verify"] = flag
    if "tls-server-name" in inner:
        clean_inner["tls-server-name"] = _require_str(
            inner["tls-server-name"], label, "tls-server-name"
        )
    if "proxy-url" in inner:
        clean_inner["proxy-url"] = _validate_proxy(inner["proxy-url"], label)

    out: dict[str, Any] = {"cluster": clean_inner}
    _copy_name(entry, out, label)
    return out


def _clean_user_entry(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise UnsupportedKubeconfigError(f"kubeconfig users entry #{index} must be a mapping")
    label = _entry_label(entry, "users", index)
    inner = entry.get("user")
    if not isinstance(inner, dict):
        raise UnsupportedKubeconfigError(f"kubeconfig {label} is missing a 'user' mapping")

    rejected = sorted(_REJECTED_USER_KEYS & inner.keys())
    if rejected:
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} uses an unsupported credential mechanism {rejected!r}; "
            "exec plugins, legacy auth-provider blocks and local credential files are "
            "rejected — use an inline token, username/password, or client "
            "certificate/key data"
        )

    clean_inner: dict[str, Any] = {}
    for key in _INLINE_USER_FIELDS:
        if key in inner:
            clean_inner[key] = _require_str(inner[key], label, key)

    out: dict[str, Any] = {"user": clean_inner}
    _copy_name(entry, out, label)
    return out


def _clean_context_entry(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise UnsupportedKubeconfigError(f"kubeconfig contexts entry #{index} must be a mapping")
    label = _entry_label(entry, "contexts", index)
    inner = entry.get("context")
    if not isinstance(inner, dict):
        raise UnsupportedKubeconfigError(f"kubeconfig {label} is missing a 'context' mapping")

    clean_inner: dict[str, Any] = {}
    for key in _CONTEXT_FIELDS:
        if key in inner:
            clean_inner[key] = _require_str(inner[key], label, key)

    out: dict[str, Any] = {"context": clean_inner}
    _copy_name(entry, out, label)
    return out


def _validate_server(server: Any, label: str) -> str:
    if not isinstance(server, str) or not server.strip():
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} is missing a non-empty string 'server' endpoint"
        )
    parts = urlsplit(server)
    if parts.scheme not in _ALLOWED_SERVER_SCHEMES or not parts.netloc:
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} 'server' must be an http(s) URL with a host"
        )
    return server


def _validate_proxy(proxy: Any, label: str) -> str:
    if not isinstance(proxy, str) or not proxy.strip():
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} 'proxy-url' must be a non-empty string"
        )
    parts = urlsplit(proxy)
    if parts.scheme not in _ALLOWED_PROXY_SCHEMES or not parts.netloc:
        raise UnsupportedKubeconfigError(
            f"kubeconfig {label} 'proxy-url' must be an http/https/socks5 URL with a host"
        )
    return proxy
