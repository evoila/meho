#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Reads the lists of required vendor API files on the spec shelf.
#
# The lists are in ci/spec-shelf/: one small .txt file per product
# version. Each line names one file, as a path from the root of the shelf
# repo (for example docs/<product>-<version>/<file>). Blank lines and
# lines that start with "#" are skipped. Adding the files of a new
# connector adds one new list file; ci.yml does not change.
#
# Usage:
#   spec_shelf.sh paths  <list-dir>
#       Print the shelf folders to check out (docs/<folder>), one per line.
#   spec_shelf.sh verify <list-dir> <shelf-root>
#       Check that each listed file exists under <shelf-root>. Print one
#       line per file. A missing file fails with the
#       "Spec-shelf layout drift" error.
#
# Any problem with the lists themselves (no list folder, no list, an
# empty list, a bad line) fails too, so a broken list never checks
# nothing and passes.

set -euo pipefail

die() {
  echo "::error title=Spec-shelf list error::$*" >&2
  exit 1
}

# Print every listed file once, sorted.
read_lists() {
  local dir="$1" list line count
  local -a lists
  [ -d "$dir" ] || die "list folder ${dir} not found"
  shopt -s nullglob
  lists=("$dir"/*.txt)
  shopt -u nullglob
  [ "${#lists[@]}" -gt 0 ] || die "no .txt list in ${dir}"
  for list in "${lists[@]}"; do
    count=0
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      line="${line#"${line%%[![:space:]]*}"}"
      line="${line%"${line##*[![:space:]]}"}"
      case "$line" in
        '' | '#'*) continue ;;
      esac
      case "$line" in
        *[[:space:]]* | *//* | */ | */./* | */../* | */. | */..)
          die "${list}: bad line '${line}'" ;;
        docs/*/*) ;;
        *) die "${list}: bad line '${line}' (must be docs/<folder>/<file>)" ;;
      esac
      echo "$line"
      count=$((count + 1))
    done <"$list"
    [ "$count" -gt 0 ] || die "${list} names no file"
  done | LC_ALL=C sort -u
}

cmd_paths() {
  local files
  files="$(read_lists "$1")"
  printf '%s\n' "$files" | cut -d/ -f1-2 | LC_ALL=C sort -u
}

cmd_verify() {
  local root="$2" files f missing=0
  files="$(read_lists "$1")"
  while IFS= read -r f; do
    if [ ! -f "${root}/${f}" ]; then
      echo "::error title=Spec-shelf layout drift::${root}/${f} missing after the spec-shelf checkout — the real-spec lanes would silently skip. Fix the shelf layout or the list in ci/spec-shelf/."
      missing=$((missing + 1))
    fi
  done <<<"$files"
  [ "$missing" -eq 0 ] || exit 1
  while IFS= read -r f; do
    echo "spec-shelf OK: ${root}/${f}"
  done <<<"$files"
}

case "${1:-}" in
  paths)
    [ "$#" -eq 2 ] || die "usage: spec_shelf.sh paths <list-dir>"
    cmd_paths "$2"
    ;;
  verify)
    [ "$#" -eq 3 ] || die "usage: spec_shelf.sh verify <list-dir> <shelf-root>"
    cmd_verify "$2" "$3"
    ;;
  *)
    die "usage: spec_shelf.sh paths <list-dir> | verify <list-dir> <shelf-root>"
    ;;
esac
