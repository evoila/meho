#!/usr/bin/env bash
# Pre-push hook: blocks direct pushes to the public repo remote.
# The mirror GitHub Action is the ONLY authorized path to the public repo.
#
# Installed by pre-commit via .pre-commit-config.yaml (stage: pre-push).
# pre-commit exposes the remote URL as $PRE_COMMIT_REMOTE_URL — git's native
# positional-arg interface is not used here.

PUBLIC_REMOTE_PATTERNS=(
  "github.com/evoila/meho"
  "github.com:evoila/meho"
)

# pre-commit always sets PRE_COMMIT_REMOTE_URL during pre-push. An empty value
# here means the script was invoked outside pre-commit's pre-push stage (e.g.
# copied directly into .git/hooks/pre-push). Skip with a clear warning so the
# no-op is explicit rather than a silent fail-open. Server-side branch
# protection on the public repo is the authoritative gate; this hook is the
# developer-side paper cut.
if [ -z "${PRE_COMMIT_REMOTE_URL:-}" ]; then
  echo "WARN: block-public-remote-push.sh invoked without PRE_COMMIT_REMOTE_URL." >&2
  echo "  This script is designed to run under pre-commit's pre-push stage." >&2
  echo "  Skipping public-remote check (no remote URL in context)." >&2
  exit 0
fi

remote_url="${PRE_COMMIT_REMOTE_URL}"

for pattern in "${PUBLIC_REMOTE_PATTERNS[@]}"; do
  if [[ "$remote_url" == *"$pattern"* ]]; then
    echo ""
    echo "BLOCKED: Direct push to public repo is not allowed."
    echo ""
    echo "The public repo (evoila/meho) is updated automatically by the"
    echo "mirror GitHub Action after CI passes on private main."
    echo ""
    echo "Push your changes to the private repo (origin) instead:"
    echo "  git push origin <branch>"
    echo ""
    echo "Emergency bypass (use with extreme caution):"
    echo "  git push --no-verify public main"
    echo ""
    exit 1
  fi
done

exit 0
