// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const pageA = "---\ntype: fact\napplies_to: product-and-version\nstatus: confirmed\nsource: meho://kb/a\n---\nA text.\n"

// exportServer answers POST /api/v1/kb/export with result and counts the calls.
func exportServer(t *testing.T, result api.KbExportResult) (*httptest.Server, *int) {
	t.Helper()
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/export", func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(result)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv, &calls
}

func sampleExport() api.KbExportResult {
	return api.KbExportResult{
		PageCount: 3,
		Pages: []api.KbExportPage{
			{Path: "a.md", Slug: "a", Content: pageA},
			{Path: "b.md", Slug: "b", Content: "---\ntype: fact\n---\nNew B.\n"},
			{Path: "c.md", Slug: "c", Content: "---\ntype: fact\n---\nC.\n"},
		},
		RemoveCount: 2,
		Remove: []api.KbExportRemoval{
			{Path: "old.md", Slug: "old", Status: "retired"},
			{Path: "never-there.md", Slug: "never-there", Status: "replaced"},
		},
		SkippedCount: 1,
		Skipped:      []api.KbExportSkip{{Slug: "with-secret", Reason: "the entry holds what looks like a secret (api_key on line 2); remove it"}},
	}
}

// seedWiki makes a folder with a.md unchanged, b.md changed, old.md to
// remove and keep.md that the export must not touch.
func seedWiki(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range map[string]string{
		"a.md":    pageA,
		"b.md":    "---\ntype: fact\n---\nOld B.\n",
		"old.md":  "retired page\n",
		"keep.md": "a page written in the wiki\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(raw)
}

func TestRunExportWritesAddsChangesAndRemoves(t *testing.T) {
	srv, calls := exportServer(t, sampleExport())
	dir := seedWiki(t)
	cmd, stdout, stderr := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runExport: %v; stderr=%s", err, stderr.String())
	}
	if *calls != 1 {
		t.Errorf("expected one call; got %d", *calls)
	}
	if got := readFile(t, filepath.Join(dir, "b.md")); !strings.Contains(got, "New B.") {
		t.Errorf("b.md not changed: %q", got)
	}
	if got := readFile(t, filepath.Join(dir, "c.md")); !strings.Contains(got, "C.") {
		t.Errorf("c.md not added: %q", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "old.md")); !os.IsNotExist(err) {
		t.Errorf("old.md should be removed; stat err=%v", err)
	}
	if got := readFile(t, filepath.Join(dir, "keep.md")); got != "a page written in the wiki\n" {
		t.Errorf("keep.md was touched: %q", got)
	}
	out := stdout.String()
	for _, want := range []string{
		"add     " + filepath.Join(dir, "c.md"),
		"change  " + filepath.Join(dir, "b.md"),
		"remove  " + filepath.Join(dir, "old.md"),
		"added:      1", "changed:    1", "unchanged:  1", "removed:    1", "skipped:    1",
		"with-secret: the entry holds what looks like a secret",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "never-there") {
		t.Errorf("a file that is not there must not be listed as removed:\n%s", out)
	}
}

func TestRunExportSecondRunChangesNothing(t *testing.T) {
	srv, _ := exportServer(t, sampleExport())
	dir := seedWiki(t)
	cmd, _, _ := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("first run: %v", err)
	}
	cmd, stdout, _ := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL, JSONOut: true}); err != nil {
		t.Fatalf("second run: %v", err)
	}
	var plan exportPlan
	if err := json.Unmarshal(stdout.Bytes(), &plan); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(plan.Added)+len(plan.Changed)+len(plan.Removed) != 0 || len(plan.Unchanged) != 3 {
		t.Errorf("second run should change nothing; got %+v", plan)
	}
}

