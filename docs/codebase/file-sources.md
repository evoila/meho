# Named file sources

## Overview

A **named file source** is a place a file may come from. A tenant admin
gives it a name and a base address: scheme, host, port and an optional
path prefix. An operation that moves a file takes an address, and MEHO
accepts it only when it lies under one of the named sources.

The check is one helper in the backplane:
`backend/src/meho_backplane/connectors/_shared/file_sources.py`. It is also
in the connector kit (`meho_connector_kit`), so a connector package uses
it and keeps no list of its own.

## Key types

- `FileSource`: `name`, `base` and `private_network_allowed` (false by
  default). `base` is a `FileSourceBase`: `scheme`, `host`, `port` and
  `path_prefix`.
- `file_sources_from_map(mapping)`: reads a map of name to base address.
- `file_sources_from_hosts(entries)`: reads a list of `host` or
  `host:port` entries, each as `https://host:port/`.
- `resolve_file_source(url, sources)`: returns the source the address lies
  under, or raises `FileSourceError`.
- `FileSourceError`: has a `rule` that says which rule failed. Its text
  never holds the address, because an address can hold a token.
- `file_source_resolver(source)`: the connect-time screen for a source.

## The rules

`resolve_file_source` refuses, before any connection:

- a scheme other than `http` or `https` (rule `scheme`);
- a user name or password (`userinfo`) or a fragment (`fragment`);
- an address whose scheme, host or port differ from every source
  (`no_source`), or whose path is not under the path of a matching source
  on a `/` boundary (`path_outside_source`);
- a path with a `.` or `..` part after full decoding (`path_dots`). A
  path is decoded until it stops changing, so `%252e` counts as a dot. A
  part may carry `;` parameters, so `..;x` counts as `..`;
- a path that cannot be decoded to the end (`path_undecodable`): a `%`
  without two hex digits, bytes that are not UTF-8, or more than eight
  rounds of encoding.

When more than one source fits, the one with the longest path prefix wins.

## The address guard

The address guard (see the target address guard page) still runs for
every source, at connect time, with pinned addresses.
`file_source_resolver(source)` returns a function for
`build_port_pinned_async_transport`. It calls `screen_dial_addresses` with
`allow_private` set from the source.

`allow_private` is a keyword argument of `screen_and_resolve` and
`screen_dial_addresses`. It is off by default, so every other caller
behaves as before. When it is on, private addresses (RFC 1918 and
unique-local IPv6) pass. Loopback, link-local and cloud metadata addresses
are always refused. The install-wide address allowlist does not change: a
private source is allowed per source, never for the whole install.

## What comes next

The connector packages that keep their own list today (`datastore_upload_sources`,
`file_hosts` and `vdt_sources`) move to this helper in a later change, after
they pin the new kit version. The kit version is 1.6.
