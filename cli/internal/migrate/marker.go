// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

// TouchMarker writes the migration-complete marker for sourceDir.
// The marker path is $XDG_CONFIG_HOME/meho/migrated-from/<sanitized-dir>.
// Full implementation lives in T5 (#612); this stub satisfies the T4
// compile dependency and returns nil (no-op).
func TouchMarker(_ string) error { return nil }

// MarkerExists reports whether the migration marker for sourceDir is
// present. Full implementation in T5; stub always returns false.
func MarkerExists(_ string) (bool, error) { return false, nil }
