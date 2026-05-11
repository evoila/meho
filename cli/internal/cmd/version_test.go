// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/version"
)

// TestVersionCommandRendersAllFields locks the output contract of
// `meho version`: a single line containing the version, commit, and
// build-date strings exactly as injected by ldflags. Downstream
// consumers (install scripts, smoke tests in G2.8) parse this line —
// a layout change without updating them is a breaking change.
//
// t.Parallel() is deliberately NOT called: this test mutates the
// package-level version.Version/Commit/Date vars. Running it
// concurrently with any future sibling test that reaches RunE would
// race on those globals (m1 follow-up to PR #174's review). The
// other tests in this file also omit t.Parallel() for symmetry — the
// stream-routing test rebinds os.Stdout/os.Stderr globally, which
// equally rules out parallelism inside this package.
func TestVersionCommandRendersAllFields(t *testing.T) {
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

// TestVersionCommandWritesToRealStdoutNotStderr is the load-bearing
// stream-routing contract test. It catches the class of bug where
// the subcommand renders via cmd.Print/Printf/Println — those helpers
// delegate to cobra's OutOrStderr (see github.com/spf13/cobra@v1.10.2
// command.go:1434-1446), so an un-configured production binary
// writes its version banner to os.Stderr while a cobra-buffer-based
// test still sees the output land in the SetOut buffer (because both
// OutOrStdout and OutOrStderr return the same outWriter when SetOut
// has been called — see command.go:412-420 / 398-400).
//
// The only way to exercise the production code path in a unit test
// is to bypass cobra's writer override entirely: swap os.Stdout and
// os.Stderr for OS-level pipes, run Execute() WITHOUT calling
// SetOut/SetErr, then read each pipe back. Any future regression that
// reintroduces cmd.Print* will fail here even when the buffer-based
// assertion above keeps passing.
func TestVersionCommandWritesToRealStdoutNotStderr(t *testing.T) {
	origVersion, origCommit, origDate := version.Version, version.Commit, version.Date
	t.Cleanup(func() {
		version.Version = origVersion
		version.Commit = origCommit
		version.Date = origDate
	})
	version.Version = "v9.9.9-test"
	version.Commit = "deadbeef"
	version.Date = "2026-05-10T00:00:00Z"

	// Redirect the real OS-level stdout/stderr. We restore both in
	// the cleanup hook regardless of test outcome — a leaked
	// redirect would corrupt every subsequent test's output.
	origStdout, origStderr := os.Stdout, os.Stderr
	stdoutR, stdoutW, err := os.Pipe()
	if err != nil {
		t.Fatalf("create stdout pipe: %v", err)
	}
	stderrR, stderrW, err := os.Pipe()
	if err != nil {
		t.Fatalf("create stderr pipe: %v", err)
	}
	os.Stdout = stdoutW
	os.Stderr = stderrW
	t.Cleanup(func() {
		os.Stdout = origStdout
		os.Stderr = origStderr
	})

	// Run the command WITHOUT SetOut/SetErr so cobra falls back to
	// os.Stdout / os.Stderr defaults — the production path.
	root := newRootCmd()
	root.SetArgs([]string{"version"})
	execErr := root.Execute()

	// Close the writer ends so io.ReadAll on the reader sees EOF.
	if err := stdoutW.Close(); err != nil {
		t.Fatalf("close stdout pipe writer: %v", err)
	}
	if err := stderrW.Close(); err != nil {
		t.Fatalf("close stderr pipe writer: %v", err)
	}

	stdoutBytes, err := io.ReadAll(stdoutR)
	if err != nil {
		t.Fatalf("read stdout pipe: %v", err)
	}
	stderrBytes, err := io.ReadAll(stderrR)
	if err != nil {
		t.Fatalf("read stderr pipe: %v", err)
	}

	if execErr != nil {
		t.Fatalf("execute version: %v (stderr: %q)", execErr, string(stderrBytes))
	}
	if !strings.Contains(string(stdoutBytes), "meho v9.9.9-test") {
		t.Fatalf("version banner missing from real stdout: stdout=%q stderr=%q",
			string(stdoutBytes), string(stderrBytes))
	}
	if len(stderrBytes) != 0 {
		t.Fatalf("version wrote to real stderr (must be empty): %q",
			string(stderrBytes))
	}
}

// TestVersionCommandRejectsArgs guards the cobra.NoArgs contract —
// the subcommand takes no positional arguments today and shouldn't
// silently accept them tomorrow. Catches an accidental switch to
// cobra.ArbitraryArgs during refactors.
func TestVersionCommandRejectsArgs(t *testing.T) {
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
