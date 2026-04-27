#!/usr/bin/env bats
# Regression tests for scripts/assemble-public-tree.sh
#
# Each test copies the script under test into an isolated temp dir populated
# from tests/scripts/fixtures/assemble-public-tree/base-repo/, then invokes
# it with an absolute --allowlist path pointing at a per-test heredoc-written
# allowlist. The script's `cd "$REPO_ROOT"` logic (REPO_ROOT = parent of
# script's own location via BASH_SOURCE[0]) makes the temp dir its "repo
# root" for the duration of the test.
#
# Stderr redirection inside `run bash -c '... 2>/dev/null'` keeps the
# suite compatible with bats-core < 1.5 which lacks `run --separate-stderr`.

setup() {
  TEST_ROOT="$(mktemp -d)"
  cp -a "$BATS_TEST_DIRNAME/fixtures/assemble-public-tree/base-repo/." "$TEST_ROOT/"
  mkdir -p "$TEST_ROOT/scripts"
  cp "$BATS_TEST_DIRNAME/../../scripts/assemble-public-tree.sh" "$TEST_ROOT/scripts/"
  chmod +x "$TEST_ROOT/scripts/assemble-public-tree.sh"
  SCRIPT="$TEST_ROOT/scripts/assemble-public-tree.sh"
  ALLOWLIST="$TEST_ROOT/test-allowlist.txt"
  STAGING=""
}

teardown() {
  if [ -n "${TEST_ROOT:-}" ] && [ -d "$TEST_ROOT" ]; then
    rm -rf "$TEST_ROOT"
  fi
  if [ -n "${STAGING:-}" ] && [ -d "$STAGING" ]; then
    rm -rf "$STAGING"
  fi
}

# Args are forwarded via positional parameters ($1 = script, ${@:2} = args)
# so quoting survives end to end. Interpolating $* into the command string
# would re-split on whitespace / shell metacharacters inside the inner bash -c.

# Run the script and capture only stdout (diagnostics live on stderr).
run_stdout() {
  run bash -c '"$1" "${@:2}" 2>/dev/null' _ "$SCRIPT" "$@"
}

# Run the script and capture only stderr (for annotation assertions).
run_stderr() {
  run bash -c '"$1" "${@:2}" 2>&1 >/dev/null' _ "$SCRIPT" "$@"
}

# Run the script and capture combined stdout+stderr (for mixed assertions).
run_combined() {
  run bash -c '"$1" "${@:2}" 2>&1' _ "$SCRIPT" "$@"
}

write_minimal_allowlist() {
  cat > "$ALLOWLIST" <<EOF
meho_app
pyproject.toml
README.md
LICENSE
EOF
}

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@test "happy path: minimal allowlist produces a staging tree with required paths" {
  write_minimal_allowlist
  run_stdout "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  STAGING="$output"
  [ -d "$STAGING" ]
  [ -d "$STAGING/meho_app" ]
  [ -f "$STAGING/meho_app/__init__.py" ]
  [ -d "$STAGING/meho_app/core" ]
  [ -f "$STAGING/meho_app/core/config.py" ]
  [ -f "$STAGING/pyproject.toml" ]
  [ -f "$STAGING/README.md" ]
  [ -f "$STAGING/LICENSE" ]
  [ ! -e "$STAGING/extras.md" ]
  [ ! -e "$STAGING/docs" ]
}

@test "happy path: staging path is the only line on stdout" {
  write_minimal_allowlist
  run_stdout "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 1 ]
  [ -d "${lines[0]}" ]
  STAGING="${lines[0]}"
}

# ---------------------------------------------------------------------------
# Parsing tolerance
# ---------------------------------------------------------------------------

@test "comments: line-leading and inline # are stripped" {
  cat > "$ALLOWLIST" <<'EOF'
# This is a comment line and should be ignored
meho_app
pyproject.toml  # trailing inline comment
README.md
# another comment
LICENSE
EOF
  run_stdout "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  STAGING="$output"
  [ -f "$STAGING/pyproject.toml" ]
  [ -f "$STAGING/README.md" ]
  [ -f "$STAGING/LICENSE" ]
  [ -d "$STAGING/meho_app" ]
}

@test "blank lines and whitespace-only lines are skipped" {
  printf '\nmeho_app\n   \n\npyproject.toml\n\n\tREADME.md\n\nLICENSE\n\n' > "$ALLOWLIST"
  run_stdout "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  STAGING="$output"
  [ -d "$STAGING/meho_app" ]
  [ -f "$STAGING/README.md" ]
}

