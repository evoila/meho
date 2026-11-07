// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// runRootCmd runs `meho dashboard <args>` through cobra.
func runRootCmd(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	cmd, stdout, stderr := newRunCmd(t)
	root := NewRootCmd()
	root.SetOut(stdout)
	root.SetErr(stderr)
	root.SetArgs(args)
	err := root.ExecuteContext(cmd.Context())
	return stdout.String(), stderr.String(), err
}

const stubDashboardFile = `{"format":"meho-dashboard/1","dashboard":{"name":"estate"},` +
	`"sensors":[{"name":"items-a","connector_id":"x-rest-1.0","op_id":"x.item.list",` +
	`"target":"site-a-one","params":{"limit":5},` +
	`"assertion":{"select":{"path":"$.count"},"compare":{"type":"threshold","op":"lt","critical":10}},` +
	`"cadence_kind":"interval","interval_seconds":60}]}`

func TestRunExportWritesTheFile(t *testing.T) {
	var gotName string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/checks/dashboard-files/export" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		gotName = r.URL.Query().Get("name")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, stubDashboardFile)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	out := filepath.Join(t.TempDir(), "estate.json")
	_, stderr, err := runRootCmd(t, "export", "estate", "--output", out, "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("export: %v; stderr=%s", err, stderr)
	}
	if gotName != "estate" {
		t.Errorf("name query wrong: %q", gotName)
	}
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	var got, want any
	_ = json.Unmarshal(data, &got)
	_ = json.Unmarshal([]byte(stubDashboardFile), &want)
	gotJSON, _ := json.Marshal(got)
	wantJSON, _ := json.Marshal(want)
	if string(gotJSON) != string(wantJSON) {
		t.Errorf("file differs from the server answer:\n%s\n%s", gotJSON, wantJSON)
	}
}

func TestRunExportNotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"detail":"dashboard_not_found"}`)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	_, stderr, err := runRootCmd(t, "export", "nope", "--backplane", srv.URL)
	if err == nil || !strings.Contains(stderr, "dashboard_not_found") {
		t.Fatalf("expected dashboard_not_found; err=%v stderr=%s", err, stderr)
	}
}

func writeStubFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "estate.json")
	if err := os.WriteFile(path, []byte(stubDashboardFile), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	return path
}

func TestRunImportSendsFileMapAndFlags(t *testing.T) {
	var raw map[string]json.RawMessage
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/checks/dashboard-files/import" {
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &raw); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"dry_run":true,"changed":true,"dashboard_id":null,`+
			`"dashboard_name":"estate","dashboard_action":"create","sensors":`+
			`[{"name":"items-a","action":"create"},{"name":"items-b","action":"update",`+
			`"changed_fields":["severity"]}],"members_added":["items-a"],"members_removed":["old"]}`)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runRootCmd(t, "import", writeStubFile(t),
		"--map-target", "site-a-one=site-b-one", "--dry-run", "--force", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("import: %v; stderr=%s", err, stderr)
	}
	var targetMap map[string]string
	_ = json.Unmarshal(raw["target_map"], &targetMap)
	if targetMap["site-a-one"] != "site-b-one" || len(targetMap) != 1 {
		t.Errorf("target_map wrong: %s", raw["target_map"])
	}
	if string(raw["dry_run"]) != "true" || string(raw["force"]) != "true" {
		t.Errorf("flags wrong: dry_run=%s force=%s", raw["dry_run"], raw["force"])
	}
	var sentFile, wantFile any
	_ = json.Unmarshal(raw["file"], &sentFile)
	_ = json.Unmarshal([]byte(stubDashboardFile), &wantFile)
	a, _ := json.Marshal(sentFile)
	b, _ := json.Marshal(wantFile)
	if string(a) != string(b) {
		t.Errorf("file not sent as read:\n%s\n%s", a, b)
	}
	for _, want := range []string{
		"dry run: nothing was written",
		"dashboard estate: create",
		"sensor items-b: update (severity)",
		"members added: items-a",
		"members removed (not deleted): old",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout misses %q:\n%s", want, stdout)
		}
	}
}

func TestRunImportListsEveryProblem(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = io.WriteString(w, `{"detail":[`+
			`{"loc":["body","target_map","site-a-one"],"msg":"target 'site-a-one' is not mapped",`+
			`"type":"dashboard_import_unmapped_target"},`+
			`{"loc":["body","file"],"msg":"sensor 'items-a': operation 'x.item.list' is not found here",`+
			`"type":"dashboard_import_unknown_operation"}]}`)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	_, stderr, err := runRootCmd(t, "import", writeStubFile(t), "--backplane", srv.URL)
	if err == nil {
		t.Fatal("expected an error")
	}
	for _, want := range []string{"'site-a-one' is not mapped", "'x.item.list' is not found here"} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr misses %q:\n%s", want, stderr)
		}
	}
}

func TestRunImportNothingChanged(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"dry_run":false,"changed":false,`+
			`"dashboard_id":"11111111-1111-1111-1111-111111111111","dashboard_name":"estate",`+
			`"dashboard_action":"unchanged","sensors":[{"name":"items-a","action":"unchanged"}]}`)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	stdout, stderr, err := runRootCmd(t, "import", writeStubFile(t),
		"--map-target", "site-a-one=site-b-one", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("import: %v; stderr=%s", err, stderr)
	}
	if !strings.Contains(stdout, "nothing changed") {
		t.Errorf("expected 'nothing changed':\n%s", stdout)
	}
}

func TestRunImportRejectsBadInputLocally(t *testing.T) {
	dir := t.TempDir()
	notJSON := filepath.Join(dir, "bad.json")
	_ = os.WriteFile(notJSON, []byte("not json"), 0o600)
	good := writeStubFile(t)
	cases := [][]string{
		{filepath.Join(dir, "missing.json")},
		{notJSON},
		{good, "--map-target", "no-equals-sign"},
		{good, "--map-target", "=target"},
		{good, "--map-target", "a=b", "--map-target", "a=c"},
		{good, "--tenant", "nope"},
	}
	for _, args := range cases {
		called := false
		srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
			called = true
		}))
		seedXDGAndToken(t, srv.URL)
		_, _, err := runRootCmd(t, append(append([]string{"import"}, args...), "--backplane", srv.URL)...)
		srv.Close()
		if err == nil {
			t.Errorf("%v: expected an error", args)
		}
		if called {
			t.Errorf("%v: no request may be sent", args)
		}
	}
}
