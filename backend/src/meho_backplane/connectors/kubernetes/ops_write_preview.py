# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for ``k8s.secret.create``, ``k8s.job.create`` (#4850)
and ``k8s.cronjob.run``.

The first two ops are declared ``credential_write``
(``kubernetes/op_classes.py``): a Secret's
values ride in the params, and a Job's pod template can hold secrets in its
``env`` values or its command line. For such an op the generic echo of
params is turned off, so without these builders the approver saw only the op
id and the target. ``k8s.cronjob.run`` takes only names, but its params say
nothing about what will run: the builder reads the CronJob and shows it.

=====================  ========================================================
op_id                  preview
=====================  ========================================================
``k8s.secret.create``  ``{name, namespace, type, key_names, inline_key_names,
                       labels?, annotations?,
                       string_data_secret_ref?, data_secret_ref?,
                       string_data_secret_mount?, data_secret_mount?}``
``k8s.job.create``     ``{name, namespace, spec}``; ``spec`` is the Job
                       spec from the params, made safe to show (below)
``k8s.cronjob.run``    ``{cronjob, namespace, job_name, schedule, suspend,
                       concurrency_policy, active_jobs, job_spec}``;
                       ``job_spec`` is the CronJob's job template spec,
                       made safe the same way. ``active_jobs`` names this
                       CronJob's Jobs that still run. Read from the
                       cluster at park time.
=====================  ========================================================

What is never shown
===================

