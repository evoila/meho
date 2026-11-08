#!/usr/bin/env bash
# Put one pinned spaCy language model into the Python environment of a CI
# job, without a download when possible. Run it in the folder of the uv
# project.
#
# Usage: install_spacy_model.sh <name> <version> <sha256>
#
# Order of choices:
#   1. The model is already installed at this version: use it.
#   2. The model file is in the tool folder (the job restores that folder
#      with actions/cache, keyed by the version): install it.
#   3. Download it with fetch_with_retry.sh, check the sha256, keep it in the
#      tool folder so the cache can save it, and install it.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <name> <version> <sha256>" >&2
  exit 2
fi

name="$1"
version="$2"
sha="$3"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
wheel="${name}-${version}-py3-none-any.whl"
tool_dir="${HOME}/.cache/meho-ci-tools/${name}-${version}"
url="https://github.com/explosion/spacy-models/releases/download/${name}-${version}/${wheel}"

installed="$(uv run python -c \
  'import importlib.metadata as m, sys; print(m.version(sys.argv[1]))' \
  "${name}" 2>/dev/null || true)"
if [[ "${installed}" == "${version}" ]]; then
  echo "${name} ${version} is already installed."
  exit 0
fi

if [[ -f "${tool_dir}/${wheel}" ]]; then
  echo "${name} ${version} comes from the cache."
else
  echo "${name} ${version} is not installed or in the cache. Downloading it."
  mkdir -p "${tool_dir}"
  "${here}/fetch_with_retry.sh" "${url}" "${tool_dir}/${wheel}" "${sha}"
fi

uv pip install --no-deps "${tool_dir}/${wheel}"
