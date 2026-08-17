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

	"github.com/spf13/cobra"
)

// createdTargetJSON is a complete Target row as POST /api/v1/targets
// returns it on 201 (response_model=Target). It must decode cleanly
// into api.Target — UUIDs, RFC3339 timestamps, and the nullable
// columns are all present so printTargetSummary renders every line.
const createdTargetJSON = `{
  "id": "11111111-1111-1111-1111-111111111111",
  "tenant_id": "22222222-2222-2222-2222-222222222222",
  "name": "rdc-vault",
  "aliases": [],
  "product": "vault",
  "host": "vault.evba.lab",
  "port": null,
  "fqdn": null,
  "auth_model": "shared_service_account",
  "vpn_required": false,
  "secret_ref": "tenants/22222222-2222-2222-2222-222222222222/rdc-vault",
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
  "updated_at": "2026-08-17T12:00:00Z"
}`

// runAddCmd drives a fresh `meho targets add` command end-to-end
// through cobra (flag parsing, required-flag + ExactArgs validation,
// RunE) with stdout/stderr buffers attached. When backplaneURL is set
// it is appended as --backplane so the verb talks to the caller's
// httptest server. Mirrors the newRunCmd helper but exercises the real
// command wiring rather than a bare cobra.Command.
func runAddCmd(t *testing.T, backplaneURL string, args ...string) (*bytes.Buffer, *bytes.Buffer, error) {
	t.Helper()
	cmd := newAddCmd()
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

// TestNewAddCmdRegisteredWithCreateAlias pins the acceptance criterion
// that newAddCmd() is grafted onto `meho targets` and carries the
// documented `create` alias.
func TestNewAddCmdRegisteredWithCreateAlias(t *testing.T) {
	t.Parallel()
	root := NewRootCmd()
	var add *cobra.Command
	for _, c := range root.Commands() {
		if c.Name() == "add" {
			add = c
			break
		}
	}
	if add == nil {
		t.Fatalf("`meho targets add` not registered on the root command")
	}
	found := false
	for _, a := range add.Aliases {
		if a == "create" {
			found = true
		}
	}
	if !found {
		t.Errorf("expected `create` alias on add; got %v", add.Aliases)
	}
}

// TestAddHelpListsFlags asserts `meho targets add --help` surfaces the
// full flag set mirroring TargetCreate, so operators discover the verb
// from its help alone.
func TestAddHelpListsFlags(t *testing.T) {
	t.Parallel()
	cmd := newAddCmd()
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	cmd.SetErr(&buf)
	cmd.SetArgs([]string{"--help"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("`add --help`: %v", err)
	}
	help := buf.String()
	for _, want := range []string{
		"--product", "--host", "--alias", "--version", "--port", "--fqdn",
		"--secret-ref", "--auth-model", "--verify-tls", "--tls-ca-pin",
		"--tls-server-name", "--preferred-impl", "--note",
	} {
		if !strings.Contains(help, want) {
			t.Errorf("`add --help` missing flag %q; got:\n%s", want, help)
		}
	}
}

// TestAddMissingRequiredFlags pins that --product and --host are
// required: cobra rejects the invocation before RunE (no network), and
// the error names the missing flag.
func TestAddMissingRequiredFlags(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"missing product", []string{"rdc-vault", "--host", "vault.evba.lab"}, "product"},
		{"missing host", []string{"rdc-vault", "--product", "vault"}, "host"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			cmd := newAddCmd()
			var buf bytes.Buffer
			cmd.SetOut(&buf)
			cmd.SetErr(&buf)
			cmd.SetArgs(c.args)
			err := cmd.Execute()
			if err == nil {
				t.Fatalf("expected error when %s", c.name)
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Errorf("error %q should name the missing flag %q", err.Error(), c.want)
			}
		})
	}
}

