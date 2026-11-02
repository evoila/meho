#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Build a connector overlay image: the public MEHO image plus connector
# packages (wheels). See docs/codebase/connector-packages.md.
#
#   build.sh --base ghcr.io/evoila/meho@sha256:<digest> \
#            --connectors DIR [--deps DIR] --tag IMAGE:TAG \
#            [--platform LIST] [--push] [--sign | --sign-key KEY] [--reviews DIR]
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
#   --sign-key    sign the pushed image with this key (needs --push). KEY is a
#                 key file or another key reference that cosign accepts. cosign
#                 reads the password of a key file from COSIGN_PASSWORD. The
#                 signature goes to the registry, not to a transparency log.
#   --reviews     folder with the review records (*.json). Needed to sign:
#                 each connector wheel needs a record by a person for its
#                 exact name, version and wheel digest. Checked before the
#                 build starts.
#
# No package index is used: every wheel must be in the two folders.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="" connectors="" deps="" tag="" platform="" reviews="" sign_key="" push=0 sign=0

die() { echo "build.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base) base="${2:-}"; shift 2 ;;
    --connectors) connectors="${2:-}"; shift 2 ;;
    --deps) deps="${2:-}"; shift 2 ;;
    --tag) tag="${2:-}"; shift 2 ;;
    --platform) platform="${2:-}"; shift 2 ;;
    --reviews) reviews="${2:-}"; shift 2 ;;
    --push) push=1; shift ;;
    --sign) sign=1; shift ;;
    --sign-key)
      case "${2:-}" in ""|-*) die "--sign-key needs a key reference" ;; esac
      sign=1; sign_key="$2"; shift 2 ;;
    -h|--help) sed -n '5,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$base" ] || die "--base is required"
[ -n "$connectors" ] || die "--connectors is required"
[ -n "$tag" ] || die "--tag is required"
[[ "$base" =~ @sha256:[0-9a-f]{64}$ ]] || die "--base must be pinned by digest (name@sha256:<64 hex>), got: $base"
[ -d "$connectors" ] || die "--connectors is not a folder: $connectors"
[ -z "$deps" ] || [ -d "$deps" ] || die "--deps is not a folder: $deps"
sign_option="--sign"
[ -z "$sign_key" ] || sign_option="--sign-key"
[ "$sign" -eq 0 ] || [ "$push" -eq 1 ] || die "$sign_option needs --push: only a pushed image can be signed"
[ "$sign" -eq 0 ] || [ -n "$reviews" ] || die "$sign_option needs --reviews: a person must review each connector package before it is signed"
[ -z "$reviews" ] || [ -d "$reviews" ] || die "--reviews is not a folder: $reviews"
# Stop before the push when a key file is missing. Other key references have a colon.
[ -z "$sign_key" ] || [[ "$sign_key" == *:* ]] || [ -f "$sign_key" ] || die "--sign-key: no key file at $sign_key"

ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
mkdir -p "$ctx/connectors" "$ctx/deps"
cp "$here/Dockerfile" "$here/overlay_check.py" "$ctx/"
shopt -s nullglob
wheels=("$connectors"/*.whl)
[ "${#wheels[@]}" -gt 0 ] || die "no connector wheel (*.whl) in $connectors"
cp "${wheels[@]}" "$ctx/connectors/"
if [ -n "$reviews" ]; then
  # Check the copies that go into the image, so the reviewed wheels are the built ones.
  python3 -I -S "$here/overlay_check.py" check-reviews --connectors "$ctx/connectors" --reviews "$reviews" \
    || die "signing stopped: the review check failed (see the message above)"
fi
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
    if [ -n "$sign_key" ]; then
      # cosign 3 stops on --tlog-upload=false unless --use-signing-config=false is also set.
      cosign sign --yes --key "$sign_key" --tlog-upload=false --use-signing-config=false "${repo}@${digest}"
    else
      cosign sign --yes "${repo}@${digest}"
    fi
    echo "signed: ${repo}@${digest}"
  fi
fi
