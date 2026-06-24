# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure-function OpenAPI 3.0/3.1 spec parser.

Reads a YAML or JSON OpenAPI document and returns a list of
:class:`EndpointDescriptorProto`. T2 (#403) consumes the output to
upsert :class:`meho_backplane.db.models.EndpointDescriptor` rows.

No DB session, no LLM call, no event loop. The only side effects are
the file read or HTTP GET that pulls the spec bytes — the parsing
itself is pure.

YAML parsing prefers ``yaml.CSafeLoader`` (LibYAML) for speed on large
specs and falls back to the pure-Python ``yaml.SafeLoader`` when
LibYAML isn't built into the local PyYAML wheel. Both loaders refuse
the unsafe constructors that turn YAML into RCE.

Supported spec dialects:

* OpenAPI 3.0.x (the vCenter / vi-json baseline at v0.2).
* OpenAPI 3.1.x (jsonschema 2020-12-compatible; newer customer specs).

Out of scope (no conversion performed in-process):

* Swagger 2.0 — rejected with an actionable :exc:`UnsupportedSpecError`
  that names the conversion path (convert to OpenAPI 3.x with
  ``swagger2openapi`` / ``converter.swagger.io`` and re-ingest). The
  parser stays 3.x-only on purpose: the maintained 2.0→3.0 converters
  are Node/web-service tools, and a hand-rolled converter is a large
  correctness surface the operator review queue can't backstop. See
  the Harbor 2.x ``swagger.yaml`` exemplar (#1532).
* GraphQL SDL / WSDL / protobuf — separate parsers; v0.2.next.
* Cross-document ``$ref`` (``$ref: "other.yaml#/..."``) — raises
  :exc:`UnsupportedSpecError`.
* Deep ``$ref`` resolution — only top-level refs under each parameter
  / body schema are inlined; nested ``$ref`` strings are preserved
  verbatim for the dispatcher's jsonschema validator to resolve at
  call time.

Known limitation: when an operation declares two parameters with the
same ``name`` in different ``in`` locations (e.g. a ``cluster`` path
param **and** a ``cluster`` query param on the same op), the
flattened ``parameter_schema`` keys collide on the property name and
only the latter wins. OpenAPI 3.1 does allow this combination, but
the vCenter / NSX / SDDC Manager specs in scope for v0.2 never
exercise it. T2's registration helper logs a warning when it spots a
collision; T1 produces what the spec literally says.
"""

from __future__ import annotations

import io
import ipaddress
import json
import re
import socket
from collections.abc import Iterable
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from meho_backplane.operations._rfc6570 import split_path_operator
from meho_backplane.operations.ingest.exceptions import (
    InvalidSchemaError,
    InvalidSpecError,
    UnsupportedSpecError,
    UpstreamNotSpecError,
)
from meho_backplane.operations.ingest.refs import (
    normalize_boolean_schema as _normalize_boolean_schema,
)
from meho_backplane.operations.ingest.refs import (
    resolve_shallow_ref as _resolve_shallow_ref,
)
from meho_backplane.operations.ingest.refs import (
    select_media_type_schema as _select_media_type_schema,
)
from meho_backplane.operations.ingest.schemas import (
    EndpointDescriptorProto,
    SafetyLevel,
)

__all__ = [
    "InvalidSchemaError",
    "InvalidSpecError",
    "UnsupportedSpecError",
    "UpstreamNotSpecError",
    "detect_spec_format",
    "parse_openapi",
    "read_spec_info_version",
]


try:
    # LibYAML-backed loader — ~5-10x faster on the 10 MB ``vi-json.yaml``
    # spec than the pure-Python fallback. Optional because PyYAML wheels
    # for some platforms / Python versions ship without LibYAML support.
    _YamlLoader: type[yaml.SafeLoader] = yaml.CSafeLoader
except AttributeError:  # pragma: no cover — PyYAML always ships SafeLoader
    _YamlLoader = yaml.SafeLoader


# OpenAPI 3.0.x and 3.1.x are the two supported major.minor pairs.
# Patch level (the third digit) is accepted as-is — semver-style
# bugfix versions never change the parser's contract.
_SUPPORTED_OPENAPI_RE = re.compile(r"^3\.(0|1)(\.\d+)?$")

# Operator-facing remediation appended to the Swagger-2.0 rejection.
# The parser stays OpenAPI-3.x-only on purpose (no spec-conversion
# dependency in the Python backend — the de-facto 2.0→3.0 converters
# are Node/web-service tools, and a hand-rolled converter is a large
# correctness surface the review queue can't backstop). Instead of a
# bare "not supported", the rejection names the concrete conversion
# path so the operator can self-serve: run a converter, then re-ingest
# the OpenAPI-3.x output through the same path. ``swagger2openapi`` /
# the hosted ``converter.swagger.io`` are the maintained converters.
_SWAGGER_2_CONVERSION_REMEDIATION = (
    "convert it to OpenAPI 3.x first (e.g. the swagger2openapi CLI "
    "`npx swagger2openapi swagger.yaml -o openapi.yaml`, or the hosted "
    "converter at https://converter.swagger.io/), then ingest the "
    "converted 3.x document"
)

# Path-parameter placeholders look like ``{cluster}`` / ``{vm-id}`` /
# ``{filter.names}``. Compiled once and reused per operation.
_PATH_PARAM_RE = re.compile(r"\{([^{}/]+)\}")

# OpenAPI 3.x operation-level keys other than the verbs. Used to skip
# ``parameters`` / ``summary`` / ``description`` while iterating verbs.
_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})

# Verbs the parser maps to ``safety_level="caution"`` by default.
# Anything outside this set + the ``dangerous`` set below falls into
# the ``safe`` bucket.
_CAUTION_VERBS = frozenset({"POST", "PUT", "PATCH"})
_DANGEROUS_VERBS = frozenset({"DELETE"})

# Default HTTP timeouts for spec fetches. Specs sit behind CDN URLs
# and rarely take more than a couple of seconds; a 30 s ceiling keeps
# pathological cases from hanging an ingest.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Maximum number of redirect hops the spec fetcher will follow. Each hop
# is re-validated against the destination guard before the next request
# fires, so a chain longer than this cap is rejected with InvalidSpecError.
_MAX_REDIRECTS = 5

# Hard cap on spec response body size. OpenAPI specs for the largest VMware
# suites (vi-json.yaml) run to ~10 MB; 20 MB gives comfortable headroom
# while preventing a redirect to a large internal endpoint from exhausting
# pod memory.
_MAX_SPEC_BYTES = 20 * 1024 * 1024  # 20 MiB


# Content-Type prefixes the upstream-fetch path accepts as spec-shaped.
# OpenAPI specs are served as ``application/json`` (the modern default),
# ``application/x-yaml`` / ``application/yaml`` / ``text/yaml`` /
# ``text/x-yaml`` (YAML's wandering history of registered + provisional
# media types), or ``text/plain`` (the GitHub-raw fallback most CI specs
# end up on). Anything else -- ``text/html`` from a developer-portal
# landing page, ``application/octet-stream`` from a misconfigured host
# -- is rejected with :exc:`UpstreamNotSpecError` so the operator gets
# a structured 422 instead of an opaque YAML parse error at line 33.
_ACCEPTED_SPEC_CONTENT_TYPES: tuple[str, ...] = (
    "application/json",
    "application/yaml",
    "application/x-yaml",
    "text/yaml",
    "text/x-yaml",
    "text/plain",  # raw.githubusercontent.com serves YAML as text/plain
)


def detect_spec_format(content: bytes) -> str:
    """Return ``"json"`` if ``content`` looks like JSON, else ``"yaml"``.

    Sniffs the first non-whitespace byte: ``{`` or ``[`` → JSON;
    anything else → YAML. Cheap and reliable — both YAML and JSON
    serialised OpenAPI specs are tens of megabytes, and probing them
    fully would mean a wasted parse.

    Args:
        content: Raw spec bytes.

    Returns:
        ``"json"`` or ``"yaml"``.
    """
    for byte in content:
        if byte in (0x20, 0x09, 0x0A, 0x0D, 0xEF, 0xBB, 0xBF):  # ws + UTF-8 BOM bytes
            continue
        return "json" if byte in (0x7B, 0x5B) else "yaml"  # '{' or '['
    return "yaml"  # empty / whitespace-only document — YAML's parse error is clearer


def parse_openapi(
    spec_path_or_uri: str,
    *,
    spec_source: str | None = None,
    content: str | None = None,
) -> list[EndpointDescriptorProto]:
    """Parse an OpenAPI 3.0 or 3.1 spec into a list of
    :class:`EndpointDescriptorProto` rows.

    Args:
        spec_path_or_uri: ``https://`` URL pointing at the spec.
            Only the ``https`` scheme is accepted on the
            network-facing ingest path; non-``https`` URIs
            (including ``http://``, ``file://``, and bare paths)
            raise :exc:`InvalidSpecError`. The destination is
            validated against the SSRF guard in
            :func:`_assert_fetchable_remote_url` before any
            network connection is opened.
        spec_source: Optional logical-source tag (e.g.
            ``"spec:vcenter.yaml"``) injected into each row's
            ``tags`` so operators can distinguish rows when a single
            connector ingests multiple specs (vCenter merges
            ``vcenter.yaml`` and ``vi-json.yaml``).
        content: Optional inline spec text. When the CLI uploads it
            for a ``docs:`` / ``file://`` source, it is used verbatim
            instead of fetching *spec_path_or_uri*; the https-only SSRF
            guard then applies only to the no-content (URL) path.

    Returns:
        A list of :class:`EndpointDescriptorProto`. One entry per
        (method, path) operation; path-level / spec-level metadata
        is not represented here. Paths with no operations are
        silently skipped.

    Raises:
        InvalidSpecError: Document is not a mapping, lacks ``paths``,
            the URI scheme is not ``https``, or the resolved
            destination is a private/loopback/link-local/reserved
            address.
        UnsupportedSpecError: Spec version is not 3.0.x / 3.1.x, or the
            document references a cross-document ``$ref``.
        UpstreamNotSpecError: HTTP fetch succeeded (2xx) but the
            response's ``Content-Type`` declared a non-spec media type
            (e.g. ``text/html`` from a developer-portal landing page).
            Raised before any decoding so callers see a precise
            "upstream isn't a spec" diagnostic instead of an opaque
            YAML / JSON parse error.
        InvalidSchemaError: A local ``$ref`` points at a missing
            component, or a structurally unsupported shape is used.
        yaml.YAMLError: Malformed YAML — bubbles up from the loader.
        json.JSONDecodeError: Malformed JSON — bubbles up.
        httpx.HTTPError: HTTP fetch failure for URL inputs.
    """
    spec_bytes = _load_spec_bytes(spec_path_or_uri, content=content)
    spec = _decode_spec(spec_bytes)
    _validate_openapi_version(spec)

    paths = spec.get("paths")
    if paths is None:
        raise InvalidSpecError("OpenAPI document has no 'paths' key")
    if not isinstance(paths, dict):
        raise InvalidSpecError(f"'paths' must be a mapping, got {type(paths).__name__}")

    components = spec.get("components") or {}
    if not isinstance(components, dict):
        raise InvalidSpecError(f"'components' must be a mapping, got {type(components).__name__}")
    component_schemas = components.get("schemas") or {}
    if not isinstance(component_schemas, dict):
        raise InvalidSpecError(
            f"'components.schemas' must be a mapping, got {type(component_schemas).__name__}"
        )
    component_parameters = components.get("parameters") or {}
    if not isinstance(component_parameters, dict):
        raise InvalidSpecError(
            f"'components.parameters' must be a mapping, got {type(component_parameters).__name__}"
        )
    component_responses = components.get("responses") or {}
    if not isinstance(component_responses, dict):
        raise InvalidSpecError(
            f"'components.responses' must be a mapping, got {type(component_responses).__name__}"
        )
    component_request_bodies = components.get("requestBodies") or {}
    if not isinstance(component_request_bodies, dict):
        raise InvalidSpecError(
            f"'components.requestBodies' must be a mapping, got "
            f"{type(component_request_bodies).__name__}"
        )

    return list(
        _iter_operations(
            paths=paths,
            component_schemas=cast(dict[str, Any], component_schemas),
            component_parameters=cast(dict[str, Any], component_parameters),
            component_responses=cast(dict[str, Any], component_responses),
            component_request_bodies=cast(dict[str, Any], component_request_bodies),
            spec_source=spec_source,
        )
    )


def read_spec_info_version(spec_path_or_uri: str, *, content: str | None = None) -> str | None:
    """Return the spec's ``info.version`` string, or ``None`` if absent.

    Lightweight companion to :func:`parse_openapi` for the ingest
    pipeline's spec-vs-label cross-check (G0.9-T8). Loads the spec
    bytes the same way :func:`parse_openapi` does, decodes them, runs
    the supported-OpenAPI-version gate so callers don't need to
    re-validate, and returns ``info.version`` verbatim.

    Returning the raw string (rather than a parsed
    :class:`packaging.version.Version`) keeps this function spec-only;
    the pipeline layer handles PEP 440 parsing and the
    classification ladder against the operator-supplied label.

    Args:
        spec_path_or_uri: ``https://`` URL — same scheme constraint
            as :func:`parse_openapi`. The SSRF/destination guard in
            :func:`_assert_fetchable_remote_url` fires here too.
        content: Optional inline spec text, used verbatim when the CLI
            uploaded it for a ``docs:`` / ``file://`` source -- same
            semantics as :func:`parse_openapi`'s ``content``.

    Returns:
        The ``info.version`` string when present; ``None`` when
        ``info`` or ``info.version`` is missing or not a string. The
        cross-check at the pipeline layer treats ``None`` as "no
        cross-check possible" rather than as a mismatch, so older
        specs missing ``info.version`` keep ingesting under whatever
        operator label.

    Raises:
        InvalidSpecError: Document is not a mapping, URI scheme is not
            ``https``, or destination guard fires. Same shape
            :func:`parse_openapi` raises.
        UnsupportedSpecError: Spec version is not 3.0.x / 3.1.x — the
            same gate the parser enforces; surfaced here so callers
            can fail fast before touching ``info.version``.
        UpstreamNotSpecError: HTTP fetch succeeded but the response
            declared a non-spec media type. Same shape
            :func:`parse_openapi` raises; see that function's
            docstring for context.
        yaml.YAMLError: Malformed YAML — bubbles up from the loader.
        json.JSONDecodeError: Malformed JSON — bubbles up.
        httpx.HTTPError: HTTP fetch failure for URL inputs.
    """
    spec_bytes = _load_spec_bytes(spec_path_or_uri, content=content)
    spec = _decode_spec(spec_bytes)
    _validate_openapi_version(spec)
    info = spec.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    if not isinstance(version, str) or not version:
        return None
    return version


def _assert_fetchable_remote_url(url: str) -> None:
    """Validate that ``url`` is safe to fetch from the backplane's network position.

    Enforces two invariants before any socket is opened:

    1. **Scheme allowlist** — only ``https`` is permitted on the
       network-facing ingest path. ``http`` is rejected because it
       cannot protect the transport and is indistinguishable from a
       redirect-bypass target after a single 30x hop. ``file://``,
       bare paths, and every other scheme are rejected because no
       local-file read is reachable from the API/MCP-driven
       ``IngestRequest.uri``.

    2. **Destination guard** — the hostname is resolved via
       ``socket.getaddrinfo`` and every returned address is checked
       with :mod:`ipaddress`. Any candidate that is private,
       loopback, link-local, ULA, unspecified, multicast, or
       otherwise reserved triggers immediate rejection. This covers
       at minimum ``127.0.0.0/8``, ``10.0.0.0/8``,
       ``172.16.0.0/12``, ``192.168.0.0/16``, ``169.254.0.0/16``
       (cloud metadata), ``::1``, ``fc00::/7``, ``fe80::/10``.

    The same check is called for every redirect hop in
    :func:`_load_spec_bytes` so a benign-looking initial host cannot
    30x-redirect the fetcher into an internal address.

    Args:
        url: The full URL to validate (scheme + host + path).

    Raises:
        InvalidSpecError: Scheme is not ``https``, the hostname is
            absent or unresolvable, or any resolved address is
            non-public. The message is intentionally terse and
            path-free so the response is not a network-topology
            oracle for the caller.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidSpecError(f"spec URI must use the https scheme; got {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise InvalidSpecError("spec URI must include a hostname")
    try:
        addr_infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InvalidSpecError("spec URI hostname could not be resolved") from exc
    if not addr_infos:
        raise InvalidSpecError("spec URI hostname resolved to no addresses")
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        raw_ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise InvalidSpecError("spec URI resolved to an unrecognised address format") from exc
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise InvalidSpecError(
                "spec URI resolves to a non-public address; remote fetch refused"
            )


def _load_spec_bytes(spec_path_or_uri: str, content: str | None = None) -> bytes:
    """Resolve a spec source to raw bytes: uploaded content, or an https fetch.

    When *content* is provided, it is the inline spec text the CLI
    uploaded for a ``docs:`` / ``file://`` source (so no local path or
    non-https scheme reaches the backend). It is used verbatim -- capped
    at :data:`_MAX_SPEC_BYTES`, no fetch, no scheme guard -- and
    *spec_path_or_uri* serves only as the audit label.

    Without *content*, *spec_path_or_uri* must be an ``https://`` URL: a
    bare ``docs:`` shorthand (normally expanded CLI-side) is rejected
    with :exc:`UnsupportedSpecError`, and the fetch + every other scheme
    check is delegated to :func:`_fetch_spec_bytes`.

    Raises:
        InvalidSpecError: Non-https scheme, destination guard fires, or
            the uploaded content / fetched body exceeds the size cap.
        UnsupportedSpecError: Bare ``docs:`` shorthand reached the
            backend unexpanded.
        UpstreamNotSpecError: 2xx fetch with a non-spec ``Content-Type``.
        httpx.HTTPError: Network-level fetch failure.
    """
    if content is not None:
        # The CLI uploads the resolved bytes for ``docs:`` / ``file://``
        # sources so no local path or non-https scheme reaches the
        # backend; use them verbatim (still size-capped) and skip the
        # fetch + scheme guard.
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SPEC_BYTES:
            raise InvalidSpecError(
                f"spec content exceeds the {_MAX_SPEC_BYTES // (1024 * 1024)} MiB size limit"
            )
        return raw
    if urlparse(spec_path_or_uri).scheme == "docs":
        # The ``docs:<connector-id>/<file>`` shorthand is a CLI-side
        # convenience the CLI expands to a real URI against
        # ``$CLAUDE_RDC_DOCS`` before the request reaches the backend,
        # which has no docs root of its own. A bare ``docs:`` URI that
        # survives to here was never expandable; reject it as an
        # unsupported scheme that names the remedy rather than letting
        # it fall through to the https guard and surface as a generic
        # scheme error.
        raise UnsupportedSpecError(
            f"the 'docs:' spec-source scheme is not resolvable by the "
            f"backplane (got {spec_path_or_uri!r}); it is a CLI-side "
            f"shorthand the CLI expands against $CLAUDE_RDC_DOCS before "
            f"the request reaches the backend. Set $CLAUDE_RDC_DOCS so "
            f"the CLI resolves it, or pass an 'https://' spec URI.",
        )
    return _fetch_spec_bytes(spec_path_or_uri)


def _fetch_spec_bytes(spec_path_or_uri: str) -> bytes:
    """Fetch + size-cap the spec body from an ``https://`` URL.

    Only ``https://`` is accepted: the destination is validated via
    :func:`_assert_fetchable_remote_url` before any socket opens, and
    again after every redirect hop, so a 30x chain cannot escape the
    public-IP constraint by routing through a benign-looking host.

    After a 2xx response the ``Content-Type`` is inspected against the
    spec allow-list; a non-spec media type raises
    :exc:`UpstreamNotSpecError`. The body is streamed and capped at
    :data:`_MAX_SPEC_BYTES` (20 MiB) so a redirect to a large internal
    endpoint cannot exhaust pod memory.

    Raises:
        InvalidSpecError: Scheme is not ``https``, destination guard
            fires (private/loopback/link-local/reserved IP), or the
            response body exceeds the size cap.
        UpstreamNotSpecError: 2xx response with a non-spec ``Content-Type``.
        httpx.HTTPStatusError: Non-2xx HTTP response.
        httpx.HTTPError: Network-level fetch failure.
    """
    _assert_fetchable_remote_url(spec_path_or_uri)

    current_url = spec_path_or_uri
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            # Stream rather than buffer: ``client.get`` reads the whole
            # body into memory before the size cap below can fire, which
            # defeats the memory-exhaustion guard. ``stream`` lets the cap
            # abort the read mid-flight; the ``with`` block closes the
            # socket on the redirect ``continue``, the over-cap ``raise``,
            # and the success ``return`` alike.
            with client.stream("GET", current_url) as response:
                if response.has_redirect_location:
                    # Resolve relative / protocol-relative Location headers
                    # against the current URL so a valid 30x to a relative
                    # path isn't rejected by the https-only guard.
                    next_url = urljoin(current_url, str(response.headers["location"]))
                    # Re-validate the (now absolute) redirect target before
                    # following it so a 30x to a private IP is rejected here,
                    # before a socket opens to it.
                    _assert_fetchable_remote_url(next_url)
                    current_url = next_url
                    continue
                response.raise_for_status()
                _reject_non_spec_content_type(
                    upstream_url=spec_path_or_uri,
                    content_type=response.headers.get("content-type"),
                )
                # Cap the body size as it streams in, so a redirect to a
                # large internal endpoint can't exhaust pod memory.
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > _MAX_SPEC_BYTES:
                        raise InvalidSpecError(
                            f"spec response exceeds the {_MAX_SPEC_BYTES // (1024 * 1024)} MiB"
                            " size limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        raise InvalidSpecError(f"spec URI followed more than {_MAX_REDIRECTS} redirects")


def _reject_non_spec_content_type(
    *,
    upstream_url: str,
    content_type: str | None,
) -> None:
    """Raise :exc:`UpstreamNotSpecError` when ``content_type`` is not spec-shaped.

    The check is intentionally a prefix match against
    :data:`_ACCEPTED_SPEC_CONTENT_TYPES` -- servers tack
    ``; charset=utf-8`` (or stricter media-type parameters) onto the
    base type, and the parameters are irrelevant to "is this YAML/JSON".

    A missing header (``content_type is None``) is treated as non-spec
    -- every legitimate spec host (raw.githubusercontent.com, vendor
    appliances) sets the header, and the alternative is silently
    accepting the HTML developer-portal pages that motivated this
    check.
    """
    if content_type is None:
        raise UpstreamNotSpecError(upstream_url=upstream_url, content_type=None)
    # Lowercase before prefix check so ``Content-Type: TEXT/HTML`` is
    # caught the same as ``text/html``; HTTP media types are
    # case-insensitive per RFC 9110 §8.3.1.
    normalized = content_type.lower().split(";", 1)[0].strip()
    if not any(normalized == accepted for accepted in _ACCEPTED_SPEC_CONTENT_TYPES):
        raise UpstreamNotSpecError(upstream_url=upstream_url, content_type=content_type)


def _decode_spec(content: bytes) -> dict[str, Any]:
    """Decode raw spec bytes into a Python dict.

    Picks YAML or JSON by sniffing the first non-whitespace byte
    (:func:`detect_spec_format`). YAML parsing uses the C loader
    when available; JSON parsing uses stdlib.

    Raises:
        InvalidSpecError: Root document isn't a mapping.
        yaml.YAMLError: Malformed YAML.
        json.JSONDecodeError: Malformed JSON.
    """
    fmt = detect_spec_format(content)
    parsed: Any
    if fmt == "json":
        parsed = json.loads(content)
    else:
        parsed = yaml.load(io.BytesIO(content), Loader=_YamlLoader)
    if not isinstance(parsed, dict):
        raise InvalidSpecError(
            f"OpenAPI document must parse to a mapping, got {type(parsed).__name__}"
        )
    return parsed


def _validate_openapi_version(spec: dict[str, Any]) -> None:
    """Confirm the spec carries a supported ``openapi`` version string.

    OpenAPI 3.0.x and 3.1.x are supported. Swagger 2.0 specs declare
    ``swagger: "2.0"`` (no ``openapi`` key) and are rejected with an
    actionable :exc:`UnsupportedSpecError` that names the conversion
    path (a 2.0-only surface such as Harbor 2.x's ``swagger.yaml`` is
    onboarded by converting it to OpenAPI 3.x and re-ingesting the
    output). Newer specs with future major versions raise the same
    error type without the conversion remedy.
    """
    if "swagger" in spec:
        version = spec.get("swagger", "<missing>")
        raise UnsupportedSpecError(
            f"Swagger 2.0 specs are not ingestible directly (document declares "
            f"swagger={version!r}); {_SWAGGER_2_CONVERSION_REMEDIATION}"
        )
    raw_version = spec.get("openapi")
    if not isinstance(raw_version, str):
        raise InvalidSpecError("OpenAPI document must declare a string 'openapi' version")
    if not _SUPPORTED_OPENAPI_RE.match(raw_version):
        raise UnsupportedSpecError(
            f"OpenAPI version {raw_version!r} is not supported (expected 3.0.x or 3.1.x)"
        )


def _iter_operations(
    *,
    paths: dict[str, Any],
    component_schemas: dict[str, Any],
    component_parameters: dict[str, Any],
    component_responses: dict[str, Any],
    component_request_bodies: dict[str, Any],
    spec_source: str | None,
) -> Iterable[EndpointDescriptorProto]:
    """Yield one :class:`EndpointDescriptorProto` per (method, path)."""
    for path_template, path_item in paths.items():
        if not isinstance(path_item, dict):
            # A non-dict ``paths.<path>`` value is malformed per the
            # OpenAPI spec. Skip rather than abort the whole ingest;
            # T4's review queue will surface partial-spec issues.
            continue
        path_level_params = path_item.get("parameters") or []
        if not isinstance(path_level_params, list):
            raise InvalidSchemaError(
                f"paths.{path_template}.parameters must be a list, "
                f"got {type(path_level_params).__name__}"
            )
        for verb, operation in path_item.items():
            if verb not in _VERBS:
                continue
            if not isinstance(operation, dict):
                continue
            yield _build_proto(
                method=verb.upper(),
                path=path_template,
                operation=operation,
                path_level_params=path_level_params,
                component_schemas=component_schemas,
                component_parameters=component_parameters,
                component_responses=component_responses,
                component_request_bodies=component_request_bodies,
                spec_source=spec_source,
            )


def _build_proto(
    *,
    method: str,
    path: str,
    operation: dict[str, Any],
    path_level_params: list[Any],
    component_schemas: dict[str, Any],
    component_parameters: dict[str, Any],
    component_responses: dict[str, Any],
    component_request_bodies: dict[str, Any],
    spec_source: str | None,
) -> EndpointDescriptorProto:
    """Assemble a single :class:`EndpointDescriptorProto`."""
    op_params = operation.get("parameters") or []
    if not isinstance(op_params, list):
        raise InvalidSchemaError(
            f"paths.{path}.{method.lower()}.parameters must be a list, "
            f"got {type(op_params).__name__}"
        )

    parameter_schema = _build_parameter_schema(
        path=path,
        method=method,
        path_level_params=path_level_params,
        op_level_params=op_params,
        request_body=operation.get("requestBody"),
        component_schemas=component_schemas,
        component_parameters=component_parameters,
        component_request_bodies=component_request_bodies,
    )
    response_schema = _extract_response_schema(
        responses=operation.get("responses") or {},
        component_schemas=component_schemas,
        component_responses=component_responses,
    )

    raw_tags = operation.get("tags")
    if raw_tags is None:
        tags: list[str] = []
    elif isinstance(raw_tags, list):
        tags = [t for t in raw_tags if isinstance(t, str)]
    else:
        # ``tags: "admin"`` would otherwise be iterated as characters
        # by the list comprehension. Fail fast so the spec-author /
        # operator sees the mistake at ingest time rather than after
        # the rows are persisted.
        raise InvalidSchemaError(
            f"paths.{path}.{method.lower()}.tags must be a list, got {type(raw_tags).__name__}"
        )
    if spec_source is not None:
        tags.append(spec_source)

    return EndpointDescriptorProto(
        op_id=f"{method}:{path}",
        method=method,
        path=path,
        summary=_optional_string(operation.get("summary")),
        description=_optional_string(operation.get("description")),
        tags=tags,
        parameter_schema=parameter_schema,
        response_schema=response_schema,
        safety_level=_safety_level_for(method),
        requires_approval=False,
    )


def _optional_string(value: Any) -> str | None:
    """Coerce a possibly-empty spec field to ``str | None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value or None


def _safety_level_for(method: str) -> SafetyLevel:
    """Heuristic safety classification by HTTP verb."""
    if method in _DANGEROUS_VERBS:
        return "dangerous"
    if method in _CAUTION_VERBS:
        return "caution"
    return "safe"


def _build_parameter_schema(
    *,
    path: str,
    method: str,
    path_level_params: list[Any],
    op_level_params: list[Any],
    request_body: Any,
    component_schemas: dict[str, Any],
    component_parameters: dict[str, Any],
    component_request_bodies: dict[str, Any],
) -> dict[str, object]:
    """Flatten path + operation parameters + request body into one JSON Schema object.

    Path-level parameters apply to every operation under the path
    (OpenAPI 3.x rule); operation-level parameters override them when
    both ``name`` and ``in`` match. Each surviving parameter becomes
    a top-level property on the returned object with the
    ``x-meho-param-loc`` extension carrying its OpenAPI ``in`` value.

    A path parameter whose declared ``name`` carries a leading RFC6570
    expression operator (``{"name": "+path", "in": "path"}`` for a
    ``/events/{+path}`` template) is keyed on the *bare* name (``path``) so
    the property matches the name the renderer resolves the value by after
    stripping the operator from the template (the operator set is shared via
    :data:`~meho_backplane.operations._rfc6570.RFC6570_PATH_OPERATORS`).
    ``descriptor.path`` keeps the operator verbatim -- the renderer needs it
    to select reserved expansion. A spec declaring both ``path`` and
    ``+path`` as path parameters collides on the bare key and raises
    :class:`InvalidSchemaError` rather than silently dropping one.

    Parameters may be inlined (``{"name": ..., "in": ..., "schema": ...}``)
    or referenced via ``{"$ref": "#/components/parameters/<name>"}`` —
    the second form is what ``vi-json.yaml`` uses on every operation
    (the shared ``moId`` path parameter). Refs are resolved against
    ``component_parameters`` here, before the resolved-param ``schema``
    field is itself ref-resolved against ``component_schemas`` (the
    parameter object's ``schema`` field can independently carry its
    own ``$ref`` into ``#/components/schemas/*``).

    The request body (when present) is inlined as a single ``body``
    property whose schema is the resolved ``application/json`` (or
    fallback) schema. Operators rarely need a body-param name; the
    dispatcher uses ``x-meho-param-loc == "body"`` to recover the
    payload regardless of property name. Operations with no params
    at all get the empty-but-valid ``{"type": "object", "properties":
    {}}``.
    """
    properties: dict[str, object] = {}
    required: list[str] = []

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_param in [*path_level_params, *op_level_params]:
        resolved = _resolve_shallow_ref(raw_param, component_schemas, component_parameters)
        if not isinstance(resolved, dict):
            raise InvalidSchemaError(
                f"paths.{path}.{method.lower()}: parameter must be a mapping, "
                f"got {type(resolved).__name__}"
            )
        name = resolved.get("name")
        location = resolved.get("in")
        if not isinstance(name, str) or not isinstance(location, str):
            # OpenAPI demands both fields. Skip malformed entries
            # quietly — T4's review surfaces them at operator-review
            # time; aborting the whole ingest on one bad path would
            # block a 950-path spec on a single mistake.
            continue
        merged[(name, location)] = resolved

    _populate_param_properties(
        merged,
        properties=properties,
        required=required,
        component_schemas=component_schemas,
        path=path,
        method=method,
    )

    body_property = _build_body_property(request_body, component_schemas, component_request_bodies)
    if body_property is not None:
        body_schema = dict(body_property["schema"])
        body_schema["x-meho-param-loc"] = "body"
        properties["body"] = body_schema
        if body_property["required"]:
            required.append("body")

    schema: dict[str, object] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _populate_param_properties(
    merged: dict[tuple[str, str], dict[str, Any]],
    *,
    properties: dict[str, object],
    required: list[str],
    component_schemas: dict[str, Any],
    path: str,
    method: str,
) -> None:
    """Add one JSON-Schema property per merged parameter, keyed on its name.

    Mutates *properties* and *required* in place (the body property is added by
    the caller afterwards). Each parameter's ``x-meho-param-loc`` carries its
    OpenAPI ``in``; a ``path`` parameter is implicitly required per OpenAPI 3.x.

    A path parameter whose declared ``name`` carries a leading RFC6570
    expression operator (e.g. VCF Operations-for-Logs declares
    ``{"name": "+path"}`` for the template ``/events/{+path}``) is keyed on the
    *bare* name (``path``): that is the name the renderer
    (:func:`~meho_backplane.operations._branches._substitute_path`) resolves
    the value by after stripping the operator from the template, and so the
    name the caller supplies. Keeping the operator in the property key would
    force the caller to pass ``+path`` to satisfy validation, which the renderer
    would then fail to resolve -- the #2003/#2066 dead-op. ``descriptor.path``
    keeps the operator verbatim so the renderer still selects reserved
    expansion.

    Two **path** parameters that collapse onto the same bare key (a spec
    declaring both ``path`` and ``+path``) raise :class:`InvalidSchemaError`
    rather than silently dropping one -- one of the resulting template vars
    would be unsatisfiable. A same-name collision *across different* ``in``
    locations (the legal OpenAPI ``id``-in-path + ``id``-in-query) is **not** a
    fault and never raises: the params flatten to one property (the model
    carries a single ``x-meho-param-loc`` per name), and the **path** location
    wins regardless of declaration order so the path template var stays
    satisfiable -- a query/header twin can't shadow it into an undispatchable
    op. The guard is scoped to path-vs-path so it never rejects this legal
    shape.
    """
    path_bare_names: set[str] = set()
    for (name, location), param in merged.items():
        prop_schema = _build_param_property(param, component_schemas)
        prop_schema["x-meho-param-loc"] = location
        prop_name = name
        if location == "path":
            _operator, prop_name = split_path_operator(name)
            if prop_name in path_bare_names:
                # Another path param already claimed this bare key once the
                # RFC6570 operator was stripped (``path`` + ``+path``, in either
                # order). Failing loudly beats silently dropping a path var the
                # renderer still needs. Scoped to path-vs-path: a collision with
                # a non-path param of the same name is the legal cross-location
                # shape handled below.
                raise InvalidSchemaError(
                    f"paths.{path}.{method.lower()}: path parameter {name!r} "
                    f"normalises to property key {prop_name!r} (RFC6570 operator "
                    "stripped), which another path parameter already declares; a "
                    "spec must not declare both the bare and an operator-prefixed "
                    "form of one path variable."
                )
            path_bare_names.add(prop_name)
        elif prop_name in path_bare_names:
            # A non-path param shares its name with a path param already
            # placed. Don't let it overwrite the path property -- the path var
            # must stay substitutable or the op is undispatchable. Keep the
            # path binding; drop this twin (it can't have its own property key
            # in the flattened model anyway). Order-independent: the symmetric
            # "path declared after the twin" case is handled by the path branch
            # overwriting the twin below.
            continue
        properties[prop_name] = prop_schema
        is_required = param.get("required") is True or location == "path"
        if is_required and prop_name not in required:
            required.append(prop_name)


def _build_param_property(
    param: dict[str, Any],
    component_schemas: dict[str, Any],
) -> dict[str, object]:
    """Extract the JSON Schema fragment for one parameter.

    OpenAPI lets a parameter declare its type either via a ``schema``
    sub-object (the common case) or — for header / cookie /
    form-style — via the legacy inline-type form. The schema form
    wins; the legacy form falls back to ``{"type": <type>}`` synthesis.
    Description / example metadata is hoisted into the property
    schema so the dispatcher's error messages stay informative.

    OpenAPI 3.1 (aligned with JSON Schema 2020-12) lets ``schema`` be
    a bare boolean: ``true`` accepts every value, ``false`` rejects
    every value. Both are normalised to their dict equivalents via
    :func:`_normalize_boolean_schema` so the rest of the parser can
    treat the property as a regular dict.
    """
    schema = param.get("schema")
    out: dict[str, object]
    if isinstance(schema, bool):
        normalised = _normalize_boolean_schema(schema)
        # _normalize_boolean_schema always returns a dict for bool input.
        assert normalised is not None
        out = dict(normalised)
    elif isinstance(schema, dict):
        resolved = _resolve_shallow_ref(schema, component_schemas)
        resolved_normalised = _normalize_boolean_schema(resolved)
        # ``None`` here means the resolved value isn't a dict OR a bool —
        # treat as untyped (matches anything). Real specs don't trip this.
        out = {} if resolved_normalised is None else dict(resolved_normalised)
    elif "type" in param:
        out = {"type": param["type"]}
    else:
        # Untyped param — accept any value. JSON Schema 2020-12 says
        # an empty object schema matches any value, which is what we
        # want here.
        out = {}
    if "description" in param and isinstance(param["description"], str):
        out.setdefault("description", param["description"])
    return out


def _build_body_property(
    request_body: Any,
    component_schemas: dict[str, Any],
    component_request_bodies: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the ``{"schema": ..., "required": bool}`` body slot, or ``None``.

    The ``request_body`` argument may be an inline Request Body Object
    or a ``{"$ref": "#/components/requestBodies/<name>"}`` pointer
    (OpenAPI 3.0 §4.7.10 / 3.1 §4.8.13). The latter is uncommon in the
    v0.x catalogue today but is a first-class component bucket per the
    spec, so the resolver opts into the bucket via
    ``component_request_bodies``.
    """
    if not isinstance(request_body, dict):
        return None
    resolved = _resolve_shallow_ref(
        request_body,
        component_schemas,
        component_request_bodies=component_request_bodies,
    )
    if not isinstance(resolved, dict):
        return None
    content = resolved.get("content")
    if not isinstance(content, dict):
        return None
    media_type_schema = _select_media_type_schema(content, component_schemas)
    if media_type_schema is None:
        return None
    return {
        "schema": media_type_schema,
        # Strict identity check — OpenAPI's requestBody.required is a
        # boolean per spec, and accepting truthy strings ("yes") or
        # numbers would mis-mark mistyped specs as required-body when
        # the author meant something else. Anything not literally
        # ``True`` is treated as not-required.
        "required": resolved.get("required") is True,
    }


def _collect_2xx_response_codes(responses: dict[str, Any]) -> list[str]:
    """Return response keys to scan in preference order.

    Most-specific 2xx codes first, then OpenAPI 3.1's wildcard ``2XX``,
    then any other key starting with ``"2"`` that wasn't already
    picked.
    """
    candidates = [c for c in ("200", "201", "202", "203", "204") if c in responses]
    if "2XX" in responses:
        candidates.append("2XX")
    candidates.extend(
        key
        for key in responses
        if isinstance(key, str) and key.startswith("2") and key not in candidates
    )
    return candidates


def _extract_response_schema(
    *,
    responses: dict[str, Any],
    component_schemas: dict[str, Any],
    component_responses: dict[str, Any],
) -> dict[str, object] | None:
    """Pick the success response's schema, preferring ``200`` over ``201`` over wildcard.

    Each ``responses.<code>`` entry may be an inline Response Object
    or a ``{"$ref": "#/components/responses/<name>"}`` pointer
    (OpenAPI 3.0 §4.7.7 / 3.1 §4.8.16). The GitHub REST API spec uses
    response refs for every shared envelope (``accepted``,
    ``not_found``, ``validation_failed`` etc), so the resolver opts
    into the bucket via ``component_responses``.
    """
    if not isinstance(responses, dict):
        return None
    for code in _collect_2xx_response_codes(responses):
        response = _resolve_shallow_ref(
            responses[code],
            component_schemas,
            component_responses=component_responses,
        )
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        schema = _select_media_type_schema(content, component_schemas)
        if schema is not None:
            return schema
    return None
