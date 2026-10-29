#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Build a connector overlay image: the public MEHO image plus connector
# packages (wheels). See docs/codebase/connector-packages.md.
#
#   build.sh --base ghcr.io/evoila/meho@sha256:<digest> \
#            --connectors DIR [--deps DIR] --tag IMAGE:TAG \
#            [--platform LIST] [--push] [--sign]
#
#   --base        the public image, pinned by its digest. A tag alone is refused.
#   --connectors  folder with the connector package wheels (*.whl).
#   --deps        folder with the extra wheels they need (optional).
#   --tag         the name of the new image.
#   --platform    passed to docker buildx (for example linux/amd64,linux/arm64).
#   --push        push the image. Without it, the image is loaded locally.
#   --sign        sign the pushed image with cosign (needs --push). evoila
#                 uses this for its own images (tier 2). A user's own image
#                 (tier 1) is built without it.
#
# No package index is used: every wheel must be in the two folders.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="" connectors="" deps="" tag="" platform="" push=0 sign=0

die() { echo "build.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base) base="${2:-}"; shift 2 ;;
    --connectors) connectors="${2:-}"; shift 2 ;;
    --deps) deps="${2:-}"; shift 2 ;;
    --tag) tag="${2:-}"; shift 2 ;;
    --platform) platform="${2:-}"; shift 2 ;;
    --push) push=1; shift ;;
    --sign) sign=1; shift ;;
    -h|--help) sed -n '5,22p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$base" ] || die "--base is required"
[ -n "$connectors" ] || die "--connectors is required"
[ -n "$tag" ] || die "--tag is required"
[[ "$base" =~ @sha256:[0-9a-f]{64}$ ]] || die "--base must be pinned by digest (name@sha256:<64 hex>), got: $base"
[ -d "$connectors" ] || die "--connectors is not a folder: $connectors"
[ -z "$deps" ] || [ -d "$deps" ] || die "--deps is not a folder: $deps"
[ "$sign" -eq 0 ] || [ "$push" -eq 1 ] || die "--sign needs --push: only a pushed image can be signed"

ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
mkdir -p "$ctx/connectors" "$ctx/deps"
cp "$here/Dockerfile" "$here/overlay_check.py" "$ctx/"
shopt -s nullglob
wheels=("$connectors"/*.whl)
[ "${#wheels[@]}" -gt 0 ] || die "no connector wheel (*.whl) in $connectors"
cp "${wheels[@]}" "$ctx/connectors/"
if [ -n "$deps" ]; then
  extra=("$deps"/*.whl)
  [ "${#extra[@]}" -eq 0 ] || cp "${extra[@]}" "$ctx/deps/"
fi

args=(--build-arg "BASE_IMAGE=$base" --tag "$tag" --metadata-file "$ctx/metadata.json")
[ -z "$platform" ] || args+=(--platform "$platform")
if [ "$push" -eq 1 ]; then args+=(--push); else args+=(--load); fi

docker buildx build "${args[@]}" "$ctx"

if [ "$push" -eq 1 ]; then
  digest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$ctx/metadata.json")"
  repo="${tag%:*}"
  case "${tag##*/}" in *:*) ;; *) repo="$tag" ;; esac
  echo "pushed: ${repo}@${digest}"
  if [ "$sign" -eq 1 ]; then
    cosign sign --yes "${repo}@${digest}"
    echo "signed: ${repo}@${digest}"
  fi
fi
