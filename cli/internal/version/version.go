// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package version exposes build-time identity information for the
// meho CLI binary. The three package-level vars below are overridden
// at link time via -ldflags '-X github.com/evoila/meho/cli/internal/version.<Field>=...'
// — see the Makefile for the canonical invocation. The defaults
// ("dev" / "unknown") are what appears in `go run` / `go install`
// builds where ldflags are absent; an operator seeing them in a
// release binary is a packaging bug.
package version

var (
	// Version is the semver tag of this build (e.g. "v0.1.0").
	Version = "dev"
	// Commit is the short git SHA the build was produced from.
	Commit = "unknown"
	// Date is the RFC3339 UTC timestamp of the build.
	Date = "unknown"
)

// Info bundles the three build-time strings into a single struct so
// callers (the `version` subcommand, future `status` output, the
// User-Agent header on backplane requests) can pass them around as
// one value rather than reading three package vars.
type Info struct {
	Version string
	Commit  string
	Date    string
}

// Get returns a snapshot of the current build-time identity. It's a
// function (not a var) so the linker-injected values are observed
// even when the test suite mutates the package vars at runtime.
func Get() Info {
	return Info{Version: Version, Commit: Commit, Date: Date}
}
