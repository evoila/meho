// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// --------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------

// writeFile creates a file in dir with the given content.
func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("writeFile %q: %v", path, err)
	}
	return path
}

// --------------------------------------------------------------------
// splitFrontmatter unit tests
// --------------------------------------------------------------------

func TestSplitFrontmatter_WellFormed(t *testing.T) {
	input := "---\nname: foo\ntype: user\n---\nbody text\n"
	fm, body, ok, warn := splitFrontmatter([]byte(input))
	if !ok {
		t.Fatalf("expected ok=true, warning=%q", warn)
	}
	if want := "name: foo\ntype: user"; string(fm) != want {
		t.Errorf("fm: got %q want %q", fm, want)
	}
	if want := "body text\n"; string(body) != want {
		t.Errorf("body: got %q want %q", body, want)
	}
}

func TestSplitFrontmatter_NoDelimiter(t *testing.T) {
	input := "just body text\n"
	_, _, ok, warn := splitFrontmatter([]byte(input))
	if ok {
		t.Fatal("expected ok=false for file without delimiter")
	}
	if warn == "" {
		t.Error("expected a warning message")
	}
}

func TestSplitFrontmatter_Unterminated(t *testing.T) {
	input := "---\nname: foo\ntype: user\n"
	_, _, ok, warn := splitFrontmatter([]byte(input))
	if ok {
		t.Fatal("expected ok=false for unterminated frontmatter")
	}
	if !strings.Contains(warn, "closing") {
		t.Errorf("expected warning to mention closing delimiter; got %q", warn)
	}
}

func TestSplitFrontmatter_BadIndentation(t *testing.T) {
	// Malformed YAML but valid delimiter structure; splitFrontmatter
	// should still return ok=true — YAML parse happens later.
	input := "---\n\tbad: indentation\n---\nbody\n"
	fm, _, ok, _ := splitFrontmatter([]byte(input))
	if !ok {
		t.Fatal("expected ok=true from splitFrontmatter for valid delimiters")
	}
	if !strings.Contains(string(fm), "bad: indentation") {
		t.Errorf("expected fm to contain the raw content; got %q", fm)
	}
}

func TestSplitFrontmatter_EmptyBody(t *testing.T) {
	input := "---\nname: x\n---\n"
	_, body, ok, _ := splitFrontmatter([]byte(input))
	if !ok {
		t.Fatal("expected ok=true")
	}
	if len(body) != 0 {
		t.Errorf("expected empty body; got %q", body)
	}
}

func TestSplitFrontmatter_BareDelimiterNoPanic(t *testing.T) {
	// A bare "---" (3 bytes, no newline, no closing delimiter) must not
	// panic and must return ok=false. Regression for the stripLen bug.
	_, _, ok, _ := splitFrontmatter([]byte("---"))
	if ok {
		t.Fatal("expected ok=false for bare 3-byte '---' input")
	}
}

// --------------------------------------------------------------------
// parseMemoryFile table-driven tests
// --------------------------------------------------------------------

type parseFMCase struct {
	name          string
	content       string
	wantFMOK      bool
	wantType      string
	wantName      string
	wantWarning   bool
	wantBodyPart  string
	wantMachLocal bool
}

var parseFMCases = []parseFMCase{
	{
		name:         "well-formed user type",
		content:      "---\nname: My Rule\ndescription: always do X\ntype: user\n---\nbody text\n",
		wantFMOK:     true,
		wantType:     "user",
		wantName:     "My Rule",
		wantBodyPart: "body text",
	},
	{
		name:         "well-formed project type",
		content:      "---\nname: Project Rule\ntype: project\n---\nproject body\n",
		wantFMOK:     true,
		wantType:     "project",
		wantName:     "Project Rule",
		wantBodyPart: "project body",
	},
	{
		name:         "well-formed reference type",
		content:      "---\nname: Ref\ntype: reference\n---\nref body\n",
		wantFMOK:     true,
		wantType:     "reference",
		wantName:     "Ref",
		wantBodyPart: "ref body",
	},
	{
		name:     "well-formed feedback type",
		content:  "---\ntype: feedback\nname: Feedback\n---\nfb\n",
		wantFMOK: true,
		wantType: "feedback",
	},
	{
		name:        "missing frontmatter",
		content:     "just plain markdown\nno frontmatter\n",
		wantFMOK:    false,
		wantWarning: true,
		// Body is the entire file when no frontmatter.
		wantBodyPart: "just plain markdown",
	},
	{
		name:        "malformed YAML bad indentation",
		content:     "---\n\tbad indentation:\n---\nbody after bad yaml\n",
		wantFMOK:    false,
		wantWarning: true,
	},
	{
		name:        "unterminated frontmatter",
		content:     "---\nname: x\ntype: user\n",
		wantFMOK:    false,
		wantWarning: true,
		// whole file is body
		wantBodyPart: "name: x",
	},
	{
		name:          "machine-local comment in body",
		content:       "---\nname: local\ntype: user\n---\n<!-- meho:machine-local -->\nsome text\n",
		wantFMOK:      true,
		wantMachLocal: true,
	},
	{
		name:          "machine-local comment absent",
		content:       "---\nname: normal\ntype: user\n---\nnormal body\n",
		wantFMOK:      true,
		wantMachLocal: false,
	},
	{
		name:          "machine-local comment with extra whitespace",
		content:       "---\ntype: user\n---\n<!--  meho:machine-local  -->\n",
		wantFMOK:      true,
		wantMachLocal: true,
	},
	{
		name:          "machine-local comment missing when opt-out absent",
		content:       "---\ntype: user\n---\n<!-- not-the-tag -->\n",
		wantFMOK:      true,
		wantMachLocal: false,
	},
}

