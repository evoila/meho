#!/usr/bin/env bash
# Apply the desired GitHub repo metadata (description + topics) for
# `evoila/meho` from the declarative source of truth at
# `.github/repo-config.yml`.
#
# Idempotent: re-applies the same description + topics every run.
# Requires: `gh` (authenticated as a maintainer with repo:admin or
# equivalent), and either `yq` (preferred) or Python 3 with PyYAML.
#
# Usage:
#   scripts/setup/repo-metadata.sh           # apply
#   scripts/setup/repo-metadata.sh --dry-run # print, do not mutate
#
# Verify after:
#   gh api repos/evoila/meho --jq '{description, topics}'
set -euo pipefail

REPO="${REPO:-evoila/meho}"
CONFIG_FILE="${CONFIG_FILE:-$(git rev-parse --show-toplevel)/.github/repo-config.yml}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f $CONFIG_FILE ]]; then
  echo "config file not found: $CONFIG_FILE" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required but not installed" >&2
  exit 1
fi

# Parse description + topics from YAML. Prefer yq; fall back to python.
if command -v yq >/dev/null 2>&1; then
  DESCRIPTION="$(yq -r '.repository.description' "$CONFIG_FILE")"
  # yq emits one topic per line on `.[] | .repository.topics[]`.
  mapfile -t TOPICS < <(yq -r '.repository.topics[]' "$CONFIG_FILE")
elif command -v python3 >/dev/null 2>&1; then
  read -r DESCRIPTION < <(python3 - "$CONFIG_FILE" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)
desc = cfg["repository"]["description"]
# Collapse YAML folded-scalar whitespace ("foo\n  bar" -> "foo bar").
print(" ".join(desc.split()))
PY
  )
  mapfile -t TOPICS < <(python3 - "$CONFIG_FILE" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)
for t in cfg["repository"]["topics"]:
    print(t)
PY
  )
else
  echo "neither yq nor python3+PyYAML available to parse YAML" >&2
  exit 1
fi

if [[ -z $DESCRIPTION ]] || [[ ${#TOPICS[@]} -eq 0 ]]; then
  echo "failed to parse description or topics from $CONFIG_FILE" >&2
  exit 1
fi

# Build JSON array of topics for the topics endpoint.
TOPICS_JSON="$(printf '%s\n' "${TOPICS[@]}" | jq -R . | jq -sc .)"

echo "repo:         $REPO"
echo "description:  $DESCRIPTION"
echo "topics:       ${TOPICS[*]}"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "(dry-run; no API calls)"
  exit 0
fi

echo "==> PATCH repos/$REPO (description)"
gh api --method PATCH "repos/$REPO" \
  -f description="$DESCRIPTION" \
  --jq '{description: .description}'

echo "==> PUT repos/$REPO/topics"
# The topics endpoint requires a JSON body with a "names" array; build
# it via `--input -` so the array is preserved (not flattened by `-f`).
jq -nc --argjson names "$TOPICS_JSON" '{names: $names}' |
  gh api --method PUT "repos/$REPO/topics" --input - \
    --jq '{topics: .names}'

echo "==> done. Verify:"
echo "    gh api repos/$REPO --jq '{description, topics}'"
