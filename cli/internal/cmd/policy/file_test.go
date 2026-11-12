// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package policy

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

const testFile = "policy: v1\nclassifications:\n  - {match: {op: 'k8s.*'}, category: network}\n"

func seedToken(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
}

func run(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	root := NewRootCmd()
	var stdout, stderr bytes.Buffer
	root.SetOut(&stdout)
	root.SetErr(&stderr)
	root.SetArgs(args)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := root.ExecuteContext(ctx)
	return stdout.String(), stderr.String(), err
}

func writeFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "policy.yaml")
	if err := os.WriteFile(path, []byte(testFile), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	return path
}

// server answers one route with status and body, and records the request.
type seen struct {
	method, path, query, contentType, body string
}

func server(t *testing.T, status int, body string, got *seen) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		*got = seen{r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get("Content-Type"), string(raw)}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	seedToken(t, srv.URL)
	return srv
}

func TestValidateSendsTheFileAsYAML(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"ok":true,"scope":"tenant","mistakes":[],"warnings":[]}`, &got)
	stdout, stderr, err := run(t, "validate", writeFile(t), "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("validate: %v; stderr=%s", err, stderr)
	}
	if got.method != http.MethodPost || got.path != "/api/v1/policy/validate" {
		t.Errorf("unexpected %s %s", got.method, got.path)
	}
	if got.contentType != "application/yaml" || got.body != testFile {
		t.Errorf("file not sent as YAML: %q %q", got.contentType, got.body)
	}
	if !strings.Contains(got.query, "scope=tenant") {
		t.Errorf("scope missing: %q", got.query)
	}
	if !strings.Contains(stdout, "The file is valid.") {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestValidateListsMistakesAndFails(t *testing.T) {
	var got seen
	srv := server(t, 200,
		`{"ok":false,"scope":"tenant","mistakes":["`+"`routing`"+` is not a known key."],"warnings":[]}`, &got)
	stdout, _, err := run(t, "validate", writeFile(t), "--backplane", srv.URL)
	if err == nil {
		t.Fatal("expected an error for a file with mistakes")
	}
	if !strings.Contains(stdout, "mistake: `routing` is not a known key.") {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestDiffIsAPreviewAndPrintsCounts(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"preview":true,"ok":true,"scope":"install","mistakes":[],"warnings":[],`+
		`"no_change":false,"file_sha256":"a","based_on":"b","diff":{`+
		`"description":{"added":[],"changed":[],"removed":[]},`+
		`"taxonomy":{"added":["team-net"],"changed":[],"removed":[]},`+
		`"classifications":{"added":[{"entry":{"match":{"op":"k8s.*"}},"matches":3,"changes":2}],`+
		`"changed":[],"removed":[]},"operations_changed":2}}`, &got)
	stdout, stderr, err := run(t, "diff", writeFile(t), "--scope", "install", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("diff: %v; stderr=%s", err, stderr)
	}
	if got.path != "/api/v1/policy/import" || !strings.Contains(got.query, "preview=true") ||
		!strings.Contains(got.query, "scope=install") {
		t.Errorf("unexpected %s?%s", got.path, got.query)
	}
	for _, want := range []string{
		"+ taxonomy: team-net",
		"+ classifications: changes=2 entry={\"match\":{\"op\":\"k8s.*\"}} matches=3",
		"Operations whose values change: 2",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, stdout)
		}
	}
}

