# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``meho-settings.yaml`` file: its models, its parser and its checks.

The file is a Kubernetes-style frame (``apiVersion`` / ``kind`` /
``metadata`` / ``spec``) whose ``spec`` holds only what MEHO's database
holds: per tenant, the policies, the conventions and the targets. See
``docs/codebase/settings.md`` for the full shape and the list of what is
never in the file.

Every check here turns a problem into **one plain sentence that names the
path** (``spec.tenants[0].targets[3].host``) and never repeats the refused
value: the models set ``hide_input_in_errors=True`` and
:func:`format_validation_errors` builds sentences from the error location and
type only. :func:`parse_settings_file` runs every check and returns all the
mistakes together, so one preview lists everything that needs fixing.

Key semantics, shared by the parser and the service:

* An **absent** key leaves the stored value unchanged; an absent section
  (``policies`` / ``conventions`` / ``targets``) leaves the whole section
  unchanged.
* ``null`` in a policy means "use the instance default". Target and
  convention fields whose column cannot be empty do not accept ``null``.
* Nothing is ever deleted: an item that exists in MEHO but not in the file is
  reported as "not in file" and left alone.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.mail.allowlist import parse_recipient_allowlist
from meho_backplane.conventions.schemas import ConventionKind
from meho_backplane.targets.schemas import AuthModel

__all__ = [
    "API_VERSION",
    "KIND",
    "MAX_FILE_BYTES",
    "ParsedSettings",
    "SettingsConvention",
    "SettingsFile",
    "SettingsTarget",
    "SettingsTenant",
    "TenantPolicies",
    "find_secret_shaped_values",
    "format_validation_errors",
    "parse_settings_file",
    "render_path",
]

#: The one file format this release reads and writes.
API_VERSION: Final[str] = "meho.evoila.com/v1"
KIND: Final[str] = "Settings"

#: Refuse anything larger before parsing it. A settings file for a large
#: site (hundreds of targets, dozens of conventions) is well under 1 MiB.
MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Base for every file model: unknown keys are mistakes, values never echo."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class FlightRecorderPolicy(_Strict):
    """``policies.flight_recorder``: the three per-tenant capture columns."""

    enabled: StrictBool | None = None
    retention_days: Annotated[StrictInt, Field(ge=1, le=365)] | None = None
    agent_readable: StrictBool | None = None


class TenantPolicies(_Strict):
    """``spec.tenants[].policies``; ``null`` anywhere means the instance default."""

    announce_gate_enabled: StrictBool | None = None
    flight_recorder: FlightRecorderPolicy | None = None
    mail_recipient_allowlist: str | None = None

    @field_validator("mail_recipient_allowlist")
    @classmethod
    def _validate_allowlist_grammar(cls, value: str | None) -> str | None:
        # Same grammar as ``PATCH /api/v1/tenants/mail-recipient-policy``.
        if value is None or value == "":
            return value
        try:
            parse_recipient_allowlist(value)
        except ValueError:
            raise ValueError(
                "is not a valid recipient allowlist (comma-separated addresses "
                "or @domains, no spaces)"
            ) from None
        return value


class SettingsConvention(_Strict):
    """``spec.tenants[].conventions[]``: one tenant convention, keyed by ``slug``.

    ``title`` and ``kind`` are needed only when the convention is new; the
    service reports their absence then. ``kind`` cannot change on import (the
    convention routes cannot change it either).
    """

    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(default="", min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=64_000)
    kind: ConventionKind = ConventionKind.REFERENCE
    priority: StrictInt = Field(default=0, ge=-32768, le=32767)


class SettingsTarget(_Strict):
    """``spec.tenants[].targets[]``: the ``TargetCreate`` keys, keyed by ``name``.

    The same entry shape as ``meho targets export`` (#4048). ``secret_ref`` is
    a path, never a password; absent means "MEHO works it out from the name"
    for a new target and "unchanged" for an existing one. Defaults below only
    apply to a new target.
    """

    name: str = Field(min_length=1, max_length=200)
    product: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=100)
    host: str = Field(min_length=1, max_length=512)
    port: Annotated[StrictInt, Field(ge=1, le=65535)] | None = None
    fqdn: str | None = None
    aliases: list[str] = Field(default_factory=list)
    auth_model: AuthModel = AuthModel.SHARED_SERVICE_ACCOUNT
    secret_ref: str | None = None
    #: ``{connector_field: source_field}`` (#3711): field names, never values.
    #: The file holds the whole map: an import removes stored entries it leaves out.
    secret_field_map: dict[str, str] = Field(default_factory=dict)
    verify_tls: StrictBool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = Field(default=None, max_length=512)
    vpn_required: StrictBool = False
    preferred_impl_id: str | None = Field(default=None, max_length=200)
    flight_recorder_capture: StrictBool | None = None
    notes: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class SettingsTenant(_Strict):
    """``spec.tenants[]``. ``id`` is the tenant's Keycloak id (a UUID).

    ``id`` may be left out only in a one-tenant file; the tenant is then the
    importing operator's own.
    """

    id: UUID | None = None
    policies: TenantPolicies | None = None
    conventions: list[SettingsConvention] | None = None
    targets: list[SettingsTarget] | None = None


