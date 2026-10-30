// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// runUpdateCmd drives a fresh `meho targets update` command through
// cobra, appending --backplane when backplaneURL is set.
func runUpdateCmd(t *testing.T, backplaneURL string, args ...string) (*bytes.Buffer, *bytes.Buffer, error) {
	t.Helper()
	cmd := newUpdateCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
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

// updatedTargetJSON is a complete Target row as PATCH /api/v1/targets/{name}
// returns it on 200. The names are made up; nothing in it points at a
// real deployment.
const updatedTargetJSON = `{
  "id": "11111111-1111-1111-1111-111111111111",
  "tenant_id": "22222222-2222-2222-2222-222222222222",
  "name": "demo-vault",
  "aliases": [],
  "product": "vault",
  "host": "vault.example.com",
  "port": 8200,
  "fqdn": null,
  "auth_model": "shared_service_account",
  "vpn_required": false,
  "secret_ref": null,
  "preferred_impl_id": null,
  "fingerprint": null,
  "notes": null,
  "extras": {},
  "tls_ca_pin": null,
  "tls_server_name": null,
  "verify_tls": true,
  "version": null,
  "deleted_at": null,
  "created_at": "2026-08-17T12:00:00Z",
  "updated_at": "2026-08-17T12:05:00Z"
}`

// fakePatchServer answers PATCH /api/v1/targets/{name} with the reply
// it is started with and records the method, path and decoded body.
type fakePatchServer struct {
	method string
	path   string
	body   map[string]any
	raw    string
}

func (f *fakePatchServer) start(t *testing.T, status int, reply string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.method = r.Method
		f.path = r.URL.EscapedPath()
		raw, _ := io.ReadAll(r.Body)
		f.raw = string(raw)
		_ = json.Unmarshal(raw, &f.body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(reply))
	}))
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv.URL
}

// TestNewUpdateCmdRegistered pins that `update` hangs off `meho targets`.
func TestNewUpdateCmdRegistered(t *testing.T) {
	t.Parallel()
	for _, c := range NewRootCmd().Commands() {
		if c.Name() == "update" {
			return
		}
	}
	t.Fatalf("`meho targets update` not registered on the root command")
}

// TestUpdateSendsOnlySetFieldsAsPatch pins the sparse PATCH: the
// request goes to /api/v1/targets/{name}, carries only the flags the
// operator set (snake_case keys), and the result renders like describe.
func TestUpdateSendsOnlySetFieldsAsPatch(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakePatchServer
	url := f.start(t, http.StatusOK, updatedTargetJSON)
	stdout, stderr, err := runUpdateCmd(t, url, "demo-vault",
		"--port", "8200", "--verify-tls=false", "--preferred-impl", "vault-1.x",
		"--note", "moved", "--alias", "a1")
	if err != nil {
		t.Fatalf("update: %v\nstderr=%s", err, stderr.String())
	}
	if f.method != http.MethodPatch || f.path != "/api/v1/targets/demo-vault" {
		t.Errorf("want PATCH /api/v1/targets/demo-vault; got %s %s", f.method, f.path)
	}
	want := map[string]any{
		"port": float64(8200), "verify_tls": false,
		"preferred_impl_id": "vault-1.x", "notes": "moved",
	}
	for k, v := range want {
		if f.body[k] != v {
			t.Errorf("body[%q]: got %#v; want %#v", k, f.body[k], v)
		}
	}
	if a, ok := f.body["aliases"].([]any); !ok || len(a) != 1 || a[0] != "a1" {
		t.Errorf("aliases: got %#v", f.body["aliases"])
	}
	if len(f.body) != len(want)+1 {
		t.Errorf("body should carry only the set fields; got %v", f.body)
	}
	if !strings.Contains(stdout.String(), "demo-vault") {
		t.Errorf("stdout should render the updated target:\n%s", stdout.String())
	}
}

// TestUpdateClearSendsExplicitNull pins the headline case: drop the
// credential and switch to plain HTTP in one call. secret_ref must be
// a present JSON null, and --extra replaces extras.
func TestUpdateClearSendsExplicitNull(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakePatchServer
	url := f.start(t, http.StatusOK, updatedTargetJSON)
	_, stderr, err := runUpdateCmd(t, url, "prom",
		"--clear", "secret-ref", "--clear", "note", "--extra", "scheme=http")
	if err != nil {
		t.Fatalf("update --clear: %v\nstderr=%s", err, stderr.String())
	}
	for _, k := range []string{"secret_ref", "notes"} {
		v, present := f.body[k]
		if !present || v != nil {
			t.Errorf("%s must be a present JSON null; raw body %s", k, f.raw)
		}
	}
	extras, ok := f.body["extras"].(map[string]any)
	if !ok || extras["scheme"] != "http" {
		t.Errorf("extras: got %#v", f.body["extras"])
	}
}

