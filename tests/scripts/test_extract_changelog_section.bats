#!/usr/bin/env bats
# Regression tests for scripts/extract-changelog-section.sh
#
# Each test populates an isolated CHANGELOG fixture and invokes the script
# with an explicit fixture path so the tests do not depend on the repo's
# real CHANGELOG.md.

setup() {
  TEST_ROOT="$(mktemp -d)"
  SCRIPT="$BATS_TEST_DIRNAME/../../scripts/extract-changelog-section.sh"
  FIXTURE="$TEST_ROOT/CHANGELOG.md"
}

teardown() {
  if [ -n "${TEST_ROOT:-}" ] && [ -d "$TEST_ROOT" ]; then
    rm -rf "$TEST_ROOT"
  fi
}

# Standard KAC 1.1.0 fixture with a single version section above the
# bottom link-ref block. Mirrors the v0.1.0 launch state.
write_single_version_fixture() {
  cat > "$FIXTURE" <<'EOF'
# Changelog

## [Unreleased]

_No unreleased changes yet._

## [0.1.0] - 2026-05-02

Initial public release.

### Added

- feature one
- feature two

[Unreleased]: https://example/compare/v0.1.0...HEAD
[0.1.0]: https://example/releases/tag/v0.1.0
EOF
}

# Two version sections to exercise the next-h2 terminator.
write_two_versions_fixture() {
  cat > "$FIXTURE" <<'EOF'
# Changelog

## [Unreleased]

_None._

## [0.2.0] - 2026-06-01

Second release content.

## [0.1.0] - 2026-05-02

First release content.

[Unreleased]: https://example/compare/v0.2.0...HEAD
[0.2.0]: https://example/releases/tag/v0.2.0
[0.1.0]: https://example/releases/tag/v0.1.0
EOF
}

# Heading exists but no body (the "graduated [Unreleased] but forgot
# to move entries" mistake).
write_empty_section_fixture() {
  cat > "$FIXTURE" <<'EOF'
# Changelog

## [Unreleased]

work in progress

## [0.1.0] - 2026-05-02

[Unreleased]: https://example/compare/v0.1.0...HEAD
[0.1.0]: https://example/releases/tag/v0.1.0
EOF
}

# Versions where one is a prefix of another in numeric form (0.1.1 vs
# 0.1.10). The literal `]` in the target is what keeps them separate.
write_prefix_collision_fixture() {
  cat > "$FIXTURE" <<'EOF'
# Changelog

## [0.1.10] - 2026-02-01

ten content.

## [0.1.1] - 2026-01-01

one content.

[0.1.10]: https://example/releases/tag/v0.1.10
[0.1.1]: https://example/releases/tag/v0.1.1
EOF
}

@test "extracts single version section between [Unreleased] and link refs" {
  write_single_version_fixture
  run "$SCRIPT" 0.1.0 "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Initial public release."* ]]
  [[ "$output" == *"### Added"* ]]
  [[ "$output" == *"feature one"* ]]
  # Must not include the heading itself.
  [[ "$output" != *"## [0.1.0]"* ]]
  # Must not include the [Unreleased] body or the bottom link refs.
  [[ "$output" != *"No unreleased changes yet"* ]]
  [[ "$output" != *"https://example/releases/tag"* ]]
}

@test "terminates at next h2 in a multi-version file" {
  write_two_versions_fixture
  run "$SCRIPT" 0.2.0 "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Second release content."* ]]
  [[ "$output" != *"First release content."* ]]
  [[ "$output" != *"## [0.1.0]"* ]]
}

@test "extracts trailing version section above link refs" {
  write_two_versions_fixture
  run "$SCRIPT" 0.1.0 "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"First release content."* ]]
  [[ "$output" != *"Second release content."* ]]
  [[ "$output" != *"https://example"* ]]
}

@test "produces empty output for a heading with no body" {
  write_empty_section_fixture
  run "$SCRIPT" 0.1.0 "$FIXTURE"
  [ "$status" -eq 0 ]
  # Whitespace-only output, no real content.
  ! printf '%s' "$output" | grep -q '[^[:space:]]'
}

@test "produces empty output for a missing version" {
  write_single_version_fixture
  run "$SCRIPT" 9.9.9 "$FIXTURE"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "0.1.1 search does not match 0.1.10 heading (literal-prefix safety)" {
  write_prefix_collision_fixture
  run "$SCRIPT" 0.1.1 "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"one content."* ]]
  [[ "$output" != *"ten content."* ]]
}

@test "0.1.10 search returns the correct section without bleeding into 0.1.1" {
  write_prefix_collision_fixture
  run "$SCRIPT" 0.1.10 "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ten content."* ]]
  [[ "$output" != *"one content."* ]]
}

@test "exits 2 on missing argument" {
  run "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage:"* ]]
}

@test "exits 2 on too many arguments" {
  run "$SCRIPT" 0.1.0 "$FIXTURE" extra
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage:"* ]]
}

@test "exits 1 when CHANGELOG file is missing" {
  run "$SCRIPT" 0.1.0 "$TEST_ROOT/nonexistent.md"
  [ "$status" -eq 1 ]
  [[ "$output" == *"cannot read CHANGELOG file"* ]]
}