func TestImportPrintsTheWaitingRequest(t *testing.T) {
	var got seen
	srv := server(t, 202, `{"preview":false,"ok":true,"scope":"tenant","mistakes":[],"warnings":[],`+
		`"no_change":false,"file_sha256":"a","based_on":"b","diff":null,`+
		`"message":"The import waits until a second admin approves it. Nothing changed yet.",`+
		`"approval_request_id":"11111111-1111-1111-1111-111111111111"}`, &got)
	stdout, stderr, err := run(t, "import", writeFile(t), "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("import: %v; stderr=%s", err, stderr)
	}
	if strings.Contains(got.query, "preview=true") {
		t.Errorf("import must not be a preview: %q", got.query)
	}
	if !strings.Contains(stdout, "waits until a second admin approves it") ||
		!strings.Contains(stdout, "Approval request: 11111111-1111-1111-1111-111111111111") {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestImportNoChange(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"preview":false,"ok":true,"scope":"tenant","mistakes":[],"warnings":[],`+
		`"no_change":true,"file_sha256":"a","based_on":"b","diff":null,"message":"No change."}`, &got)
	stdout, _, err := run(t, "import", writeFile(t), "--backplane", srv.URL)
	if err != nil || strings.TrimSpace(stdout) != "No change." {
		t.Fatalf("err=%v stdout=%q", err, stdout)
	}
}

func TestImportRefusedWithTheSwitchOff(t *testing.T) {
	var got seen
	srv := server(t, 409, `{"detail":"The policy file import is off on this MEHO."}`, &got)
	_, stderr, err := run(t, "import", writeFile(t), "--backplane", srv.URL)
	if err == nil || !strings.Contains(stderr, "The policy file import is off on this MEHO.") {
		t.Fatalf("err=%v stderr=%s", err, stderr)
	}
}

func TestImportWithMistakesFails(t *testing.T) {
	var got seen
	srv := server(t, 422, `{"preview":false,"ok":false,"scope":"tenant","mistakes":["bad entry."],`+
		`"warnings":[],"no_change":false,"file_sha256":"a","based_on":"b","diff":null}`, &got)
	stdout, _, err := run(t, "import", writeFile(t), "--backplane", srv.URL)
	if err == nil || !strings.Contains(stdout, "mistake: bad entry.") {
		t.Fatalf("err=%v stdout=%s", err, stdout)
	}
}

func TestBadScopeIsRefused(t *testing.T) {
	_, stderr, err := run(t, "validate", writeFile(t), "--scope", "world", "--backplane", "http://x")
	if err == nil || !strings.Contains(stderr, "--scope must be tenant or install") {
		t.Fatalf("err=%v stderr=%s", err, stderr)
	}
}

func TestExportWritesTheFile(t *testing.T) {
	var got seen
	srv := server(t, 200, testFile, &got)
	out := filepath.Join(t.TempDir(), "out.yaml")
	_, stderr, err := run(t, "export", "-o", out, "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("export: %v; stderr=%s", err, stderr)
	}
	if got.method != http.MethodGet || got.path != "/api/v1/policy/export" {
		t.Errorf("unexpected %s %s", got.method, got.path)
	}
	data, _ := os.ReadFile(out)
	if string(data) != testFile {
		t.Errorf("file: %q", data)
	}
}

func TestHistoryListsImports(t *testing.T) {
	var got seen
	srv := server(t, 200, `[{"id":"11111111-1111-1111-1111-111111111111","ts":"2026-11-11T10:00:00Z",`+
		`"scope":"tenant","imported_by_sub":"admin-a","approved_by_sub":"admin-b",`+
		`"file_sha256":"0123456789abcdef","file":"policy: v1\n","diff":{}}]`, &got)
	stdout, stderr, err := run(t, "history", "--limit", "5", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("history: %v; stderr=%s", err, stderr)
	}
	if !strings.Contains(got.query, "limit=5") {
		t.Errorf("query: %q", got.query)
	}
	want := "2026-11-11T10:00:00Z  imported by admin-a  approved by admin-b  sha256 0123456789ab"
	if !strings.Contains(stdout, want) {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestHistoryLimitIsChecked(t *testing.T) {
	_, stderr, err := run(t, "history", "--limit", "21", "--backplane", "http://x")
	if err == nil || !strings.Contains(stderr, "--limit must be 1 to 20") {
		t.Fatalf("err=%v stderr=%s", err, stderr)
	}
}
