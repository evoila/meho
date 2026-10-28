// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// fakeDeleteServer answers every request with the status and body it
// is started with, and records the requests it saw.
type fakeDeleteServer struct {
	calls  int
	method string
	path   string
	query  string
}

func (f *fakeDeleteServer) start(t *testing.T, status int, reply string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls++
		f.method = r.Method
		f.path = r.URL.EscapedPath()
		f.query = r.URL.RawQuery
		if reply != "" {
			w.Header().Set("Content-Type", "application/json")
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(reply))
	}))
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv.URL
}

// runDeleteCmd drives a fresh `meho targets delete` command with the
// given stdin, appending --backplane when backplaneURL is set.
func runDeleteCmd(t *testing.T, backplaneURL, stdin string, args ...string) (*bytes.Buffer, *bytes.Buffer, error) {
	t.Helper()
	cmd := newDeleteCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetIn(strings.NewReader(stdin))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	full := append([]string{}, args...)
	if backplaneURL != "" {
		full = append(full, "--backplane", backplaneURL)
	}
	cmd.SetArgs(full)
	err := cmd.Execute()
	return &stdout, &stderr, err
}

// exitCodeOf returns the exit code an error carries, or -1.
func exitCodeOf(err error) int {
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); ok {
		return x.ExitCode()
	}
	return -1
}

const hasReferences409 = `{"detail":{"kind":"target_has_references","graph_node_refs":3,` +
	`"message":"target 'demo-vault' is referenced by 3 graph_node row(s); retry with ?force=true"}}`

// TestNewDeleteCmdRegistered pins that `delete` hangs off `meho targets`
// with the flags the issue asks for.
func TestNewDeleteCmdRegistered(t *testing.T) {
	t.Parallel()
	for _, c := range NewRootCmd().Commands() {
		if c.Name() != "delete" {
			continue
		}
		for _, flag := range []string{"force", "confirm", "json", "backplane"} {
			if c.Flags().Lookup(flag) == nil {
				t.Errorf("delete is missing the --%s flag", flag)
			}
		}
		if !strings.Contains(c.Long, "DELETE /api/v1/targets/{name}") {
			t.Errorf("delete help does not name its route:\n%s", c.Long)
		}
		return
	}
	t.Fatal("`meho targets delete` not registered on the root command")
}

// TestDeleteSuccess pins the happy path: one DELETE without force, the
// human line on stdout.
func TestDeleteSuccess(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusNoContent, "")
	stdout, stderr, err := runDeleteCmd(t, url, "", "demo-vault", "--confirm")
	if err != nil {
		t.Fatalf("delete: %v\nstderr=%s", err, stderr.String())
	}
	if f.method != http.MethodDelete || f.path != "/api/v1/targets/demo-vault" || f.query != "" {
		t.Errorf("want DELETE /api/v1/targets/demo-vault; got %s %s?%s", f.method, f.path, f.query)
	}
	if !strings.Contains(stdout.String(), `deleted target "demo-vault"`) {
		t.Errorf("stdout should confirm the delete; got:\n%s", stdout.String())
	}
}

// TestDeleteSuccessJSON pins the --json success envelope.
func TestDeleteSuccessJSON(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusNoContent, "")
	stdout, stderr, err := runDeleteCmd(t, url, "", "demo-vault", "--confirm", "--json")
	if err != nil {
		t.Fatalf("delete --json: %v\nstderr=%s", err, stderr.String())
	}
	var got deleteResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, stdout.String())
	}
	if got != (deleteResult{Name: "demo-vault", Status: "deleted"}) {
		t.Errorf("unexpected envelope: %+v", got)
	}
}

// TestDeleteForceSendsForceTrue pins that --force adds force=true.
func TestDeleteForceSendsForceTrue(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusNoContent, "")
	stdout, stderr, err := runDeleteCmd(t, url, "", "demo-vault", "--force", "--confirm", "--json")
	if err != nil {
		t.Fatalf("delete --force: %v\nstderr=%s", err, stderr.String())
	}
	if f.query != "force=true" {
		t.Errorf("want query force=true; got %q", f.query)
	}
	var got deleteResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil || !got.Force || got.Status != "deleted" {
		t.Errorf("unexpected envelope (err=%v): %s", err, stdout.String())
	}
}