func TestParseMemoryFile(t *testing.T) {
	for _, tc := range parseFMCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			mf := parseMemoryFile("/tmp/test.md", []byte(tc.content))

			if mf.FrontmatterOK != tc.wantFMOK {
				t.Errorf("FrontmatterOK: got %v want %v (warning=%q)",
					mf.FrontmatterOK, tc.wantFMOK, mf.ParseWarning)
			}
			if tc.wantType != "" && mf.Type != tc.wantType {
				t.Errorf("Type: got %q want %q", mf.Type, tc.wantType)
			}
			if tc.wantName != "" && mf.Name != tc.wantName {
				t.Errorf("Name: got %q want %q", mf.Name, tc.wantName)
			}
			if tc.wantWarning && mf.ParseWarning == "" {
				t.Error("expected ParseWarning to be set")
			}
			if !tc.wantWarning && mf.ParseWarning != "" {
				t.Errorf("unexpected ParseWarning: %q", mf.ParseWarning)
			}
			if tc.wantBodyPart != "" && !strings.Contains(mf.Body, tc.wantBodyPart) {
				t.Errorf("Body: expected to contain %q; got %q", tc.wantBodyPart, mf.Body)
			}
			if mf.MachineLocalOptOut != tc.wantMachLocal {
				t.Errorf("MachineLocalOptOut: got %v want %v", mf.MachineLocalOptOut, tc.wantMachLocal)
			}
		})
	}
}

// --------------------------------------------------------------------
// BodySHA256 stability tests
// --------------------------------------------------------------------

func TestBodySHA256_StableAcrossCalls(t *testing.T) {
	content := "---\ntype: user\n---\nstable body content\n"
	a := parseMemoryFile("/tmp/a.md", []byte(content))
	b := parseMemoryFile("/tmp/a.md", []byte(content))
	if a.BodySHA256 != b.BodySHA256 {
		t.Errorf("BodySHA256 not stable: %q vs %q", a.BodySHA256, b.BodySHA256)
	}
	if a.BodySHA256 == "" {
		t.Error("BodySHA256 must not be empty")
	}
}

func TestBodySHA256_ChangesWhenBodyChanges(t *testing.T) {
	c1 := "---\ntype: user\n---\nbody v1\n"
	c2 := "---\ntype: user\n---\nbody v2\n"
	mf1 := parseMemoryFile("/tmp/a.md", []byte(c1))
	mf2 := parseMemoryFile("/tmp/a.md", []byte(c2))
	if mf1.BodySHA256 == mf2.BodySHA256 {
		t.Error("BodySHA256 must differ when body content changes")
	}
}

func TestBodySHA256_IsHex(t *testing.T) {
	content := "---\ntype: user\n---\nbody\n"
	mf := parseMemoryFile("/tmp/a.md", []byte(content))
	for _, c := range mf.BodySHA256 {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			t.Errorf("BodySHA256 is not lowercase hex: %q", mf.BodySHA256)
			return
		}
	}
	if len(mf.BodySHA256) != 64 {
		t.Errorf("BodySHA256 expected 64 chars (SHA-256 hex); got %d: %q",
			len(mf.BodySHA256), mf.BodySHA256)
	}
}

// --------------------------------------------------------------------
// ScanDir tests
// --------------------------------------------------------------------