* A Secret value: only the key names. A ``*_secret_ref`` map is shown,
  because it holds a store path per key (``vault:<path>#<field>``), not a
  value. The value is read from the store only when the approved call runs.
  The ``*_secret_mount`` names are shown too: they say which store mount the
  paths are read from. The Secret's ``labels`` and ``annotations`` are
  metadata, not values, so they are shown in full.
* In a Job spec: every ``env`` value and every ``httpHeaders`` value (in a
  probe or lifecycle ``httpGet``). The name stays. ``valueFrom`` and
  ``envFrom`` stay too: they name a Secret or ConfigMap and a key, not a
  value. All the rest of the spec is shown, so the approver sees what the
  pods run and with which rights: ``command``, ``args``, ``image``,
  ``securityContext``, ``hostPID`` / ``hostNetwork`` / ``hostIPC``,
  ``nodeName``, ``nodeSelector``, ``tolerations``, ``affinity``,
  ``volumes`` and ``volumeMounts``. A container's ``command`` and ``args``
  are read as one command line, the way Kubernetes runs them. The words
  stay, but these values are hidden: the value of a ``NAME=VALUE`` or
  ``--name=value`` whose name holds a secret word (``PGPASSWORD=...``,
  ``AWS_SECRET_ACCESS_KEY=...``, ``--password=...``, any length), the
  argument after a secret flag (``--password x``, ``--token x``, ``-p x``;
  also when the flag ends ``command`` and the value starts ``args``), the
  password in ``user:password`` after ``-u`` or ``--user``, the password
  in a URL that carries a user and password and tokens with a known
  vendor prefix (``ghp_...``). How far such a value runs depends on who
  reads the list item. A list item is one argument of the program, no
  shell splits it: the value is the rest of the item, spaces, quotes and
  shell operators included, so ``PGPASSWORD=x;tail`` and
  ``"PGPASSWORD=x"suffix`` are hidden whole. Only the item right after
  ``-c``, or after a short option group that holds ``c`` (``-ec``,
  ``-lc``), is a shell command string (``sh -c "..."``, ``bash -lc
  "..."``): there the value is one shell word, quotes and escapes
  included, and an unquoted shell operator (``;`` ``&`` ``|`` ``<`` ``>``
  ``(`` ``)``) ends it, so ``sh -ec "PGPASSWORD=x;id"`` keeps ``id`` and
  the commands after a secret stay visible. Then the shared
  params redaction of the generic echo runs over the spec: it hides
  secret-shaped text anywhere (JWTs, bearer tokens) and the value of a
  field with a secret-shaped name (a ``secret`` volume, for example). A
  secret on a command line with no known shape (``mytool hunter2``) is
  shown.

``k8s.apply`` has its own builder in :mod:`meho_backplane.operations._preview`
(a server dry-run). It uses :func:`apply_manifest_echo` and
:func:`apply_subject` from this module to also show the manifest it applies
(#3877). The echo hides the same kind of values:

* A Secret's ``data`` and ``stringData`` values: only the key names stay.
  This also applies to a Secret nested inside another document, for
  example a custom resource that carries a Secret in its spec.
* Every ``env`` entry's ``value``: only the variable name stays.
* Every ``httpHeaders`` entry's ``value`` in a probe or lifecycle hook
  ``httpGet``: only the header name stays.
* Every ``command`` and ``args`` list, at any depth (containers, init and
  ephemeral containers, probe and lifecycle ``exec``): only its size in
  bytes stays, as ``{"hidden": true, "bytes": N}``, because a command line
  can hold a token.
* The ``kubectl.kubernetes.io/last-applied-configuration`` annotation, in
  any ``annotations`` map at any depth (the document's own metadata, a pod
  template's metadata, a nested object): it is a full copy of an earlier
  object and can hold all of the above.

The Secret and Job builders read only ``ctx.params`` and do no network
calls. The CronJob builder reads the CronJob once; a missing CronJob raises,
and the park then stores the ``preview_unavailable`` marker with the reason.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import math
import re
from collections.abc import Callable
from typing import Any

from kubernetes_asyncio import client

from meho_backplane.broadcast.events import is_secret_name_value_pair, is_secret_param_name
from meho_backplane.connectors.kubernetes.op_classes import (
    PLAIN_SETTINGS,
    SECRET_VALUE_FIELDS,
    hide_command_secrets,
)
from meho_backplane.connectors.kubernetes.ops_write_apply_summary import OBJECT_SUMMARY_KEY
from meho_backplane.connectors.kubernetes.ops_write_dangerous import _parse_manifest, error_subject
from meho_backplane.operations._preview import (
    PreviewContext,
    _redact_echoed_params,
    register_person_only_preview_keys,
    register_preview_builder,
)
from meho_backplane.redaction.command_line import HIDDEN

#: Put in place of a value the approver must not see.
_HIDDEN = HIDDEN

#: This annotation holds a full JSON copy of an earlier object, Secret values included.
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

#: Most bytes of manifest the ``k8s.apply`` echo keeps, counting every entry
#: of the echo as it is stored, after every redaction. A document that does
#: not fit in full is shown by its identity only; when not even that fits,
#: the rest of the documents are summed up in one closing entry. So a huge
#: manifest, or a huge number of documents, cannot fill the approval row.
MANIFEST_ECHO_MAX_BYTES = 16_384

#: The shared params redaction the caller runs over the hidden documents,
#: before the echo budget is applied. Takes and returns one dict per document.
ManifestRedactor = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]

#: Most objects named in the ``k8s.apply`` subject line.
_SUBJECT_MAX_OBJECTS = 5


def _sorted_keys(value: Any) -> list[str]:
    """Sorted key names of a dict param; ``[]`` for anything else."""
    return sorted(str(key) for key in value) if isinstance(value, dict) else []


def _ref_map(value: Any) -> dict[str, str] | None:
    """A string map (store refs per key, labels, annotations) sorted, or ``None`` when absent."""
    if not isinstance(value, dict) or not value:
        return None
    return {str(key): str(ref) for key, ref in sorted(value.items())}


async def _secret_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.secret.create`` — the Secret and its key names, never a value."""
    params = ctx.params
    inline_keys = set(_sorted_keys(params.get("string_data"))) | set(
        _sorted_keys(params.get("data"))
    )
    string_refs = _ref_map(params.get("string_data_secret_ref"))
    data_refs = _ref_map(params.get("data_secret_ref"))
    all_keys = inline_keys | set(string_refs or {}) | set(data_refs or {})
    preview: dict[str, Any] = {
        "resource": "secret",
        "name": params.get("name"),
        "namespace": params.get("namespace"),
        "type": params.get("type", "Opaque"),
        "key_names": sorted(all_keys),
        "inline_key_names": sorted(inline_keys),
    }
    for meta_field in ("labels", "annotations"):
        meta = _ref_map(params.get(meta_field))
        if meta is not None:
            preview[meta_field] = meta
    if string_refs is not None:
        preview["string_data_secret_ref"] = string_refs
    if data_refs is not None:
        preview["data_secret_ref"] = data_refs
    for mount_field in ("string_data_secret_mount", "data_secret_mount"):
        mount = params.get(mount_field)
        if mount:
            preview[mount_field] = str(mount)
    return preview


def _parts_bytes(parts: list[Any]) -> int:
    """Total UTF-8 size of the parts of a command line."""
    return sum(len(str(part).encode("utf-8")) for part in parts)


def _job_spec_echo(ctx: PreviewContext, spec: Any) -> Any:
    """A Job spec safe to show the approver (see the module docstring).

    The ``env`` and ``httpHeaders`` values are hidden by structure; the
    command lines stay, with their secret values hidden. Then the shared
    params redaction runs over the result, so secret-shaped text is hidden
    wherever it is.
    """
    tenant = str(ctx.operator.tenant_id) if ctx.operator.tenant_id is not None else None
    redacted = _redact_echoed_params(
        {"spec": _hide_values(spec, hide_commands=False)},
        connector_id=ctx.connector_id,
        tenant=tenant,
        op_id=ctx.descriptor.op_id,
    )
    return redacted.get("spec")


async def _job_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.job.create`` — the Job spec as it will be created, no secret value."""
    return {
        "resource": "job",
        "name": ctx.params.get("name"),
        "namespace": ctx.params.get("namespace"),
        "spec": await asyncio.to_thread(_job_spec_echo, ctx, ctx.params.get("spec")),
    }


async def _cronjob_run_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``k8s.cronjob.run`` — the CronJob's job template spec, no secret value.

    Reads the CronJob through the connector. Declines (``None``) when no
    Kubernetes connector or target resolved, so the generic echo applies.
    """
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.ops_cronjob import model_to_json, read_cronjob

    connector = ctx.connector_instance
    if not isinstance(connector, KubernetesConnector) or ctx.target is None:
        return None
    cronjob_name = str(ctx.params.get("cronjob") or "")
    namespace = str(ctx.params.get("namespace") or "")
    api_client = await connector._get_api_client(ctx.target, ctx.operator)
    cronjob = await read_cronjob(client.BatchV1Api(api_client), namespace, cronjob_name)
    spec = cronjob.spec
    active = cronjob.status.active if cronjob.status is not None else None
    return {
        "resource": "cronjob_run",
        "cronjob": cronjob_name,
        "namespace": namespace,
        "job_name": ctx.params.get("job_name") or f"{cronjob_name}-manual-<random>",
        "schedule": spec.schedule,
        "suspend": bool(spec.suspend),
        "concurrency_policy": spec.concurrency_policy,
        "active_jobs": [ref.name for ref in active or [] if ref.name],
        "job_spec": await asyncio.to_thread(
            _job_spec_echo, ctx, model_to_json(spec.job_template.spec)
        ),
    }


#: Lists of ``{name, value}`` entries whose ``value`` is hidden: container
#: ``env`` and the ``httpHeaders`` of a probe or lifecycle ``httpGet``.
_NAME_VALUE_LISTS = ("env", "httpHeaders")

#: Command-line lists: a container's ``command`` and ``args``, and the
#: ``exec.command`` of a probe or lifecycle hook. In this order they make one
#: command line, the way Kubernetes runs them.
_COMMAND_LISTS = ("command", "args")


def _hide_command_line(
    node: dict[str, Any], *, p_after_programs: bool = False
) -> dict[str, list[Any]]:
    """The ``command`` and ``args`` lists of *node* with their secret values hidden.

    Both lists are read as one command line, so a secret flag that ends
    ``command`` hides the first argument of ``args``. Keys whose value is
    not a list are left out. *p_after_programs* is the ``k8s.apply`` rule
    (:func:`~.op_classes.hide_command_secrets`).
    """
    lists = {key: node[key] for key in _COMMAND_LISTS if isinstance(node.get(key), list)}
    hidden = hide_command_secrets(
        [part for parts in lists.values() for part in parts], p_after_programs=p_after_programs
    )
    out: dict[str, list[Any]] = {}
    start = 0
    for key, parts in lists.items():
        out[key] = hidden[start : start + len(parts)]
        start += len(parts)
    return out


def _hide_secret_values(secret: dict[str, Any]) -> dict[str, Any]:
    """Copy a ``kind: Secret`` dict with every value of its value fields hidden.

    The fields are :data:`~.op_classes.SECRET_VALUE_FIELDS`, the same ones
    the check of the stored params reads (``data``, ``stringData`` and
    ``binaryData``); the key names stay.
    """
    out = dict(secret)
    for field in SECRET_VALUE_FIELDS:
        if field in out:
            value = out[field]
            out[field] = (
                {str(key): _HIDDEN for key in value} if isinstance(value, dict) else _HIDDEN
            )
    return out


def _is_secret_scalar(key: Any, item: Any, *, secret_pair: bool) -> bool:
    """True for a value that is not a text, a map or a list, under a secret name.

    YAML reads an unquoted ``password: 123456`` as a number and
    ``password: yes`` as a yes/no value. A secret name is a key the name rule marks
    (:func:`~meho_backplane.broadcast.events.is_secret_param_name`), or the
    ``value`` of a ``{name, value}`` pair whose name it marks
    (*secret_pair*). A field of :data:`~.op_classes.PLAIN_SETTINGS` keeps its value.
    """
    if item is None or isinstance(item, str | dict | list) or not isinstance(key, str):
        return False
    if key in PLAIN_SETTINGS:
        return False
    return is_secret_param_name(key) or (secret_pair and key == "value")


def _hide_values(node: Any, *, hide_commands: bool = True) -> Any:
    """Copy *node*, hiding at any depth every value that can hold a secret.

    * The ``value`` of every entry in an ``env`` or ``httpHeaders`` list is
      hidden; the name stays. The entry's other fields are still walked, so
      a Secret or a command line nested in such an entry is hidden too.
    * Every ``command`` or ``args`` list becomes ``{"hidden": true, "bytes":
      <size>}``. With ``hide_commands=False`` (the Job previews) the
      command lines stay, so the approver reads what will run; only the
      secret values on them are hidden, with ``command`` and ``args`` read
      as one command line (:func:`_hide_command_line`).
    * Every dict with ``kind: Secret`` keeps only the key names of its
      ``data``, ``stringData`` and ``binaryData``. This catches a Secret nested in another
      object, not only a top-level Secret document.
    * The last-applied annotation is hidden in every ``annotations`` map,
      so a pod template's copy is caught too.
    * A number or a yes/no value under a secret name is hidden
      (:func:`_is_secret_scalar`). The shared redaction that runs after
      this hides a text under a secret name, but keeps such a value.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        command_line = {} if hide_commands else _hide_command_line(node)
        secret_pair = is_secret_name_value_pair(node)
        for key, item in node.items():
            if key in command_line:
                out[key] = command_line[key]
            elif _is_secret_scalar(key, item, secret_pair=secret_pair):
                out[key] = _HIDDEN
            elif key in _NAME_VALUE_LISTS and isinstance(item, list):
                out[key] = [
                    _hide_values({**entry, "value": _HIDDEN}, hide_commands=hide_commands)
                    if isinstance(entry, dict) and "value" in entry
                    else _hide_values(entry, hide_commands=hide_commands)
                    for entry in item
                ]
            elif key in _COMMAND_LISTS and isinstance(item, list):
                out[key] = {"hidden": True, "bytes": _parts_bytes(item)}
            elif key == "annotations" and isinstance(item, dict) and _LAST_APPLIED in item:
                out[key] = _hide_values(
                    {**item, _LAST_APPLIED: _HIDDEN}, hide_commands=hide_commands
                )
            else:
                out[key] = _hide_values(item, hide_commands=hide_commands)
        return _hide_secret_values(out) if out.get("kind") == "Secret" else out
    if isinstance(node, list):
        return [_hide_values(item, hide_commands=hide_commands) for item in node]
    return node


def _identity_only(doc: dict[str, Any], size: int) -> dict[str, Any]:
    """A document cut down to its identity, saying why the rest is left out."""
    raw_metadata = doc.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    identity = {key: metadata[key] for key in ("name", "namespace") if key in metadata}
    return {
        "apiVersion": doc.get("apiVersion"),
        "kind": doc.get("kind"),
        "metadata": identity,
        "echo_omitted": f"document is {size} bytes; the echo keeps at most "
        f"{MANIFEST_ECHO_MAX_BYTES} bytes of manifest",
    }


def _omitted_summary(count: int) -> dict[str, Any]:
    """The closing entry that counts the documents the echo has no room for."""
    return {
        "omitted_documents": count,
        "echo_omitted": f"{count} more document(s) not shown; the echo keeps at most "
        f"{MANIFEST_ECHO_MAX_BYTES} bytes of manifest",
    }


def _entry_bytes(entry: dict[str, Any]) -> int:
    """UTF-8 size of one echo entry as JSON, plus the two bytes it costs in the list.

    Summed over the entries, this is the size of the whole echo as JSON:
    each entry adds a ``, `` separator, and the ``[`` and ``]`` of the list
    take the place of the separator the last entry does not need.
    """
    return len(json.dumps(entry).encode("utf-8")) + 2


#: The text of a number YAML reads as not a number or as endless.
_NOT_FINITE = {"nan": ".nan", "inf": ".inf", "-inf": "-.inf"}


def _plain_scalar(value: Any) -> Any:
    """*value* as a value the database can store as JSON.

    YAML reads an unquoted date or time as a date object, ``.nan`` and
    ``.inf`` as numbers that JSON cannot hold, and ``!!binary`` as bytes.
    A date or time becomes its ISO text, as the client sends it; such a
    number becomes its YAML text; bytes become their base64 text, as the
    manifest has them. A null character in a text (``"\\0"`` in YAML)
    becomes the two characters ``\\0``, as the database cannot store it.
    Other text, a whole number, a finite number, a yes/no value and
    ``None`` stay; any other value becomes its text.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\\0")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _NOT_FINITE[str(value)]
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


def _plain(node: Any, done: dict[int, Any]) -> Any:
    """A copy of *node* with each key and value made plain (:func:`_plain_scalar`).

    A YAML alias names one object many times, or names an object inside
    itself: *done* holds the copy of each dict and list already made, so
    the copy keeps the same sharing and a loop ends.
    """
    if isinstance(node, dict):
        if id(node) not in done:
            out: dict[Any, Any] = {}
            done[id(node)] = out
            for key, item in node.items():
                out[_plain_scalar(key)] = _plain(item, done)
        return done[id(node)]
    if isinstance(node, list):
        if id(node) not in done:
            items: list[Any] = []
            done[id(node)] = items
            items.extend(_plain(item, done) for item in node)
        return done[id(node)]
    return _plain_scalar(node)


def card_documents(manifest: str) -> list[dict[str, Any]]:
    """The documents of *manifest* for the approval card, each value made plain.

    Every value is plain before anything is hidden or measured
    (:func:`_plain`). So a date or a ``.nan`` is hidden as text by each
    filter, and the stored request holds only values the database accepts.
    """
    done: dict[int, Any] = {}
    return [_plain(doc, done) for doc in _parse_manifest(manifest)]


def plain_copy(node: Any) -> Any:
    """A copy of *node* that the database can store, each value made plain (:func:`_plain`)."""
    return _plain(node, {})


def _identity_texts(doc: Any) -> list[Any]:
    """``apiVersion``, ``kind``, ``metadata.namespace`` and ``metadata.name`` of *doc*."""
    if not isinstance(doc, dict):
        return [None] * 4
    metadata = doc.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return [doc.get("apiVersion"), doc.get("kind"), metadata.get("namespace"), metadata.get("name")]


def names_as_on_card(manifest: str, message: str, *, redact: ManifestRedactor) -> str:
    """*message* with each object of *manifest* named as the card names it.

    The error of a failed dry run names the object it stopped at, with the
    text of the manifest (:func:`~.ops_write_dangerous.error_subject`). The
    card shows the ``apiVersion``, ``kind``, namespace and name of each
    document after the card's own steps (:func:`card_documents`) and
    *redact*: a redaction rule for a field path such as
    ``manifest.*.metadata.name`` hides a name there. So in *message* each
    object label is replaced by the label of the shown document, and each
    of these texts that the card shows in another form by that form (a
    message of the API server can name the object too), where no letter or
    digit touches it. The longest text wins where two start at one place.
    The HTTP status (``(HTTP 404 Not Found)``) stays as it is.
    """
    raw_docs = _parse_manifest(manifest)
    shown_docs = redact(card_documents(manifest))
    swaps: dict[str, str] = {}
    for raw, shown in zip(raw_docs, shown_docs, strict=False):
        for raw_text, shown_text in zip(_identity_texts(raw), _identity_texts(shown), strict=True):
            if isinstance(raw_text, str) and raw_text and raw_text != shown_text:
                swaps.setdefault(raw_text, _HIDDEN if shown_text is None else str(shown_text))
    for raw, shown in zip(raw_docs, shown_docs, strict=False):
        swaps[error_subject(raw)] = error_subject(shown)
    changed = sorted((text for text, to in swaps.items() if text != to), key=len, reverse=True)
    if not changed:
        return message
    # One pass, so a replaced text is not replaced again. A text is replaced
    # only where no letter or digit touches it, so a name ``4`` stays out of
    # ``8443``. The HTTP status of the error stays as it is.
    pattern = re.compile(
        "(?<![A-Za-z0-9])(?:" + "|".join(re.escape(text) for text in changed) + ")(?![A-Za-z0-9])"
    )
    parts = _HTTP_STATUS.split(message)
    return "".join(
        part if index % 2 else pattern.sub(lambda match: swaps[match.group(0)], part)
        for index, part in enumerate(parts)
    )


#: The HTTP status of a refused apply (``(HTTP 404 Not Found)``, from
#: ``ops_write_dangerous._reject``). :func:`names_as_on_card` keeps it.
_HTTP_STATUS = re.compile(r"(\(HTTP [0-9]{3}(?: [A-Za-z][A-Za-z -]*)?\))")


def apply_manifest_echo(manifest: str, *, redact: ManifestRedactor) -> list[dict[str, Any]]:
    """The ``k8s.apply`` manifest as one dict per document, safe to show.

    Secret ``data`` / ``stringData`` values, ``env`` and ``httpHeaders``
    values, ``command`` / ``args`` text and the last-applied annotation are
    hidden at any depth (see the module docstring). Then *redact*, the
    caller's shared params redaction, runs over all documents. Only then is
    the budget applied, because a redaction marker can be longer than the
    value it replaces: the stored echo is what is measured. The documents
    are kept in order, and every entry of the result counts toward
    :data:`MANIFEST_ECHO_MAX_BYTES`: a document that does not fit in full
    keeps only its identity, and once not even an identity fits, one last
    entry counts the documents left out.
    """
    docs = redact([_hide_values(doc) for doc in card_documents(manifest)])
    # Room is kept for the closing summary while documents may still follow,
    # so the whole echo stays within the budget, summary included.
    summary_bytes = _entry_bytes(_omitted_summary(len(docs)))
    echo: list[dict[str, Any]] = []
    used = 0
    for index, hidden in enumerate(docs):
        budget = MANIFEST_ECHO_MAX_BYTES - (summary_bytes if index < len(docs) - 1 else 0)
        size = _entry_bytes(hidden)
        if used + size <= budget:
            echo.append(hidden)
            used += size
            continue
        identity = _identity_only(hidden, size)
        identity_size = _entry_bytes(identity)
        if used + identity_size <= budget:
            echo.append(identity)
            used += identity_size
            continue
        echo.append(_omitted_summary(len(docs) - index))
        break
    return echo


def apply_subject(applied: Any) -> str | None:
    """Name the objects a ``k8s.apply`` touches, e.g. ``Deployment/prod/web``.

    Built from the dry-run's per-document summary (``kind``, ``namespace``,
    ``name``). A cluster-wide object has no namespace part. Names at most
    five objects and counts the rest. ``None`` when nothing is named.
    """
    if not isinstance(applied, list):
        return None
    names = [
        "/".join(str(entry[key]) for key in ("kind", "namespace", "name") if entry.get(key))
        for entry in applied
        if isinstance(entry, dict) and entry.get("kind") and entry.get("name")
    ]
    if not names:
        return None
    shown = ", ".join(names[:_SUBJECT_MAX_OBJECTS])
    rest = len(names) - _SUBJECT_MAX_OBJECTS
    return f"{shown} and {rest} more" if rest > 0 else shown


#: The container lists of a pod spec, in the order Kubernetes starts them.
_CONTAINER_LISTS = ("initContainers", "containers", "ephemeralContainers")

#: The key of the ``k8s.apply`` preview that names what each pod runs. Only a
#: person reads it (:func:`apply_run_commands`).
RUN_COMMANDS_KEY = "run_commands"


def _object_label(doc: dict[str, Any]) -> str:
    """``<Kind>/<namespace>/<name>`` of a manifest document; parts it lacks are left out."""
    raw_metadata = doc.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    parts = [doc.get("kind"), metadata.get("namespace"), metadata.get("name")]
    return "/".join(str(part) for part in parts if part)


def _runs_a_command(item: dict[str, Any]) -> bool:
    """True when *item* has a ``command`` or ``args`` list: the echo shows those only by size."""
    return any(isinstance(item.get(key), list) for key in _COMMAND_LISTS)


def _containers_in(node: Any) -> list[dict[str, Any]]:
    """Every container dict at any depth of *node*, each one once, in document order.

    A container is an entry of a container list of a pod spec, or any other
    dict with a ``command`` or ``args`` list, under any field: for example
    the ``container``, ``script`` or ``steps`` of a custom kind. The ``exec``
    of a hook or probe of a container is part of that container
    (:func:`_exec_commands`), not one of its own. A YAML alias can name one
    object many times or name itself, so each dict and list is walked once.
    """
    found: list[dict[str, Any]] = []
    listed: set[int] = set()
    walked: set[int] = set()
    stack: list[Any] = [node]

    def add(container: dict[str, Any]) -> None:
        if id(container) in listed:
            return
        listed.add(id(container))
        found.append(container)
        listed.update(id(handler["exec"]) for handler in _exec_handlers(container).values())

    while stack:
        item = stack.pop()
        if not isinstance(item, dict | list) or id(item) in walked:
            continue
        walked.add(id(item))
        if isinstance(item, list):
            stack.extend(reversed(item))
            continue
        if _runs_a_command(item):
            add(item)
        for key in _CONTAINER_LISTS:
            entries = item.get(key)
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict):
                    add(entry)
        stack.extend(reversed(list(item.values())))
    return found


#: The lifecycle hooks and the probes of a container that can run a command.
_EXEC_HOOKS = ("postStart", "preStop")
_EXEC_PROBES = ("startupProbe", "livenessProbe", "readinessProbe")


def _exec_handlers(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The hooks and probes of *container* that have an ``exec`` dict, keyed by where they run.

    The keys are ``lifecycle.postStart``, ``lifecycle.preStop`` and the probe
    names.
    """
    lifecycle = container.get("lifecycle")
    handlers = {
        f"lifecycle.{hook}": lifecycle.get(hook) if isinstance(lifecycle, dict) else None
        for hook in _EXEC_HOOKS
    }
    handlers.update({probe: container.get(probe) for probe in _EXEC_PROBES})
    return {
        where: handler
        for where, handler in handlers.items()
        if isinstance(handler, dict) and isinstance(handler.get("exec"), dict)
    }


def _exec_commands(container: dict[str, Any]) -> dict[str, list[Any]]:
    """The commands the hooks and probes of *container* run, keyed by where they run.

    Secret values are hidden. A hook or probe without an ``exec`` command is
    left out.
    """
    commands = {
        where: _hide_command_line(handler["exec"], p_after_programs=True).get("command")
        for where, handler in _exec_handlers(container).items()
    }
    return {where: command for where, command in commands.items() if command is not None}


def _run_command_entry(label: str, container: dict[str, Any]) -> dict[str, Any]:
    """What one container runs: its image, its command line and the commands of
    its hooks and probes, secret values hidden."""
    entry: dict[str, Any] = {"object": label, "container": container.get("name")}
    if "image" in container:
        entry["image"] = container.get("image")
    entry.update(_hide_command_line(container, p_after_programs=True))
    exec_commands = _exec_commands(container)
    if exec_commands:
        entry["exec_commands"] = exec_commands
    return entry


#: The keys of a :func:`apply_run_commands` entry that hold what it runs.
_RUN_KEYS = ("command", "args", "exec_commands")


def _command_text(entry: dict[str, Any]) -> str:
    """What *entry* runs as one text: its command line and its hook and probe commands."""
    parts = [f"{key}: {json.dumps(entry[key])}" for key in ("command", "args") if key in entry]
    exec_commands = entry.get("exec_commands")
    if isinstance(exec_commands, dict):
        parts.extend(f"{where}: {json.dumps(cmd)}" for where, cmd in exec_commands.items())
    return "; ".join(parts)


def _name_only(entry: dict[str, Any], text: str) -> dict[str, Any]:
    """*entry* without what it runs: the object, the container, the image and the ``cut`` note."""
    cut = {key: value for key, value in entry.items() if key not in _RUN_KEYS}
    cut["cut"] = (
        f"what this container runs is {len(text.encode('utf-8'))} bytes; the card shows "
        f"only its start, as the list keeps at most {MANIFEST_ECHO_MAX_BYTES} bytes"
    )
    cut["command_cut"] = ""
    return cut


def _cut_entry(entry: dict[str, Any], room: int) -> dict[str, Any] | None:
    """*entry* with what it runs cut to fit in *room* bytes, or ``None`` when it cannot fit.

    The object, the container and the image stay. What it runs becomes
    one text (:func:`_command_text`) in ``command_cut``, cut at the end,
    and ``cut`` says so.
    """
    text = _command_text(entry)
    cut = _name_only(entry, text)
    if _entry_bytes(cut) > room:
        return None
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        cut["command_cut"] = text[:middle]
        if _entry_bytes(cut) <= room:
            low = middle
        else:
            high = middle - 1
    cut["command_cut"] = text[:low]
    return cut


def _omitted_containers(count: int) -> dict[str, Any]:
    """The closing entry that counts the containers the list has no room for."""
    return {
        "omitted_containers": count,
        "echo_omitted": f"{count} more container(s) not shown; the list keeps "
        f"at most {MANIFEST_ECHO_MAX_BYTES} bytes",
    }


def apply_run_commands(manifest: str, *, redact: ManifestRedactor) -> list[dict[str, Any]]:
    """What each container of a ``k8s.apply`` manifest runs, safe to show a person.

    One entry per container, at any depth of each document
    (:func:`_containers_in`): the init, main and ephemeral containers of a
    pod spec, and every other dict with a ``command`` or ``args`` list, so
    each command line the echo shows only by size is named here. Each entry
    has the object (named as the card shows the document), the container
    name, the image, the
    ``command`` and ``args``, and in ``exec_commands`` the ``exec`` command of
    each lifecycle hook and probe (:func:`_exec_commands`). Secret values are
    hidden the way the Job previews hide them
    (:func:`~.op_classes.hide_command_secrets`). Then *redact*, the shared
    params redaction, runs over the entries. An empty list when
    no document runs a container.

    The entries count toward :data:`MANIFEST_ECHO_MAX_BYTES`. Room is kept
    for the name of every later container, so an entry that is too large
    is cut (:func:`_cut_entry`) and the next ones are still named.
    Only when the names alone do not fit does one closing entry count the
    containers left out.

    The ``manifest`` echo of the same preview shows only the size of each
    command line. This list is for the person who approves: an agent never
    reads it (:func:`_register_kubernetes_write_preview_builders`).
    """
    docs = card_documents(manifest)
    # Each label is built from the document as the card shows it, after
    # *redact*: a rule for a field path such as ``manifest.*.metadata.name``
    # hides the name in the label too.
    labels = [_object_label(doc) for doc in redact(docs)] if docs else []
    if len(labels) != len(docs):
        labels = [_HIDDEN] * len(docs)
    entries = [
        _run_command_entry(label, container)
        for doc, label in zip(docs, labels, strict=True)
        for container in _containers_in(doc)
    ]
    redacted = redact(entries) if entries else []
    # The least room each entry needs: whole, or cut down to its name.
    least = [
        min(_entry_bytes(entry), _entry_bytes(_name_only(entry, _command_text(entry))))
        for entry in redacted
    ]
    later_least = [0] * (len(least) + 1)
    for index in range(len(least) - 1, -1, -1):
        later_least[index] = later_least[index + 1] + least[index]
    if later_least[0] > MANIFEST_ECHO_MAX_BYTES:
        # The names do not all fit: keep room for the closing count instead.
        closing = _entry_bytes(_omitted_containers(len(redacted)))
        later_least = [closing] * len(redacted) + [0]
    shown: list[dict[str, Any]] = []
    used = 0
    for index, entry in enumerate(redacted):
        room = MANIFEST_ECHO_MAX_BYTES - used - later_least[index + 1]
        fitted = entry if _entry_bytes(entry) <= room else _cut_entry(entry, room)
        if fitted is None:
            shown.append(_omitted_containers(len(redacted) - index))
            break
        shown.append(fitted)
        used += _entry_bytes(fitted)
    return shown


def _run_commands_shown_to_agents() -> bool:
    """The ``run_commands`` and ``object_summary`` keys of the preview are for a person only."""
    return False


def _register_kubernetes_write_preview_builders() -> None:
    """Wire the Secret, Job create and CronJob run previews. Called at import time."""
    register_preview_builder("k8s.secret.create", _secret_create_preview)
    register_preview_builder("k8s.job.create", _job_create_preview)
    register_preview_builder("k8s.cronjob.run", _cronjob_run_preview)
    register_person_only_preview_keys(
        "k8s.apply",
        _run_commands_shown_to_agents,
        frozenset({RUN_COMMANDS_KEY, OBJECT_SUMMARY_KEY}),
    )


_register_kubernetes_write_preview_builders()