func TestRunExportDryRunWritesNothing(t *testing.T) {
	srv, _ := exportServer(t, sampleExport())
	dir := seedWiki(t)
	cmd, stdout, _ := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, DryRun: true, BackplaneOverride: srv.URL, JSONOut: true}); err != nil {
		t.Fatalf("runExport --dry-run: %v", err)
	}
	var plan exportPlan
	if err := json.Unmarshal(stdout.Bytes(), &plan); err != nil {
		t.Fatalf("stdout not JSON: %v", err)
	}
	if !plan.DryRun || fmt.Sprint(plan.Added, plan.Changed, plan.Unchanged, plan.Removed) != "[c.md] [b.md] [a.md] [old.md]" {
		t.Errorf("unexpected plan %+v", plan)
	}
	if got := readFile(t, filepath.Join(dir, "b.md")); !strings.Contains(got, "Old B.") {
		t.Errorf("dry run changed b.md: %q", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "c.md")); !os.IsNotExist(err) {
		t.Errorf("dry run wrote c.md")
	}
	if _, err := os.Stat(filepath.Join(dir, "old.md")); err != nil {
		t.Errorf("dry run removed old.md: %v", err)
	}
}

func TestRunExportDryRunDoesNotCreateTheFolder(t *testing.T) {
	srv, _ := exportServer(t, sampleExport())
	dir := filepath.Join(t.TempDir(), "new-folder")
	cmd, stdout, _ := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, DryRun: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runExport --dry-run: %v", err)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Errorf("dry run created the folder")
	}
	if !strings.Contains(stdout.String(), "dry-run: nothing was written") || !strings.Contains(stdout.String(), "added:      3") {
		t.Errorf("unexpected stdout %q", stdout.String())
	}
}

func TestRunExportCreatesAMissingFolder(t *testing.T) {
	srv, _ := exportServer(t, sampleExport())
	dir := filepath.Join(t.TempDir(), "from-meho")
	cmd, _, stderr := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runExport: %v; stderr=%s", err, stderr.String())
	}
	if got := readFile(t, filepath.Join(dir, "a.md")); got != pageA {
		t.Errorf("a.md = %q", got)
	}
}

func TestRunExportRefusesANameOutsideTheFolder(t *testing.T) {
	for _, bad := range []string{"../escape.md", "sub/x.md", `sub\x.md`, ".hidden.md", "x.txt", ""} {
		t.Run(bad, func(t *testing.T) {
			result := sampleExport()
			result.Remove = append(result.Remove, api.KbExportRemoval{Path: bad, Slug: "x", Status: "retired"})
			srv, _ := exportServer(t, result)
			dir := seedWiki(t)
			cmd, _, stderr := newRunCmd(t)
			if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL}); err == nil {
				t.Fatalf("expected an error for %q", bad)
			}
			if !strings.Contains(stderr.String(), "not a plain .md file name") {
				t.Errorf("stderr = %q", stderr.String())
			}
			if _, err := os.Stat(filepath.Join(dir, "c.md")); !os.IsNotExist(err) {
				t.Errorf("a bad name must stop every write; c.md was written")
			}
			if _, err := os.Stat(filepath.Join(dir, "old.md")); err != nil {
				t.Errorf("a bad name must stop every removal: %v", err)
			}
		})
	}
}

func TestRunExport403SurfacesTheDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/export", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"insufficient_role"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	dir := seedWiki(t)
	cmd, _, stderr := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL}); err == nil {
		t.Fatalf("expected an error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("stderr = %q", stderr.String())
	}
	if got := readFile(t, filepath.Join(dir, "b.md")); !strings.Contains(got, "Old B.") {
		t.Errorf("a failed call must write nothing: %q", got)
	}
}

func TestRunExportRejectsEmptyDirectory(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: ""}); err == nil {
		t.Fatalf("expected error for empty directory")
	} else if !strings.Contains(stderr.String(), "non-empty <directory>") {
		t.Errorf("expected hint; got %q", stderr.String())
	}
}

func TestRunExportRefusesALinkInTheFolder(t *testing.T) {
	srv, _ := exportServer(t, sampleExport())
	dir := seedWiki(t)
	outside := filepath.Join(t.TempDir(), "outside.md")
	if err := os.WriteFile(outside, []byte("outside\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "c.md")); err != nil {
		t.Skipf("no symlinks here: %v", err)
	}
	cmd, _, stderr := newRunCmd(t)
	if err := runExport(cmd, exportOptions{Directory: dir, BackplaneOverride: srv.URL}); err == nil {
		t.Fatalf("expected an error for a link")
	}
	if !strings.Contains(stderr.String(), "is a link or a folder") {
		t.Errorf("stderr = %q", stderr.String())
	}
	if got := readFile(t, outside); got != "outside\n" {
		t.Errorf("the file behind the link was written: %q", got)
	}
}