// TestDeleteHasReferencesExplainsForce pins the 409 without --force:
// the message names the node count and the --force way out, in human
// and JSON mode.
func TestDeleteHasReferencesExplainsForce(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	want := "This target is linked from 3 topology nodes. The nodes stay, but they " +
		"will point at a deleted target. Run again with --force to delete it anyway."
	var f fakeDeleteServer
	url := f.start(t, http.StatusConflict, hasReferences409)

	_, stderr, err := runDeleteCmd(t, url, "", "demo-vault", "--confirm")
	if exitCodeOf(err) != 4 {
		t.Fatalf("409 should exit 4; got %v", err)
	}
	if !strings.Contains(stderr.String(), want) {
		t.Errorf("stderr should explain the 409; got:\n%s", stderr.String())
	}

	_, stderr, err = runDeleteCmd(t, url, "", "demo-vault", "--confirm", "--json")
	if exitCodeOf(err) != 4 {
		t.Fatalf("409 --json should exit 4; got %v", err)
	}
	var env struct {
		Error    string `json:"error"`
		Detail   string `json:"detail"`
		ExitCode int    `json:"exit_code"`
	}
	if jerr := json.Unmarshal(stderr.Bytes(), &env); jerr != nil {
		t.Fatalf("stderr is not the JSON error envelope: %v\n%s", jerr, stderr.String())
	}
	if env.Error != "unexpected_response" || env.Detail != want || env.ExitCode != 4 {
		t.Errorf("unexpected error envelope: %+v", env)
	}
	if f.query != "" {
		t.Errorf("no --force must not send force; got query %q", f.query)
	}
}

// TestDeleteOneReferenceSingular pins the singular wording.
func TestDeleteOneReferenceSingular(t *testing.T) {
	t.Parallel()
	if got := hasReferencesMessage(1); !strings.Contains(got, "linked from 1 topology node.") {
		t.Errorf("unexpected singular message: %q", got)
	}
}

// TestDeleteAmbiguousConflictUsesSharedLadder pins that a 409 that is
// not target_has_references (an ambiguous alias) still renders the
// matching names.
func TestDeleteAmbiguousConflictUsesSharedLadder(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusConflict,
		`{"detail":{"error":"ambiguous_target","query":"vc","matches":[{"name":"vc-a","aliases":[]},{"name":"vc-b","aliases":[]}]}}`)
	_, stderr, err := runDeleteCmd(t, url, "", "vc", "--confirm")
	if exitCodeOf(err) != 4 {
		t.Fatalf("409 should exit 4; got %v", err)
	}
	if !strings.Contains(stderr.String(), "vc-a, vc-b") {
		t.Errorf("stderr should list the matching names; got:\n%s", stderr.String())
	}
}

// TestDeleteNotFound pins that a 404 renders the resolver's near-misses.
func TestDeleteNotFound(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusNotFound,
		`{"detail":{"error":"no_target","query":"prmo","matches":[{"name":"prom","aliases":[]}]}}`)
	_, stderr, err := runDeleteCmd(t, url, "", "prmo", "--confirm")
	if exitCodeOf(err) != 4 {
		t.Fatalf("404 should exit 4; got %v", err)
	}
	if !strings.Contains(stderr.String(), "did you mean: prom") {
		t.Errorf("stderr should list near-misses; got:\n%s", stderr.String())
	}
}

// TestDeleteDeclinedDoesNotCallServer pins the prompt: closed stdin and
// an explicit "n" both decline, exit 0, and send no request; "y" goes
// ahead.
func TestDeleteDeclinedDoesNotCallServer(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusNoContent, "")
	for _, stdin := range []string{"", "n\n"} {
		stdout, _, err := runDeleteCmd(t, url, stdin, "demo-vault")
		if err != nil {
			t.Fatalf("decline (stdin %q) should exit 0; got %v", stdin, err)
		}
		if !strings.Contains(stdout.String(), `declined: target "demo-vault" not deleted`) {
			t.Errorf("stdout should say declined; got:\n%s", stdout.String())
		}
	}
	if f.calls != 0 {
		t.Fatalf("a declined delete must not call the server; got %d calls", f.calls)
	}
	stdout, _, err := runDeleteCmd(t, url, "", "demo-vault", "--json")
	if err != nil {
		t.Fatalf("decline --json: %v", err)
	}
	if !strings.Contains(stdout.String(), `"status": "declined"`) {
		t.Errorf("--json decline should report status declined; got:\n%s", stdout.String())
	}
	if _, _, err := runDeleteCmd(t, url, "y\n", "demo-vault"); err != nil {
		t.Fatalf("answering y should delete: %v", err)
	}
	if f.calls != 1 {
		t.Errorf("answering y should send one request; got %d", f.calls)
	}
}