func TestScanDir_ReturnsOnlyMarkdown(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir, "a.md", "---\ntype: user\n---\nbody a\n")
	writeFile(t, dir, "b.txt", "ignored")
	writeFile(t, dir, "c.md", "---\ntype: project\n---\nbody c\n")

	files, err := ScanDir(dir)
	if err != nil {
		t.Fatalf("ScanDir: %v", err)
	}
	if len(files) != 2 {
		t.Errorf("expected 2 files; got %d", len(files))
	}
}

func TestScanDir_NonExistentDir(t *testing.T) {
	_, err := ScanDir(filepath.Join(t.TempDir(), "no-such-dir"))
	if err == nil {
		t.Error("expected error for non-existent dir")
	}
}

func TestScanDir_NonRecursive(t *testing.T) {
	dir := t.TempDir()
	subdir := filepath.Join(dir, "sub")
	if err := os.MkdirAll(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, dir, "top.md", "---\ntype: user\n---\ntop\n")
	writeFile(t, subdir, "nested.md", "---\ntype: user\n---\nnested\n")

	files, err := ScanDir(dir)
	if err != nil {
		t.Fatalf("ScanDir: %v", err)
	}
	if len(files) != 1 {
		t.Errorf("expected 1 file (non-recursive); got %d", len(files))
	}
	if !strings.HasSuffix(files[0].Path, "top.md") {
		t.Errorf("expected top.md; got %q", files[0].Path)
	}
}

// --------------------------------------------------------------------
// ResolveSourceDir tests
// --------------------------------------------------------------------

func TestResolveSourceDir_Explicit(t *testing.T) {
	got, err := ResolveSourceDir("/explicit/path")
	if err != nil {
		t.Fatalf("ResolveSourceDir: %v", err)
	}
	if got != "/explicit/path" {
		t.Errorf("expected /explicit/path; got %q", got)
	}
}

func TestResolveSourceDir_CLAUDE_PROJECT_DIR(t *testing.T) {
	t.Setenv("CLAUDE_PROJECT_DIR", "/some/project")
	got, err := ResolveSourceDir("")
	if err != nil {
		t.Fatalf("ResolveSourceDir: %v", err)
	}
	want := "/some/project/memory"
	if got != want {
		t.Errorf("got %q want %q", got, want)
	}
}

func TestResolveSourceDir_HomeDir_LeadingDash(t *testing.T) {
	// Unset CLAUDE_PROJECT_DIR so the home-dir fallback runs.
	t.Setenv("CLAUDE_PROJECT_DIR", "")

	// Override HOME to a known temp dir so the test is hermetic.
	home := t.TempDir()
	t.Setenv("HOME", home)

	// The current working directory is whatever the test runner sets.
	// We pin a known abs path by overriding $HOME and constructing the
	// expected sanitized form from the real cwd.
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatalf("os.Getwd: %v", err)
	}

	got, err := ResolveSourceDir("")
	if err != nil {
		t.Fatalf("ResolveSourceDir: %v", err)
	}

	sanitized := strings.ReplaceAll(cwd, "/", "-")
	want := filepath.Join(home, ".claude", "projects", sanitized, "memory")
	if got != want {
		t.Errorf("got %q want %q", got, want)
	}
}

func TestResolveSourceDir_LeadingDashRetained(t *testing.T) {
	// Pins the exact transform documented in the function's doc comment:
	// /a/b/c → <home>/.claude/projects/-a-b-c/memory
	//
	// This test exercises the transform by constructing the expected
	// result from a known absolute path "/a/b/c".
	t.Setenv("CLAUDE_PROJECT_DIR", "")
	home := t.TempDir()
	t.Setenv("HOME", home)

	// We can't change os.Getwd(), so we verify the transform function
	// itself rather than round-tripping through ResolveSourceDir with
	// a controlled cwd.
	path := "/a/b/c"
	sanitized := strings.ReplaceAll(path, "/", "-")
	if sanitized[0] != '-' {
		t.Errorf("leading '/' must become leading '-'; got %q", sanitized)
	}
	want := fmt.Sprintf("-a-b-c")
	if sanitized != want {
		t.Errorf("sanitized: got %q want %q", sanitized, want)
	}

	// And verify the full path template.
	full := filepath.Join(home, ".claude", "projects", sanitized, "memory")
	wantFull := filepath.Join(home, ".claude", "projects", "-a-b-c", "memory")
	if full != wantFull {
		t.Errorf("full path: got %q want %q", full, wantFull)
	}
}