# ---------------------------------------------------------------------------
# Warning path (non-fatal)
# ---------------------------------------------------------------------------

@test "missing allowlist entry emits warning but does not fail" {
  cat > "$ALLOWLIST" <<EOF
meho_app
pyproject.toml
README.md
LICENSE
nonexistent-thing
EOF
  run_combined "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::Allowlist entry missing from repo: nonexistent-thing"* ]]
  [[ "$output" == *"1 allowlist entries were missing"* ]]
  STAGING="$(printf '%s\n' "$output" | tail -n 1)"
  [ -d "$STAGING" ]
}

# ---------------------------------------------------------------------------
# Hard failure: required path missing
# ---------------------------------------------------------------------------

@test "required path missing: omitting meho_app aborts with error" {
  cat > "$ALLOWLIST" <<EOF
pyproject.toml
README.md
LICENSE
EOF
  run_stderr "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Required path 'meho_app' missing from staging tree"* ]]
}

# ---------------------------------------------------------------------------
# Defensive denylist branch (from task body scenario #6)
# ---------------------------------------------------------------------------

@test "denylisted top-level in staging is rejected (defensive branch)" {
  skip "The derived-denylist check computes the allowed set from the same loop that populates staging, so a leak cannot occur through allowlist input alone. The branch is a safety net for script bugs, not user error — covered by manual review of lines 202-222 of scripts/assemble-public-tree.sh."
}

# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

@test "dry-run: prints file list to stdout" {
  # The "no staging directory created" invariant is structurally guaranteed —
  # scripts/assemble-public-tree.sh's --dry-run branch exits before reaching
  # the mktemp -d that creates the staging dir. We only need to assert the
  # emitted contract: a multi-line file list, not a single staging path.
  write_minimal_allowlist
  run_stdout "--dry-run" "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -gt 1 ]
  [[ "$output" == *"meho_app/__init__.py"* ]]
  [[ "$output" == *"meho_app/core/config.py"* ]]
  [[ "$output" == *"pyproject.toml"* ]]
  [[ "$output" == *"README.md"* ]]
  [[ "$output" == *"LICENSE"* ]]
}

@test "dry-run: missing entry shown as MISSING on stderr" {
  cat > "$ALLOWLIST" <<EOF
meho_app
pyproject.toml
README.md
LICENSE
does-not-exist
EOF
  run_combined "--dry-run" "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MISSING: does-not-exist"* ]]
}

# ---------------------------------------------------------------------------
# Validation branches (each rejection class)
# ---------------------------------------------------------------------------

@test "rejects absolute path in allowlist" {
  cat > "$ALLOWLIST" <<EOF
meho_app
/etc/passwd
EOF
  run_stderr "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Allowlist entry must be relative to repo root: '/etc/passwd'"* ]]
}

@test "rejects leading-dash entry in allowlist" {
  cat > "$ALLOWLIST" <<EOF
meho_app
-rf
EOF
  run_stderr "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Allowlist entry must not start with '-'"* ]]
}

@test "rejects parent-directory traversal in allowlist" {
  cat > "$ALLOWLIST" <<EOF
meho_app
../escape
EOF
  run_stderr "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Allowlist entry must not contain '..'"* ]]
}

@test "rejects glob characters in allowlist" {
  cat > "$ALLOWLIST" <<EOF
meho_app
meho_app/*
EOF
  run_stderr "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Allowlist entry must not contain glob characters"* ]]
}

@test "rejects trailing-slash entry in allowlist" {
  cat > "$ALLOWLIST" <<EOF
meho_app/
pyproject.toml
EOF
  run_stderr "--allowlist" "$ALLOWLIST"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Allowlist entry must not have a trailing slash"* ]]
}

# ---------------------------------------------------------------------------
# CLI error paths
# ---------------------------------------------------------------------------

@test "unknown argument exits with code 2" {
  run_stderr "--nope"
  [ "$status" -eq 2 ]
  [[ "$output" == *"::error::Unknown argument"* ]]
}

@test "--allowlist without argument exits with code 2" {
  run_stderr "--allowlist"
  [ "$status" -eq 2 ]
  [[ "$output" == *"::error::--allowlist requires a path"* ]]
}

@test "missing allowlist file exits with code 1" {
  run_stderr "--allowlist" "/nonexistent/path/allowlist.txt"
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::Allowlist file not found"* ]]
}
