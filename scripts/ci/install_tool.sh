#!/usr/bin/env bash
# Put one pinned tool on the PATH of a CI job, without a download when
# possible.
#
# Usage: install_tool.sh <name> <version> <url> <sha256> [<archive-member>]
#
# Order of choices:
#   1. The tool is already on the runner at this version: use it.
#   2. The tool is in the tool folder (the job restores that folder with
#      actions/cache, keyed by the version): use it.
#   3. Download it with fetch_with_retry.sh, check the sha256, and keep it
#      in the tool folder so the cache can save it.
#
# <archive-member> is the path of the program inside a .tar.gz file. Leave
# it out when the URL is the program itself.
# VERSION_ARG is the argument that makes the tool print its version
# (default: --version).
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 <name> <version> <url> <sha256> [<archive-member>]" >&2
  exit 2
fi

name="$1"
version="$2"
url="$3"
sha="$4"
member="${5:-}"
version_arg="${VERSION_ARG:---version}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tool_dir="${HOME}/.cache/meho-ci-tools/${name}-${version}"
bin_dir="${HOME}/.local/bin"

add_to_path() {
  if [[ -n "${GITHUB_PATH:-}" ]]; then
    echo "$1" >> "${GITHUB_PATH}"
  fi
}

# The first complete version number that the tool prints, without a leading
# "v". The whole number is compared, so 2.12.20 is not taken for 2.12.2.
installed_version() {
  "${name}" "${version_arg}" 2>&1 \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[-+.0-9A-Za-z]*' \
    | head -n 1
}

if command -v "${name}" >/dev/null 2>&1 \
   && [[ "$(installed_version)" == "${version#v}" ]]; then
  echo "${name} ${version} is already on the runner."
  exit 0
fi

if [[ -x "${tool_dir}/${name}" ]]; then
  echo "${name} ${version} comes from the cache."
else
  echo "${name} ${version} is not on the runner or in the cache. Downloading it."
  work="$(mktemp -d)"
  trap 'rm -rf "${work}"' EXIT
  "${here}/fetch_with_retry.sh" "${url}" "${work}/download" "${sha}"
  mkdir -p "${tool_dir}"
  if [[ -n "${member}" ]]; then
    tar -xzf "${work}/download" -C "${work}" "${member}"
    install -m 0755 "${work}/${member}" "${tool_dir}/${name}"
  else
    install -m 0755 "${work}/download" "${tool_dir}/${name}"
  fi
fi

mkdir -p "${bin_dir}"
install -m 0755 "${tool_dir}/${name}" "${bin_dir}/${name}"
add_to_path "${bin_dir}"
"${bin_dir}/${name}" "${version_arg}"
