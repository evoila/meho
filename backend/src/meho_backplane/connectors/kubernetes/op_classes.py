# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Kubernetes connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from collections.abc import Mapping
from typing import Any, Final

import yaml

from meho_backplane.op_classes import (
    declare_credential_capable,
    declare_credential_check,
    declare_no_secret_value,
    declare_op_classes,
)

declare_op_classes(
    credential_read=(
        # The governed guest-cluster kubeconfig read (#3496). Reads a Secret's
        # data value and stages it to a tenant-scoped Vault secret_ref,
        # returning only the ref, never the value. The class makes audit and
        # broadcast collapse to aggregate-only on top of its approval gate.
        "k8s.secret.read_to_ref",
        # #5230 -- the compare read. The backplane reads the secret values
        # itself to compare them and returns only same/different. The class
        # makes the flight recorder drop the vendor bodies (they carry the
        # values) and collapses the broadcast to aggregate-only.
        "k8s.secret.compare_to_ref",
    ),
    credential_write=(
        # The Secret ``data`` / ``stringData`` is in ``params``.
        "k8s.secret.create",
        # The values of an existing Secret ride in the same params shape as
        # k8s.secret.create (inline maps or store refs), so the same class
        # keeps them off the approval row and the broadcast.
        "k8s.secret.update",
        # The Job ``spec`` carries a pod template whose inline ``env`` entries
        # can hold credential material in ``params`` (G3.14-T1 #1403).
        "k8s.job.create",
    ),
)

# The compare read returns only "same" or "different", never a value. So it
# runs without waiting for approval (meho_backplane.operations.credential_read).
declare_no_secret_value(("k8s.secret.compare_to_ref",))

# A command in a container can read or write any credential the container
# holds. So every call waits for a person, and no standing grant, agent
# grant or run approval covers it.
declare_credential_capable(("k8s.exec",))

#: Kinds whose objects hold a credential, in lower case. A ServiceAccount
#: token is a Secret too.
CREDENTIAL_KINDS: Final[frozenset[str]] = frozenset({"secret"})


def _holds_credential(doc: Any) -> bool:
    """True when *doc* is a credential kind, a list with one, or cannot be read."""
    if not isinstance(doc, Mapping):
        return True
    kind = doc.get("kind")
    if not isinstance(kind, str) or not kind:
        return True
    if kind.lower() in CREDENTIAL_KINDS:
        return True
    items = doc.get("items")
    if items is None:
        return False
    if not isinstance(items, list):
        return True
    return any(_holds_credential(item) for item in items)


def manifest_holds_credential(params: Mapping[str, Any]) -> bool:
    """True when the ``k8s.apply`` manifest in *params* holds a credential kind.

    Any document of a multi-document manifest counts, and any item of a
    list. A manifest MEHO cannot read counts too: it is not text, it is not
    YAML or JSON, or a document has no kind.
    """
    manifest = params.get("manifest")
    if not isinstance(manifest, str):
        return True
    try:
        docs = [doc for doc in yaml.safe_load_all(manifest) if doc is not None]
    except (yaml.YAMLError, RecursionError):
        return True
    if not docs:
        return True
    return any(_holds_credential(doc) for doc in docs)


# A k8s.apply of a Secret writes a credential. Such a call waits for a
# person on every call; an apply of other kinds keeps the rules of k8s.apply.
declare_credential_check("k8s.apply", manifest_holds_credential)
