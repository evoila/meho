// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package settings

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

const sampleFile = `apiVersion: meho.evoila.com/v1
kind: Settings
spec:
  tenants:
  - id: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
    targets:
    - name: vc-01
      product: vsphere
      version: '8.0'
      host: 10.1.0.1
      extras: {}
`

const tenantA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

// fakeInstall is one MEHO: it holds a file, and a list of revisions an
// apply adds to.
type fakeInstall struct {
	file      string
	revisions []string
	mistakes  []string
	failed    []string
	applies   int
	previews  int
	exportAs  int
	exportRaw string
	importAs  int
	seenType  string
}

func (f *fakeInstall) server(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/settings/export", func(w http.ResponseWriter, _ *http.Request) {
		if f.exportAs != 0 {
			w.WriteHeader(f.exportAs)
			_, _ = io.WriteString(w, f.exportRaw)
			return
		}
		w.Header().Set("Content-Type", "application/yaml")
		_, _ = io.WriteString(w, f.file)
	})
	mux.HandleFunc("POST /api/v1/settings/import", func(w http.ResponseWriter, r *http.Request) {
		f.seenType = r.Header.Get("Content-Type")
		body, _ := io.ReadAll(r.Body)
		if f.importAs != 0 {
			w.WriteHeader(f.importAs)
			_, _ = io.WriteString(w, `{"detail":"insufficient_role"}`)
			return
		}
		preview := r.URL.Query().Get("preview") == "true"
		answer := map[string]any{
			"preview": preview, "ok": len(f.mistakes) == 0,
			"mistakes": f.mistakes, "warnings": []string{}, "diff": nil,
			"applied": false, "revision_id": nil, "failed": []string{},
		}
		if f.mistakes == nil {
			answer["mistakes"] = []string{}
			answer["diff"] = f.diffFor(string(body))
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case preview:
			f.previews++
		case len(f.mistakes) > 0:
			w.WriteHeader(http.StatusUnprocessableEntity)
		default:
			f.applies++
			revision := fmt.Sprintf("00000000-0000-0000-0000-00000000000%d", f.applies)
			f.revisions = append(f.revisions, revision)
			f.file = string(body)
			answer["applied"], answer["revision_id"] = true, revision
			answer["ok"], answer["failed"] = len(f.failed) == 0, f.failed
		}
		_ = json.NewEncoder(w).Encode(answer)
	})
	mux.HandleFunc("GET /api/v1/settings/history", func(w http.ResponseWriter, _ *http.Request) {
		rows := []map[string]any{}
		for _, id := range f.revisions {
			rows = append(rows, map[string]any{
				"id": id, "ts": "2026-11-06T12:00:00Z", "actor_sub": "admin-person",
				"audit_id": id, "file_sha256": "abc", "api_version": "meho.evoila.com/v1",
				"exported_from": nil, "diff": map[string]any{},
				"summary": map[string]any{"targets": map[string]int{"added": 1, "changed": 2}},
			})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(rows)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func (f *fakeInstall) diffFor(body string) map[string]any {
	empty := func() map[string]any {
		return map[string]any{"added": []any{}, "changed": []any{}, "not_in_file": []any{}}
	}
	targets := empty()
	if f.file != body {
		targets["added"] = []any{map[string]any{"tenant": tenantA, "key": "vc-01", "value": map[string]any{}}}
		targets["not_in_file"] = []any{map[string]any{"tenant": tenantA, "key": "vc-99"}}
	}
	return map[string]any{"tenants": empty(), "policies": empty(), "conventions": empty(), "targets": targets}
}

func seedLogin(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatal(err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL, AccessToken: "test-token", TokenType: "Bearer",
		Expiry: time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatal(err)
	}
	if err := auth.SaveConfigAt(filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL}); err != nil {
		t.Fatal(err)
	}
}

type result struct {
	stdout, stderr string
	err            error
}

func run(t *testing.T, stdin string, args ...string) result {
	t.Helper()
	cmd := NewRootCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetIn(strings.NewReader(stdin))
	cmd.SetArgs(args)
	err := cmd.Execute()
	return result{stdout.String(), stderr.String(), err}
}

func exitCode(t *testing.T, err error) int {
	t.Helper()
	if err == nil {
		return 0
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("not an exit coder: %v", err)
	}
	return coder.ExitCode()
}

func writeSample(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "meho-settings.yaml")
	if err := os.WriteFile(path, []byte(sampleFile), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// The first acceptance criterion: export from one install, preview on a
// second (prints the diff, writes nothing), apply, and history lists it.
func TestExportPreviewApplyHistoryAcrossTwoInstalls(t *testing.T) {
	first := &fakeInstall{file: sampleFile}
	firstSrv := first.server(t)
	second := &fakeInstall{file: "other"}
	secondSrv := second.server(t)
	seedLogin(t, firstSrv.URL)

	path := filepath.Join(t.TempDir(), "out.yaml")
	if got := run(t, "", "export", "--out", path); got.err != nil {
		t.Fatalf("export: %v %s", got.err, got.stderr)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != sampleFile {
		t.Fatalf("export wrote %q, %v", data, err)
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0o600 {
		t.Errorf("mode = %v", info.Mode().Perm())
	}

	seedLogin(t, secondSrv.URL)
	preview := run(t, "", "import", path, "--preview")
	if preview.err != nil {
		t.Fatalf("preview: %v", preview.err)
	}
	for _, want := range []string{"Targets:", "added", tenantA, "vc-01", "not in file", "vc-99", "never deletes", "nothing was written"} {
		if !strings.Contains(preview.stdout, want) {
			t.Errorf("preview misses %q:\n%s", want, preview.stdout)
		}
	}
	if second.applies != 0 || second.file != "other" {
		t.Fatalf("the preview wrote: applies=%d", second.applies)
	}

	applied := run(t, "", "import", path, "--yes")
	if applied.err != nil {
		t.Fatalf("apply: %v", applied.err)
	}
	if second.applies != 1 || second.file != sampleFile {
		t.Fatalf("applies=%d file=%q", second.applies, second.file)
	}
	if second.seenType != yamlContentType {
		t.Errorf("content type = %q", second.seenType)
	}
	if !strings.Contains(applied.stdout, "Applied; revision 00000000-0000-0000-0000-000000000001") {
		t.Errorf("apply output:\n%s", applied.stdout)
	}

	history := run(t, "", "history")
	if history.err != nil {
		t.Fatalf("history: %v", history.err)
	}
	for _, want := range []string{"admin-person", "targets +1 ~2", "00000000-0000-0000-0000-000000000001"} {
		if !strings.Contains(history.stdout, want) {
			t.Errorf("history misses %q:\n%s", want, history.stdout)
		}
	}
}

func TestPreviewJSONIsTheAnswerAsItIs(t *testing.T) {
	f := &fakeInstall{mistakes: []string{"`spec.tenants[0].targets[3].host` is required."}}
	srv := f.server(t)
	seedLogin(t, srv.URL)

	got := run(t, "", "import", writeSample(t), "--preview", "--json")
	if code := exitCode(t, got.err); code != output.ExitUnexpected {
		t.Fatalf("exit = %d", code)
	}
	var answer map[string]any
	if err := json.Unmarshal([]byte(got.stdout), &answer); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, got.stdout)
	}
	if answer["preview"] != true || answer["ok"] != false || answer["diff"] != nil {
		t.Errorf("answer = %v", answer)
	}
	mistakes, _ := answer["mistakes"].([]any)
	if len(mistakes) != 1 || mistakes[0] != f.mistakes[0] {
		t.Errorf("mistakes = %v", mistakes)
	}
	if f.applies != 0 {
		t.Error("a preview applied")
	}
}

func TestPreviewJSONCarriesTheDiff(t *testing.T) {
	f := &fakeInstall{file: "other"}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	got := run(t, "", "import", writeSample(t), "--preview", "--json")
	if got.err != nil {
		t.Fatal(got.err)
	}
	var answer struct {
		Diff struct {
			Targets struct {
				Added []map[string]any `json:"added"`
			} `json:"targets"`
		} `json:"diff"`
	}
	if err := json.Unmarshal([]byte(got.stdout), &answer); err != nil {
		t.Fatal(err)
	}
	if len(answer.Diff.Targets.Added) != 1 {
		t.Errorf("diff = %s", got.stdout)
	}
}

func TestMistakesPrintAsSentencesAndStopTheApply(t *testing.T) {
	f := &fakeInstall{mistakes: []string{"`apiVersion` is not a file format this MEHO reads."}}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	got := run(t, "", "import", writeSample(t), "--yes")
	if code := exitCode(t, got.err); code != output.ExitUnexpected {
		t.Fatalf("exit = %d", code)
	}
	if !strings.Contains(got.stdout, f.mistakes[0]) {
		t.Errorf("stdout:\n%s", got.stdout)
	}
	if f.applies != 0 {
		t.Error("applied a file with mistakes")
	}
}

func TestApplyAsksForAYes(t *testing.T) {
	cases := []struct {
		name    string
		stdin   string
		applies int
	}{
		{"yes", "y\n", 1},
		{"no", "n\n", 0},
		{"closed stdin", "", 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeInstall{file: "other"}
			srv := f.server(t)
			seedLogin(t, srv.URL)
			got := run(t, tc.stdin, "import", writeSample(t))
			if got.err != nil {
				t.Fatal(got.err)
			}
			if f.applies != tc.applies {
				t.Errorf("applies = %d, want %d", f.applies, tc.applies)
			}
			if !strings.Contains(got.stderr, "[y/N]") {
				t.Errorf("no question on stderr: %q", got.stderr)
			}
		})
	}
}

func TestAFileThatChangesNothingAsksNothing(t *testing.T) {
	f := &fakeInstall{file: sampleFile}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	got := run(t, "", "import", writeSample(t))
	if got.err != nil {
		t.Fatal(got.err)
	}
	if !strings.Contains(got.stdout, "Nothing to change.") || strings.Contains(got.stderr, "[y/N]") {
		t.Errorf("stdout %q stderr %q", got.stdout, got.stderr)
	}
	if f.applies != 0 {
		t.Error("applied")
	}
}

func TestApplyWithJSONNeedsYes(t *testing.T) {
	got := run(t, "", "import", "whatever.yaml", "--json")
	if code := exitCode(t, got.err); code != output.ExitUsage {
		t.Fatalf("exit = %d", code)
	}
}

func TestApplyJSONPrintsTheApplyAnswerOnly(t *testing.T) {
	f := &fakeInstall{file: "other"}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	got := run(t, "", "import", writeSample(t), "--yes", "--json")
	if got.err != nil {
		t.Fatal(got.err)
	}
	var answer map[string]any
	if err := json.Unmarshal([]byte(got.stdout), &answer); err != nil {
		t.Fatalf("stdout: %v\n%s", err, got.stdout)
	}
	if answer["preview"] != false || answer["applied"] != true || f.previews != 0 {
		t.Errorf("answer %v previews %d", answer, f.previews)
	}
}

func TestItemsThatFailedExitNonZeroAndAreListed(t *testing.T) {
	f := &fakeInstall{file: "other", failed: []string{"`vc-01` could not be written."}}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	got := run(t, "", "import", writeSample(t), "--yes")
	if code := exitCode(t, got.err); code != output.ExitUnexpected {
		t.Fatalf("exit = %d", code)
	}
	if !strings.Contains(got.stdout, f.failed[0]) {
		t.Errorf("stdout:\n%s", got.stdout)
	}
}

func TestExitCodes(t *testing.T) {
	t.Run("403 is insufficient_role", func(t *testing.T) {
		f := &fakeInstall{importAs: http.StatusForbidden, exportAs: http.StatusForbidden,
			exportRaw: `{"detail":"insufficient_role"}`}
		srv := f.server(t)
		seedLogin(t, srv.URL)
		for _, args := range [][]string{{"import", writeSample(t), "--preview"}, {"export"}} {
			got := run(t, "", args...)
			if code := exitCode(t, got.err); code != output.ExitInsufficientRole {
				t.Errorf("%v: exit = %d", args, code)
			}
		}
	})
	t.Run("401 with no refresh is auth_expired", func(t *testing.T) {
		f := &fakeInstall{exportAs: http.StatusUnauthorized, exportRaw: `{}`}
		srv := f.server(t)
		seedLogin(t, srv.URL)
		if code := exitCode(t, run(t, "", "export").err); code != output.ExitAuthExpired {
			t.Errorf("exit = %d", code)
		}
	})
	t.Run("no login is auth_expired", func(t *testing.T) {
		t.Setenv("XDG_CONFIG_HOME", t.TempDir())
		t.Setenv("MEHO_KEYRING_DISABLE", "1")
		if code := exitCode(t, run(t, "", "history").err); code != output.ExitAuthExpired {
			t.Errorf("exit = %d", code)
		}
	})
	t.Run("connection refused is unreachable", func(t *testing.T) {
		f := &fakeInstall{}
		srv := f.server(t)
		seedLogin(t, srv.URL)
		srv.Close()
		if code := exitCode(t, run(t, "", "history").err); code != output.ExitUnreachable {
			t.Errorf("exit = %d", code)
		}
	})
	t.Run("a file that cannot be read is unexpected", func(t *testing.T) {
		got := run(t, "", "import", filepath.Join(t.TempDir(), "gone.yaml"), "--preview")
		if code := exitCode(t, got.err); code != output.ExitUnexpected {
			t.Errorf("exit = %d", code)
		}
	})
}

func TestRefusedExportPrintsTheSentencesAndWritesNoFile(t *testing.T) {
	f := &fakeInstall{exportAs: http.StatusConflict,
		exportRaw: `{"detail":{"kind":"settings_export_refused","mistakes":["` +
			"`stored.a.targets.vc.extras.password` is named like a credential." + `"]}}`}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	path := filepath.Join(t.TempDir(), "out.yaml")
	got := run(t, "", "export", "--out", path)
	if code := exitCode(t, got.err); code != output.ExitUnexpected {
		t.Fatalf("exit = %d", code)
	}
	if !strings.Contains(got.stderr, "is named like a credential") {
		t.Errorf("stderr: %s", got.stderr)
	}
	if _, err := os.Stat(path); err == nil {
		t.Error("a refused export wrote a file")
	}
}

func TestEmptyHistorySaysSo(t *testing.T) {
	f := &fakeInstall{}
	srv := f.server(t)
	seedLogin(t, srv.URL)
	got := run(t, "", "history")
	if got.err != nil || !strings.Contains(got.stdout, "No settings import") {
		t.Errorf("%v %q", got.err, got.stdout)
	}
	raw := run(t, "", "history", "--json")
	if raw.err != nil || strings.TrimSpace(raw.stdout) != "[]" {
		t.Errorf("%v %q", raw.err, raw.stdout)
	}
}

// The same two files are read by backend/tests/test_ui_settings.py, so
// the CLI and the console render one diff to the same rows.
func TestRowsMatchTheFixtureTheConsoleReads(t *testing.T) {
	var diff diffBody
	readJSON(t, "testdata/preview_diff.json", &diff)
	var want []diffRow
	readJSON(t, "testdata/preview_rows.json", &want)
	got := diffRows(&diff)
	gotJSON, _ := json.MarshalIndent(got, "", " ")
	wantJSON, _ := json.MarshalIndent(want, "", " ")
	if !bytes.Equal(gotJSON, wantJSON) {
		t.Errorf("rows:\n%s\nwant:\n%s", gotJSON, wantJSON)
	}
}

func readJSON(t *testing.T, path string, into any) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, into); err != nil {
		t.Fatal(err)
	}
}

// A refresh token the login server refuses is a login problem: exit 2,
// not "unreachable".
func TestRevokedRefreshTokenAsksForALogin(t *testing.T) {
	var srv *httptest.Server
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/settings/export", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	mux.HandleFunc("/realms/meho/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"issuer":                        srv.URL + "/realms/meho",
			"device_authorization_endpoint": srv.URL + "/device",
			"token_endpoint":                srv.URL + "/token",
		})
	})
	mux.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"invalid_grant","error_description":"Session not active"}`))
	})
	srv = httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatal(err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL, Issuer: srv.URL + "/realms/meho", ClientID: "meho-cli",
		AccessToken: "eyJ.test.token", RefreshToken: "revoked", TokenType: "Bearer",
		Expiry: time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatal(err)
	}

	cmd := NewRootCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"export", "--backplane", srv.URL})
	err = cmd.Execute()
	var coded output.ExitCoder
	if !errors.As(err, &coded) || coded.ExitCode() != 2 {
		t.Fatalf("err = %v; want exit code 2 (stderr %q)", err, stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("stderr = %q", stderr.String())
	}
}