class SettingsSpec(_Strict):
    tenants: list[SettingsTenant]


class ExportedFrom(_Strict):
    mehoVersion: str | None = None  # noqa: N815 -- file key spelling
    migrationHead: str | None = None  # noqa: N815 -- file key spelling


class SettingsMetadata(_Strict):
    """Written by export, ignored by import except for the version warning."""

    exportedAt: datetime | str | None = None  # noqa: N815 -- file key spelling
    exportedFrom: ExportedFrom | None = None  # noqa: N815 -- file key spelling


class SettingsFile(_Strict):
    """The whole file. ``apiVersion`` is checked separately for a clear sentence."""

    apiVersion: str  # noqa: N815 -- file key spelling
    kind: Literal["Settings"]
    metadata: SettingsMetadata | None = None
    spec: SettingsSpec


# ---------------------------------------------------------------------------
# Paths and sentences
# ---------------------------------------------------------------------------


def render_path(loc: tuple[int | str, ...] | list[int | str]) -> str:
    """Render a location tuple as ``spec.tenants[0].targets[3].host``.

    A key name that itself looks like a secret is shown as ``<redacted>``, so a
    sentence about it never repeats it.
    """
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
            continue
        name = "<redacted>" if _secret_kind(str(part)) else str(part)
        out += f".{name}" if out else name
    return out or "(file)"


#: Error types whose pydantic message is good enough after the path.
_TYPE_SENTENCES: Final[dict[str, str]] = {
    "missing": "is required",
    "extra_forbidden": "is not a known key",
    "string_type": "must be text",
    "bool_type": "must be true or false",
    "int_type": "must be a whole number",
    "list_type": "must be a list",
    "dict_type": "must be a mapping of named keys",
    "model_type": "must be a mapping of named keys",
    "model_attributes_type": "must be a mapping of named keys",
    "uuid_type": "must be a UUID",
    "uuid_parsing": "must be a UUID",
    "string_too_short": "must not be empty",
    "literal_error": "must be Settings",
    "none_required": "must be empty",
}


def _sentence_for(err: Any, prefix: tuple[int | str, ...]) -> str:
    loc = prefix + tuple(err["loc"])
    path = render_path(loc)
    etype = err["type"]
    if etype in _TYPE_SENTENCES:
        text = _TYPE_SENTENCES[etype]
    elif etype == "value_error":
        # Our own validators raise "is not ..." phrases; foreign ones
        # (TargetCreate's SSRF guard, CA-pin parser) a full message.
        text = str(err.get("ctx", {}).get("error", err["msg"]))
    else:
        text = err["msg"][:1].lower() + err["msg"][1:]
    text = text.rstrip(".")
    return f"`{path}` {text}." if text.startswith(("is ", "must ")) else f"`{path}`: {text}."


def format_validation_errors(
    exc: ValidationError,
    prefix: tuple[int | str, ...] = (),
) -> list[str]:
    """Turn a pydantic :class:`ValidationError` into one sentence per problem.

    Uses only the location and the error type / message; never the input, so a
    refused value is not shown back. ``prefix`` is prepended to every location
    (used when a nested model is validated on its own).
    """
    sentences: list[str] = []
    for err in exc.errors(include_input=False, include_url=False):
        sentence = _sentence_for(err, prefix)
        if sentence not in sentences:
            sentences.append(sentence)
    return sentences


# ---------------------------------------------------------------------------
# YAML parsing with repeated-key detection
# ---------------------------------------------------------------------------


