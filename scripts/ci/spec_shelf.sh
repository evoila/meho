#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# The vendor API files that CI needs from the private spec shelf.
#
# Each file <list-dir>/<name>.txt names the shelf files of one connector or
# product version, one per line, relative to the shelf's docs/ folder (for
# example "argocd-3.3/argocd-swagger.json"). Blank lines and text after "#"
# are left out. A new connector adds its own file; nothing else changes.
#
#   spec_shelf.sh paths  <list-dir>
#       Print the sparse-checkout paths: "docs/<first folder>" of each
#       listed file, sorted, each once.
#   spec_shelf.sh verify <list-dir> <shelf-root>
#       Check that each listed file is under <shelf-root>/docs/. Print one
#       line per file. Fail when any file is missing.
#
# A bad list (no list file, no entry, an absolute path, "..", or a file
# without a folder) fails both commands.

set -euo pipefail

die() {
  echo "::error title=Spec-shelf list error::$1" >&2
  exit 1
}

entries() {
  local dir="$1" list line found=0
  shopt -s nullglob
  local lists=("${dir}"/*.txt)
  shopt -u nullglob
  [ "${#lists[@]}" -gt 0 ] || die "no *.txt list file in ${dir}"
  for list in "${lists[@]}"; do
    while IFS= read -r line || [ -n "${line}" ]; do
      line="${line%%#*}"
      line="${line//[[:space:]]/}"
      [ -n "${line}" ] || continue
      case "/${line}/" in
        //*|*/../*|*/./*) die "${list}: bad path '${line}' (must be relative to docs/, without '.' or '..')" ;;
      esac
      [[ "${line}" == */* ]] || die "${list}: '${line}' has no folder (write <folder>/<file>)"
      echo "${line}"
      found=1
    done < "${list}"
  done
  [ "${found}" -eq 1 ] || die "no file listed in ${dir}/*.txt"
}

sorted_entries() {
  local out
  # errexit does not reach into $(...), so pass a failure on by hand.
  out="$(entries "$1")" || return 1
  LC_ALL=C sort -u <<<"${out}"
}

cmd="${1:-}"
case "${cmd}" in
  paths)
    [ "$#" -eq 2 ] || die "usage: spec_shelf.sh paths <list-dir>"
    out="$(sorted_entries "$2")" || exit 1
    cut -d/ -f1 <<<"${out}" | LC_ALL=C sort -u | sed 's|^|docs/|'
    ;;
  verify)
    [ "$#" -eq 3 ] || die "usage: spec_shelf.sh verify <list-dir> <shelf-root>"
    out="$(sorted_entries "$2")" || exit 1
    missing=0
    count=0
    while IFS= read -r entry; do
      f="$3/docs/${entry}"
      if [ -f "${f}" ]; then
        echo "spec-shelf OK: ${f}"
        count=$((count + 1))
      else
        echo "::error title=Spec-shelf layout drift::${f} missing after the spec-shelf checkout — the real-spec lanes would silently skip. Fix the shelf layout or the list in $2/."
        missing=$((missing + 1))
      fi
    done <<<"${out}"
    [ "${missing}" -eq 0 ] || exit 1
    echo "spec-shelf OK: all ${count} listed files present."
    ;;
  *)
    die "usage: spec_shelf.sh paths <list-dir> | verify <list-dir> <shelf-root>"
    ;;
esac
