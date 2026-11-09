#!/usr/bin/env bash
# Put the pinned Tailwind CLI binaries in the build context of the backend
# image (backend/.tool-preseed/), so the image build makes no download.
#
# Usage: stage_tailwind.sh <arch>...    (arch: amd64, arm64)
#
# Order of choices for each arch:
#   1. The file is in the tool folder (the job restores that folder with
#      actions/cache, keyed by scripts/ci/tool-versions.env): use it.
#   2. Download it with fetch_with_retry.sh (more tries than for the other
#      tools), check the sha256, and keep it in the tool folder so the
#      cache can save it.
# A file in the cache with a wrong sha256 is thrown away and downloaded
# again. backend/Dockerfile checks the sha256 again when it uses the file.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <amd64|arm64>..." >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
# shellcheck source=/dev/null
source "${here}/tool-versions.env"

# The Dockerfile has the same pin as its defaults. Stop when they differ, so
# a bump in one place only is noticed here and not as a silent download.
dockerfile="${root}/backend/Dockerfile"
for pin in TAILWIND_VER TAILWIND_SHA_AMD64 TAILWIND_SHA_ARM64; do
  if ! grep -qxF "ARG ${pin}=${!pin}" "${dockerfile}"; then
    echo "::error::${pin} in scripts/ci/tool-versions.env differs from backend/Dockerfile. Change both." >&2
    exit 1
  fi
done

tool_dir="${HOME}/.cache/meho-ci-tools/tailwindcss-${TAILWIND_VER}"
out_dir="${root}/backend/.tool-preseed"
mkdir -p "${tool_dir}" "${out_dir}"

for arch in "$@"; do
  case "${arch}" in
    amd64) name=linux-x64; sha="${TAILWIND_SHA_AMD64}" ;;
    arm64) name=linux-arm64; sha="${TAILWIND_SHA_ARM64}" ;;
    *) echo "unknown arch: ${arch}" >&2; exit 2 ;;
  esac
  file="${tool_dir}/tailwindcss-${name}"
  if [[ -f "${file}" ]] && ! echo "${sha}  ${file}" | sha256sum -c - >/dev/null 2>&1; then
    echo "::warning::The cached ${file##*/} has a wrong checksum. Downloading it again."
    rm -f "${file}"
  fi
  if [[ -f "${file}" ]]; then
    echo "tailwindcss ${TAILWIND_VER} ${name} comes from the cache."
  else
    echo "tailwindcss ${TAILWIND_VER} ${name} is not in the cache. Downloading it."
    # 9 tries; the pauses add up to 6 minutes.
    FETCH_PAUSES="${TAILWIND_FETCH_PAUSES:-15 30 45 60 60 60 60 30}" "${here}/fetch_with_retry.sh" \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VER}/tailwindcss-${name}" \
      "${file}.part" "${sha}"
    mv "${file}.part" "${file}"
  fi
  install -m 0644 "${file}" "${out_dir}/tailwindcss-${name}"
done