class _DuplicateKeyLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml has no stubs here
    """``SafeLoader`` that records repeated mapping keys instead of dropping them.

    Plain ``yaml.safe_load`` keeps the last of two equal keys without a word,
    so a file with two ``host:`` lines would import whichever came second.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.duplicates: list[tuple[str, int]] = []
        self.unreadable: list[int] = []
        self.refused: list[tuple[int, str]] = []

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node:
        # Refuse anchors (``&name``) and aliases (``*name``) before any node is
        # built: each alias would count as a full copy in every later walk.
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _AnchorRefusedError(problem_mark=event.start_mark)
        return super().compose_node(parent, index)


class _AnchorRefusedError(yaml.composer.ComposerError):  # type: ignore[misc]  # yaml has no stubs here
    """A YAML anchor or alias: the settings file writes every value out."""


def _construct_mapping_checked(loader: _DuplicateKeyLoader, node: yaml.MappingNode) -> Any:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            repeated = key in seen
            seen.add(key)
        except TypeError:
            continue  # unhashable key: construct_mapping raises the real error
        if repeated:
            loader.duplicates.append((str(key), key_node.start_mark.line + 1))
    return loader.construct_mapping(node, deep=True)


def _construct_timestamp_checked(loader: _DuplicateKeyLoader, node: yaml.ScalarNode) -> Any:
    # A date-shaped value that is no real date (``2026-99-99``) makes PyYAML
    # raise ValueError; record the line and keep the text instead.
    try:
        return loader.construct_yaml_timestamp(node)
    except (ValueError, OverflowError):
        loader.unreadable.append(node.start_mark.line + 1)
        return loader.construct_scalar(node)


#: YAML types no setting uses, each of which would hide its contents from the
#: secret check: ``binary`` holds encoded bytes (a base64 JWT comes out as text
#: later), ``pairs`` and ``omap`` build tuples and ``set`` builds a set, which
#: then reach JSON as plain lists.
_REFUSED_YAML_TYPES: Final[tuple[str, ...]] = ("binary", "omap", "pairs", "set")


def _construct_refused(loader: _DuplicateKeyLoader, node: yaml.Node) -> None:
    # Record the line and build nothing, so the value is never decoded.
    loader.refused.append((node.start_mark.line + 1, node.tag.rsplit(":", 1)[-1]))


for _yaml_type in _REFUSED_YAML_TYPES:
    _DuplicateKeyLoader.add_constructor(f"tag:yaml.org,2002:{_yaml_type}", _construct_refused)
_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_checked,
)
_DuplicateKeyLoader.add_constructor("tag:yaml.org,2002:timestamp", _construct_timestamp_checked)


# ---------------------------------------------------------------------------
# Secret-shaped values
# ---------------------------------------------------------------------------

_PEM_PRIVATE_KEY_RE: Final = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----.+?-----END ([A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
_JWT_RE: Final = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_BASE64_RE: Final = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
#: Raw symmetric-key sizes (AES-128/192/256, HMAC-384/512, Fernet = 32).
_BINARY_KEY_LENGTHS: Final[frozenset[int]] = frozenset({16, 24, 32, 48, 64})
#: A mask is never "keep the stored value"; it is refused like any other secret.
_MASK_RE: Final = re.compile(
    r"^(\*+|<\s*(keep|redacted|masked|secret|unchanged|hidden)\s*>)$", re.I
)


def _compact(text: str) -> str:
    # Line breaks only: base64 is often wrapped, but a value with spaces is
    # prose, and joining prose would turn a sentence into "valid base64".
    return re.sub(r"[\r\n]+", "", text.strip())


def _b64_decode(text: str) -> bytes | None:
    compact = _compact(text)
    if len(compact) < 22 or not _BASE64_RE.match(compact):
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        if "-" in compact or "_" in compact:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _is_binary(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return any(ord(c) < 0x20 and c not in "\t\r\n" for c in text)


def _looks_like_der_private_key(data: bytes) -> bool:
    # PKCS#1 / PKCS#8 / SEC1 private keys open with SEQUENCE then INTEGER 0
    # (``02 01 00``) within the first few bytes; X.509 certificates do not.
    return len(data) >= 64 and data[0] == 0x30 and b"\x02\x01\x00" in data[2:8]


def _jwt_shaped(text: str) -> bool:
    for match in _JWT_RE.finditer(text):
        header = match.group(0).split(".", 1)[0]
        try:
            decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        except (binascii.Error, ValueError):
            continue
        if isinstance(decoded, dict) and "alg" in decoded:
            return True
    return False


def _secret_kind(text: str) -> str | None:
    """Name what *text* looks like ("a private key", "a JWT", ...) or ``None``."""
    if _MASK_RE.match(text.strip()):
        return "a mask"
    if _PEM_PRIVATE_KEY_RE.search(text):
        return "a private key"
    if _jwt_shaped(text):
        return "a JWT"
    decoded = _b64_decode(text)
    if decoded is None:
        return None
    if not _is_binary(decoded):
        inner = decoded.decode("utf-8")
        if _PEM_PRIVATE_KEY_RE.search(inner):
            return "a base64-encoded private key"
        if _jwt_shaped(inner):
            return "a base64-encoded JWT"
        return None
    if _looks_like_der_private_key(decoded):
        return "a base64-encoded private key"
    compact = _compact(text)
    mixed = (
        any(c.isupper() for c in compact)
        and any(c.islower() for c in compact)
        and any(c.isdigit() for c in compact)
    )
    if len(decoded) in _BINARY_KEY_LENGTHS and mixed:
        return "a base64-encoded key"
    return None


def contains_secret(text: str) -> bool:
    """Whether *text*, or any word in it, looks like a secret (used to scrub messages)."""
    return bool(_secret_kind(text)) or any(
        _secret_kind(word) for word in re.split(r"[\s'\"`,;()]+", text) if word
    )


def find_secret_shaped_values(data: Any, loc: tuple[int | str, ...] = ()) -> list[str]:
    """Walk *data* and return one sentence per string that looks like a secret.

    Checks every string value and every mapping key: a full ``PRIVATE KEY``
    PEM block, a JWT, the same after one base64 decode, base64 that decodes
    to a raw or DER key, and masks such as ``*`` or ``<keep>``. A
    ``CERTIFICATE`` block passes, so ``tls_ca_pin`` works. Under ``extras``, a
    key named like a credential (``password``, ``api_key``, ...) is refused
    whatever its value, since an ordinary password has no shape to find; the
    name rule is :func:`~meho_backplane.broadcast.events.is_secret_param_name`,
    the one the broadcast scrub and the approval echo use. The
    sentence names the path only, never the value.
    """
    sentences: list[str] = []
    if isinstance(data, dict):
        under_extras = "extras" in loc
        for key, value in data.items():
            child = (*loc, str(key))
            if isinstance(key, str) and (kind := _secret_kind(key)):
                sentences.append(_secret_sentence(child, kind, is_key=True))
            elif under_extras and isinstance(key, str) and is_secret_param_name(key):
                sentences.append(
                    f"`{render_path(child)}` is named like a credential; the settings file "
                    f"never carries secrets, so store it in OpenBao and refer to it with "
                    f"`secret_ref`."
                )
                continue
            sentences.extend(find_secret_shaped_values(value, child))
    elif isinstance(data, list | tuple | set | frozenset):
        for index, item in enumerate(data):
            sentences.extend(find_secret_shaped_values(item, (*loc, index)))
    elif isinstance(data, str) and (kind := _secret_kind(data)):
        sentences.append(_secret_sentence(loc, kind, is_key=False))
    return sentences


def _secret_sentence(loc: tuple[int | str, ...], kind: str, *, is_key: bool) -> str:
    what = "key name" if is_key else "value"
    if kind == "a mask":
        return (
            f"`{render_path(loc)}` holds a mask; a mask never means "
            f'"keep the stored value", so write the real setting or leave the key out.'
        )
    return (
        f"`{render_path(loc)}` has a {what} that looks like {kind}; the settings "
        f"file never carries secrets, so store it in OpenBao and refer to it with "
        f"`secret_ref`."
    )


# ---------------------------------------------------------------------------
# Whole-file parse
# ---------------------------------------------------------------------------


@dataclass
class ParsedSettings:
    """Result of :func:`parse_settings_file`: the model (if valid) and every mistake."""

    model: SettingsFile | None
    mistakes: list[str] = field(default_factory=list)
    raw: Any = None


def _repeated_item_mistakes(model: SettingsFile) -> list[str]:
    """Two tenants with one id, or two conventions / targets with one key."""
    mistakes: list[str] = []
    seen_tenants: dict[UUID | None, int] = {}
    for ti, tenant in enumerate(model.spec.tenants):
        if tenant.id in seen_tenants:
            mistakes.append(
                f"`spec.tenants[{ti}]` repeats the tenant at "
                f"`spec.tenants[{seen_tenants[tenant.id]}]`; list each tenant once."
            )
        else:
            seen_tenants[tenant.id] = ti
        for section, attr in (("conventions", "slug"), ("targets", "name")):
            seen: dict[str, int] = {}
            for i, item in enumerate(getattr(tenant, section) or []):
                key = getattr(item, attr)
                if key in seen:
                    mistakes.append(
                        f"`spec.tenants[{ti}].{section}[{i}].{attr}` repeats "
                        f"`spec.tenants[{ti}].{section}[{seen[key]}]`; list each one once."
                    )
                else:
                    seen[key] = i
    return mistakes


def _missing_tenant_id_mistakes(model: SettingsFile) -> list[str]:
    tenants = model.spec.tenants
    if len(tenants) < 2:
        return []
    return [
        f"`spec.tenants[{ti}].id` is required when the file names more than one tenant."
        for ti, tenant in enumerate(tenants)
        if tenant.id is None
    ]


def _yaml_error_sentence(exc: yaml.MarkedYAMLError) -> str:
    # Only the line: PyYAML's problem text can quote the input (an unknown
    # tag, say), and nothing here has been checked for secrets yet.
    mark = exc.problem_mark or exc.context_mark
    where = f" on line {mark.line + 1}" if mark is not None else ""
    if isinstance(exc, _AnchorRefusedError):
        return (
            f"The file is not valid YAML{where}: anchors (`&`) and aliases (`*`) "
            "are not accepted; write each value out."
        )
    return f"The file is not valid YAML{where}."


def _load_yaml(content: bytes) -> tuple[Any, _DuplicateKeyLoader] | str:
    """Size, encoding and YAML syntax: the loaded data and its loader, or one mistake."""
    if len(content) > MAX_FILE_BYTES:
        return f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB."
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "The file is not UTF-8 text."

    try:
        loader = _DuplicateKeyLoader(text)
    except yaml.YAMLError:
        # The reader refuses control characters before parsing starts.
        return "The file is not valid YAML: it holds characters YAML refuses."
    try:
        return loader.get_single_data(), loader
    except yaml.MarkedYAMLError as exc:
        return _yaml_error_sentence(exc)
    except (yaml.YAMLError, ValueError, TypeError, OverflowError, RecursionError):
        # Any other value PyYAML cannot convert; never a server error.
        return "The file is not valid YAML."
    finally:
        loader.dispose()


def parse_settings_file(content: bytes) -> ParsedSettings:
    """Parse and check *content*; collect every mistake as one sentence each.

    Order: size and encoding, YAML syntax (anchors and aliases are refused),
    repeated keys, refused YAML types, ``apiVersion``, secret-shaped values,
    the model, repeated items. All checks that can run do run, so a file with
    an unknown key, an unknown ``apiVersion`` and a
    target with no host yields three sentences. The one exception is a
    secret: once one is found nothing else is checked and no model is
    returned, so no later check (or the import) ever handles the value.
    """
    loaded = _load_yaml(content)
    if isinstance(loaded, str):
        return ParsedSettings(None, [loaded])
    raw, loader = loaded

    mistakes = [
        f"Line {line}: the key `{render_path((key,))}` appears twice in the same block."
        for key, line in loader.duplicates
    ]
    mistakes.extend(
        f"Line {line}: the value looks like a date but is not a real date."
        for line in loader.unreadable
    )
    if loader.refused:
        # Stop like a secret does: the value was never decoded or checked.
        mistakes.extend(
            f"Line {line}: `!!{yaml_type}` values are not accepted; the settings file "
            f"holds only text, numbers, true/false, lists and mappings."
            for line, yaml_type in dict.fromkeys(loader.refused)
        )
        return ParsedSettings(None, mistakes)
    if not isinstance(raw, dict):
        mistakes.append("The file must be a mapping with apiVersion, kind, metadata and spec.")
        return ParsedSettings(None, mistakes, raw)

    api_version = raw.get("apiVersion")
    if api_version is not None and api_version != API_VERSION:
        mistakes.append(f"`apiVersion` is not a file format this MEHO reads; use `{API_VERSION}`.")
    secrets = find_secret_shaped_values(raw)
    if secrets:
        # Stop here: no later check may see, and so repeat, a refused value.
        return ParsedSettings(None, [*mistakes, *secrets])

    model: SettingsFile | None = None
    try:
        model = SettingsFile.model_validate(raw)
    except ValidationError as exc:
        mistakes.extend(format_validation_errors(exc))
    if model is not None:
        mistakes.extend(_repeated_item_mistakes(model))
        mistakes.extend(_missing_tenant_id_mistakes(model))
    if mistakes:
        return ParsedSettings(None if model is None else model, mistakes, raw)
    return ParsedSettings(model, [], raw)
