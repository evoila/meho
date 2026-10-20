#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when a rendered chart makes an object outside its namespace.

A customer's cluster admin often gives the person who installs MEHO
rights in one namespace only: the built-in ``admin`` role, bound in that
namespace (install form 3, #4099). That is enough only while the chart
makes nothing outside the namespace. This script reads ``helm template``
output on stdin and fails on:

1. an object whose kind is not in ``NAMESPACED_KINDS`` — a
   cluster-scoped kind such as ``ClusterRole``, or a kind nobody has
   checked yet;
2. an object that names a namespace other than ``--namespace``.

``NAMESPACED_KINDS`` is an allow-list on purpose. A kind that is not on
it fails, so a new cluster-scoped kind (``ClusterSecretStore``,
OpenShift's ``SecurityContextConstraints``) cannot pass by accident. If
a new kind is namespaced, add it to the list.

One exception, only with ``--allow-auth-delegator``: a
``ClusterRoleBinding`` to the ``system:auth-delegator`` ClusterRole. The
chart renders it only when ``coreVault.authDelegator: true`` (default
false), and the form-3 prerequisites say a cluster admin creates it once.

Exit codes
----------

* 0 — every object is namespaced and in ``--namespace``
* 1 — at least one object is not (printed to stderr)
* 2 — bad input (no objects, or YAML that does not parse)
"""

from __future__ import annotations

import argparse
import re
import sys

import yaml

#: Namespaced kinds the chart may render. Every other kind fails.
NAMESPACED_KINDS: frozenset[str] = frozenset(
    {
        # core/v1
        "ConfigMap",
        "PersistentVolumeClaim",
        "Pod",
        "Secret",
        "Service",
        "ServiceAccount",
        # apps/v1, batch/v1
        "Deployment",
        "StatefulSet",
        "Job",
        "CronJob",
        # networking.k8s.io/v1, policy/v1, rbac.authorization.k8s.io/v1
        "Ingress",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "Role",
        "RoleBinding",
        # Custom resources the chart renders on opt-in
        # (external-secrets.io, monitoring.coreos.com)
        "ExternalSecret",
        "ServiceMonitor",
        "PrometheusRule",
    }
)

_SEPARATOR = re.compile(r"^---\s*$", re.MULTILINE)
_SOURCE = re.compile(r"^# Source: (\S+)", re.MULTILINE)


def _is_auth_delegator_binding(obj: dict) -> bool:
    role_ref = obj.get("roleRef") or {}
    return (
        obj.get("kind") == "ClusterRoleBinding" and role_ref.get("name") == "system:auth-delegator"
    )


def problems(
    rendered: str, namespace: str, *, allow_auth_delegator: bool = False
) -> tuple[int, list[str]]:
    """Return the object count and one line per object outside ``namespace``."""
    count = 0
    found: list[str] = []
    for chunk in _SEPARATOR.split(rendered):
        obj = yaml.safe_load(chunk)
        if not obj:
            continue
        count += 1
        kind = obj.get("kind")
        meta = obj.get("metadata") or {}
        source = _SOURCE.search(chunk)
        what = f"{kind}/{meta.get('name')} ({source.group(1) if source else 'unknown template'})"
        if kind not in NAMESPACED_KINDS:
            if allow_auth_delegator and _is_auth_delegator_binding(obj):
                continue
            found.append(
                f"{what}: kind {kind} is cluster-scoped, or not yet in NAMESPACED_KINDS "
                "in scripts/ci/check_chart_namespace_scope.py"
            )
        elif meta.get("namespace", namespace) != namespace:
            found.append(
                f"{what}: namespace {meta['namespace']}, not the release namespace {namespace}"
            )
    return count, found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a rendered chart makes an object outside its namespace."
    )
    parser.add_argument(
        "--namespace", required=True, help="the namespace passed to `helm template --namespace`"
    )
    parser.add_argument(
        "--label", default="render", help="names this render in the output, e.g. the values file"
    )
    parser.add_argument(
        "--allow-auth-delegator",
        action="store_true",
        help="allow the system:auth-delegator ClusterRoleBinding "
        "(for a render with coreVault.authDelegator=true)",
    )
    args = parser.parse_args(argv)

    try:
        count, found = problems(
            sys.stdin.read(), args.namespace, allow_auth_delegator=args.allow_auth_delegator
        )
    except yaml.YAMLError as exc:
        print(
            f"check_chart_namespace_scope: {args.label}: the input is not YAML: {exc}",
            file=sys.stderr,
        )
        return 2
    if count == 0:
        print(f"check_chart_namespace_scope: {args.label}: no objects on stdin", file=sys.stderr)
        return 2
    if found:
        print(f"{args.label}: the chart makes objects outside its namespace:", file=sys.stderr)
        for line in found:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"OK: {args.label}: all {count} objects are namespaced and in {args.namespace}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
