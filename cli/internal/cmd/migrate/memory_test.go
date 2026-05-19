// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/migrate"
)

// writeFixture creates a memory-file fixture under dir.
func writeFixture(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	return path
}

// runMemory executes the memory command with args and a captured
// stdout, returning (stdout, stderr, error).
func runMemory(t *testing.T, args []string, submitFn SubmitFn) (string, string, error) {
	t.Helper()
	cmd := newMemoryCmdWithSubmit(submitFn)
	var outBuf, errBuf bytes.Buffer
	cmd.SetOut(&outBuf)
	cmd.SetErr(&errBuf)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return outBuf.String(), errBuf.String(), err
}

// ── --dry-run ─────────────────────────────────────────────────────────────────

const userFixture = `---
name: daily-routine
description: my daily workflow
type: user
---
I start my day by checking email.
`

const feedbackFixture = `---
name: code-style
description: coding preferences
type: feedback
---
Prefer explicit error handling.
`

const projectFixture = `---
name: project-notes
description: team context
type: project
---
Team uses a shared kanban board.
`

func TestDryRun_EmitsJSONEnvelopes(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "user.md", userFixture)
	writeFixture(t, dir, "feedback.md", feedbackFixture)

	called := false
	submitFn := func(_ []migrate.SubmitPlan) error { called = true; return nil }

	stdout, _, err := runMemory(t, []string{"--source", dir, "--dry-run"}, submitFn)
	if err != nil {
		t.Fatalf("dry-run failed: %v", err)
	}
	if called {
		t.Error("--dry-run must not call submitFn")
	}

	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 JSON lines, got %d: %q", len(lines), stdout)
	}
	for _, line := range lines {
		var env map[string]any
		if err := json.Unmarshal([]byte(line), &env); err != nil {
			t.Errorf("line is not valid JSON: %q — %v", line, err)
		}
		for _, field := range []string{"scope", "slug", "body", "metadata", "source_id"} {
			if _, ok := env[field]; !ok {
				t.Errorf("dry-run envelope missing field %q", field)
			}
		}
		sid, _ := env["source_id"].(string)
		if !strings.HasPrefix(sid, "laptop-migration/") {
			t.Errorf("source_id = %q; want prefix laptop-migration/", sid)
		}
		// prefix length check: 17 ("laptop-migration/") + SourceIDPrefix hex chars
		wantLen := 17 + SourceIDPrefix
		if len(sid) != wantLen {
			t.Errorf("source_id len = %d; want %d", len(sid), wantLen)
		}
	}
}

func TestDryRun_MachineLocalFilesSkipped(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "user.md", userFixture)
	writeFixture(t, dir, "local.md", "---\nname: local\ndescription: d\ntype: user\n---\n/Users/bob/mypath\n")

	stdout, _, err := runMemory(t, []string{"--source", dir, "--dry-run"}, func(_ []migrate.SubmitPlan) error { return nil })
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	// only user.md emits an envelope (local.md is skipped)
	if len(lines) != 1 {
		t.Errorf("expected 1 envelope (machine-local skipped), got %d: %q", len(lines), stdout)
	}
}

func TestDryRun_IncludeMachineLocalFlag(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "local.md", "---\nname: local\ndescription: d\ntype: user\n---\n/Users/bob/mypath\n")

	stdout, _, err := runMemory(t, []string{"--source", dir, "--dry-run", "--include-machine-local"},
		func(_ []migrate.SubmitPlan) error { return nil })
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	if len(lines) != 1 {
		t.Errorf("expected 1 envelope with --include-machine-local, got %d", len(lines))
	}
}

// ── --non-interactive ─────────────────────────────────────────────────────────

func TestNonInteractive_UserFeedbackMigrated(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "user.md", userFixture)
	writeFixture(t, dir, "feedback.md", feedbackFixture)

	var submitted []migrate.SubmitPlan
	submitFn := func(plans []migrate.SubmitPlan) error {
		submitted = append(submitted, plans...)
		return nil
	}

	_, _, err := runMemory(t, []string{"--source", dir, "--non-interactive"}, submitFn)
	if err != nil {
		t.Fatalf("non-interactive failed: %v", err)
	}
	if len(submitted) != 2 {
		t.Errorf("expected 2 submitted plans, got %d", len(submitted))
	}
}

func TestNonInteractive_ProjectRefused(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "project.md", projectFixture)

	var submitted []migrate.SubmitPlan
	submitFn := func(plans []migrate.SubmitPlan) error {
		submitted = append(submitted, plans...)
		return nil
	}

	_, stderr, err := runMemory(t, []string{"--source", dir, "--non-interactive"}, submitFn)
	if err == nil {
		t.Error("expected non-zero exit when project entries present")
	}
	if len(submitted) != 0 {
		t.Errorf("expected 0 submitted (project skipped), got %d", len(submitted))
	}
	if !strings.Contains(stderr, "interactive review") {
		t.Errorf("stderr should mention interactive review, got: %q", stderr)
	}
}

func TestNonInteractive_MachineLocalSkipped(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "user.md", userFixture)
	// machine-local file should be skipped even with --include-machine-local
	writeFixture(t, dir, "local.md", "---\nname: local\ndescription: d\ntype: user\n---\n/Users/bob/path\n")

	var submitted []migrate.SubmitPlan
	submitFn := func(plans []migrate.SubmitPlan) error {
		submitted = append(submitted, plans...)
		return nil
	}

	_, _, _ = runMemory(t, []string{"--source", dir, "--non-interactive", "--include-machine-local"}, submitFn)
	for _, p := range submitted {
		if strings.Contains(p.File.Body, "/Users/bob") {
			t.Error("machine-local file must not be submitted in --non-interactive mode")
		}
	}
}

// ── source_id stability ───────────────────────────────────────────────────────

func TestDryRun_SourceIDStable(t *testing.T) {
	dir := t.TempDir()
	writeFixture(t, dir, "user.md", userFixture)

	run := func() string {
		out, _, err := runMemory(t, []string{"--source", dir, "--dry-run"},
			func(_ []migrate.SubmitPlan) error { return nil })
		if err != nil {
			t.Fatalf("dry-run error: %v", err)
		}
		var env map[string]any
		if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &env); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return env["source_id"].(string)
	}

	id1, id2 := run(), run()
	if id1 != id2 {
		t.Errorf("source_id is not stable: %q != %q", id1, id2)
	}
}

// ── empty source dir ──────────────────────────────────────────────────────────

func TestDryRun_EmptyDir(t *testing.T) {
	dir := t.TempDir()
	stdout, _, err := runMemory(t, []string{"--source", dir, "--dry-run"},
		func(_ []migrate.SubmitPlan) error { return nil })
	if err != nil {
		t.Fatalf("unexpected error on empty dir: %v", err)
	}
	if !strings.Contains(stdout, "No memory files") {
		t.Errorf("expected 'No memory files' message, got: %q", stdout)
	}
}