// TestUpdateClearCollectionsSendEmptyNotNull pins that `--clear alias`
// sends `"aliases": []` and `--clear extra` sends `"extras": {}`. Both
// columns are NOT NULL on the server, so null would be rejected; the
// empty list / map is the only way to remove every entry.
func TestUpdateClearCollectionsSendEmptyNotNull(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakePatchServer
	url := f.start(t, http.StatusOK, updatedTargetJSON)
	_, stderr, err := runUpdateCmd(t, url, "demo-vault", "--clear", "alias", "--clear", "extra")
	if err != nil {
		t.Fatalf("update --clear alias --clear extra: %v\nstderr=%s", err, stderr.String())
	}
	if a, ok := f.body["aliases"].([]any); !ok || len(a) != 0 {
		t.Errorf("aliases must be an empty list, not null; raw body %s", f.raw)
	}
	if e, ok := f.body["extras"].(map[string]any); !ok || len(e) != 0 {
		t.Errorf("extras must be an empty map, not null; raw body %s", f.raw)
	}
	if len(f.body) != 2 {
		t.Errorf("body should carry only aliases and extras; got %s", f.raw)
	}
}

// TestUpdateLocalErrors pins the checks that run before any request:
// no fields, an unknown --clear field, and set-and-clear of one field.
func TestUpdateLocalErrors(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"no fields", []string{"prom"}, "nothing to update"},
		{"unknown clear", []string{"prom", "--clear", "host"}, `invalid --clear "host"`},
		{"set and clear", []string{"prom", "--note", "x", "--clear", "note"}, "cannot be used together"},
		{"alias and clear alias", []string{"prom", "--alias", "a1", "--clear", "alias"}, "cannot be used together"},
		{"extra and clear extra", []string{"prom", "--extra", "k=v", "--clear", "extra"}, "cannot be used together"},
		{"bad extra", []string{"prom", "--extra", "=x"}, "expected key=value"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, stderr, err := runUpdateCmd(t, "", c.args...)
			type ec interface{ ExitCode() int }
			if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
				t.Fatalf("want exit 4; got %v", err)
			}
			if !strings.Contains(stderr.String(), c.want) {
				t.Errorf("stderr should contain %q; got:\n%s", c.want, stderr.String())
			}
		})
	}
}

// TestUpdateNotFoundRendersNearMisses pins that a 404 from the
// resolver goes through renderHTTPStatus (near-miss names, exit 4).
func TestUpdateNotFoundRendersNearMisses(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakePatchServer
	url := f.start(t, http.StatusNotFound,
		`{"detail":{"error":"no_target","query":"prmo","matches":[{"name":"prom","aliases":[]}]}}`)
	_, stderr, err := runUpdateCmd(t, url, "prmo", "--note", "x")
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Fatalf("404 should exit 4; got %v", err)
	}
	if !strings.Contains(stderr.String(), "did you mean: prom") {
		t.Errorf("stderr should list near-misses; got:\n%s", stderr.String())
	}
}

// TestUpdateVPNRequiredSendsOnlyWhenGiven pins that --vpn-required=false
// sends an explicit false (to clear the flag), --vpn-required sends
// true, and an update without the flag leaves vpn_required out of the
// body so the stored value stays as it is.
func TestUpdateVPNRequiredSendsOnlyWhenGiven(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	cases := []struct {
		name    string
		args    []string
		want    any
		present bool
	}{
		{"set", []string{"--vpn-required"}, true, true},
		{"clear", []string{"--vpn-required=false"}, false, true},
		{"omitted", []string{"--note", "moved"}, nil, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var f fakePatchServer
			url := f.start(t, http.StatusOK, updatedTargetJSON)
			args := append([]string{"demo-vault"}, tc.args...)
			_, stderr, err := runUpdateCmd(t, url, args...)
			if err != nil {
				t.Fatalf("update: %v\nstderr=%s", err, stderr.String())
			}
			v, present := f.body["vpn_required"]
			if present != tc.present || (present && v != tc.want) {
				t.Errorf("vpn_required: got %#v (present=%t); want %#v (present=%t); raw body %s",
					v, present, tc.want, tc.present, f.raw)
			}
		})
	}
}
