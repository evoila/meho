// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/version"
)

// TestVersionCommandRendersAllFields locks the output contract of
// `meho version`: a single line containing the version, commit, and
// build-date strings exactly as injected by ldflags. Downstream
// consumers (install scripts, smoke tests in G2.8) parse this line —
// a layout change without updating them is a breaking change.
func TestVersionCommandRendersAllFields(t *testing.T) {
	t.Parallel()

	// Override the package-level vars for the duration of the test
	// to simulate an ldflags-injected build. defer-restore so the
	// global state is identical before and after — required because
	// the package vars are read by every future test in this package.
	origVersion, origCommit, origDate := version.Version, version.Commit, version.Date
	t.Cleanup(func() {
		version.Version = origVersion
		version.Commit = origCommit
		version.Date = origDate
	})
	version.Version = "v9.9.9-test"
	version.Commit = "deadbeef"
	version.Date = "2026-05-10T00:00:00Z"

	var stdout, stderr bytes.Buffer
	root := newRootCmd()
	root.SetOut(&stdout)
	root.SetErr(&stderr)
	root.SetArgs([]string{"version"})

	if err := root.Execute(); err != nil {
		t.Fatalf("execute version: %v", err)
	}

	got := stdout.String()
	want := "meho v9.9.9-test (commit deadbeef, built 2026-05-10T00:00:00Z)\n"
	if got != want {
		t.Fatalf("version output mismatch:\n got: %q\nwant: %q", got, want)
	}
	if stderr.Len() != 0 {
		t.Fatalf("version wrote to stderr: %q", stderr.String())
	}
}

// TestVersionCommandRejectsArgs guards the cobra.NoArgs contract —
// the subcommand takes no positional arguments today and shouldn't
// silently accept them tomorrow. Catches an accidental switch to
// cobra.ArbitraryArgs during refactors.
func TestVersionCommandRejectsArgs(t *testing.T) {
	t.Parallel()

	var stdout, stderr bytes.Buffer
	root := newRootCmd()
	root.SetOut(&stdout)
	root.SetErr(&stderr)
	root.SetArgs([]string{"version", "extra"})

	err := root.Execute()
	if err == nil {
		t.Fatalf("expected error for unexpected positional arg; got nil")
	}
	if !strings.Contains(err.Error(), "unknown command") &&
		!strings.Contains(err.Error(), "accepts") {
		t.Fatalf("unexpected error message: %v", err)
	}
}