// TestAddSuccessRendersTargetAndOmitsTenantID is the happy path: a 201
// with the created Target body renders the same key-value summary as
// describe, the POST body carries the required fields, never carries
// tenant_id (server binds it from the JWT), and omits every optional
// the operator did not set.
func TestAddSuccessRendersTargetAndOmitsTenantID(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var gotBody map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("add issued %s; want POST", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(raw, &gotBody); err != nil {
			t.Errorf("request body is not JSON: %v (%s)", err, raw)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(createdTargetJSON))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runAddCmd(t, srv.URL,
		"rdc-vault", "--product", "vault", "--host", "vault.evba.lab")
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr.String())
	}

	// Required fields present, tenant_id absent.
	if gotBody["name"] != "rdc-vault" || gotBody["product"] != "vault" || gotBody["host"] != "vault.evba.lab" {
		t.Errorf("request body missing required fields: %v", gotBody)
	}
	if _, leaked := gotBody["tenant_id"]; leaked {
		t.Errorf("request body must never carry tenant_id: %v", gotBody)
	}
	// Unset optionals are omitted, not sent as JSON null — the
	// omit-vs-null distinction is load-bearing for secret_ref (an
	// omitted ref derives the per-tenant default; a null persists "no
	// credential").
	for _, k := range []string{"secret_ref", "aliases", "version", "port", "verify_tls", "notes"} {
		if _, present := gotBody[k]; present {
			t.Errorf("unset optional %q should be omitted from the body: %v", k, gotBody)
		}
	}

	out := stdout.String()
	for _, want := range []string{"rdc-vault", "vault", "vault.evba.lab", "shared_service_account"} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout should render created-target field %q:\n%s", want, out)
		}
	}
}

// TestAddSetOptionalsMapToSnakeCaseFields asserts every optional flag
// the operator does set lands in the body under its snake_case
// TargetCreate key with the right type (repeatable --alias → JSON
// array, --port → number, --verify-tls=false → bool false).
func TestAddSetOptionalsMapToSnakeCaseFields(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var gotBody map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(createdTargetJSON))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	_, stderr, err := runAddCmd(t, srv.URL,
		"rdc-vault", "--product", "vault", "--host", "vault.evba.lab",
		"--alias", "vault-a", "--alias", "vault-b",
		"--version", "1.x", "--port", "8200", "--fqdn", "vault.corp",
		"--secret-ref", "tenants/22222222-2222-2222-2222-222222222222/rdc-vault",
		"--auth-model", "shared_service_account", "--verify-tls=false",
		"--tls-server-name", "vault.san", "--preferred-impl", "vault-1.x",
		"--note", "primary lab vault")
	if err != nil {
		t.Fatalf("add with optionals: %v\nstderr=%s", err, stderr.String())
	}

	if aliases, ok := gotBody["aliases"].([]any); !ok || len(aliases) != 2 ||
		aliases[0] != "vault-a" || aliases[1] != "vault-b" {
		t.Errorf("aliases should be the two repeated values; got %v", gotBody["aliases"])
	}
	// JSON numbers decode to float64.
	if p, ok := gotBody["port"].(float64); !ok || p != 8200 {
		t.Errorf("port should be 8200; got %v", gotBody["port"])
	}
	if v, ok := gotBody["verify_tls"].(bool); !ok || v {
		t.Errorf("verify_tls should be false; got %v", gotBody["verify_tls"])
	}
	for k, want := range map[string]any{
		"version":           "1.x",
		"fqdn":              "vault.corp",
		"secret_ref":        "tenants/22222222-2222-2222-2222-222222222222/rdc-vault",
		"auth_model":        "shared_service_account",
		"tls_server_name":   "vault.san",
		"preferred_impl_id": "vault-1.x",
		"notes":             "primary lab vault",
	} {
		if gotBody[k] != want {
			t.Errorf("body[%q]: got %v; want %v", k, gotBody[k], want)
		}
	}
	// tenant_id is still never present, even on the fully-populated path.
	if _, leaked := gotBody["tenant_id"]; leaked {
		t.Errorf("request body must never carry tenant_id: %v", gotBody)
	}
}

// TestAddServer422SurfacesBodyVerbatim pins that a server rejection
// (unknown product, out-of-tenant secret_ref, SSRF-guarded host) is
// surfaced to the operator with the response body intact and the
// unexpected_response exit class (4) — the CLI runs no client-side
// validation of its own.
func TestAddServer422SurfacesBodyVerbatim(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	const detail = "unknown product 'nope'; known: vault, vcenter"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"` + detail + `"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	_, stderr, err := runAddCmd(t, srv.URL,
		"rdc-vault", "--product", "nope", "--host", "vault.evba.lab")
	if err == nil {
		t.Fatalf("expected a non-nil error on 422")
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("422 should classify as unexpected_response (exit 4); got %v", err)
	}
	if !strings.Contains(stderr.String(), detail) {
		t.Errorf("stderr should surface the 422 body verbatim; got:\n%s", stderr.String())
	}
}
