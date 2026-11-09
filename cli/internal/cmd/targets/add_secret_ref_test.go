// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// createdLinuxTargetJSON is a created Target whose secret_ref MEHO built
// from the target name. The placeholder <derived> is replaced per test.
const createdLinuxTargetJSON = `{
  "id": "11111111-1111-1111-1111-111111111111",
  "tenant_id": "22222222-2222-2222-2222-222222222222",
  "name": "linux-01",
  "aliases": [],
  "product": "linux",
  "host": "linux-01.example.test",
  "port": null,
  "fqdn": null,
  "auth_model": "shared_service_account",
  "vpn_required": false,
  "secret_ref": "tenants/22222222-2222-2222-2222-222222222222/linux-01",
  "secret_ref_derived": <derived>,
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

func serveCreatedLinuxTarget(t *testing.T, derived string) *httptest.Server {
	t.Helper()
	body := strings.Replace(createdLinuxTargetJSON, "<derived>", derived, 1)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(body))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestAddShowsThatSecretRefWasBuiltFromTheName(t *testing.T) {
	srv := serveCreatedLinuxTarget(t, "true")
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runAddCmd(t, srv.URL,
		"linux-01", "--product", "linux", "--host", "linux-01.example.test")
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr.String())
	}
	out := stdout.String()
	if !strings.Contains(out, "tenants/22222222-2222-2222-2222-222222222222/linux-01 (built from the target name") {
		t.Errorf("stdout should name the stored secret_ref and that MEHO built it:\n%s", out)
	}
}

func TestAddShowsASetSecretRefWithoutTheNote(t *testing.T) {
	srv := serveCreatedLinuxTarget(t, "false")
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runAddCmd(t, srv.URL,
		"linux-01", "--product", "linux", "--host", "linux-01.example.test",
		"--secret-ref", "tenants/22222222-2222-2222-2222-222222222222/linux-01")
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr.String())
	}
	out := stdout.String()
	if !strings.Contains(out, "tenants/22222222-2222-2222-2222-222222222222/linux-01") {
		t.Errorf("stdout should name the stored secret_ref:\n%s", out)
	}
	if strings.Contains(out, "built from the target name") {
		t.Errorf("a set secret_ref must not carry the note:\n%s", out)
	}
}

func TestAddJSONCarriesSecretRefDerived(t *testing.T) {
	srv := serveCreatedLinuxTarget(t, "true")
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runAddCmd(t, srv.URL,
		"linux-01", "--product", "linux", "--host", "linux-01.example.test", "--json")
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"secret_ref_derived": true`) {
		t.Errorf("--json output should carry secret_ref_derived:\n%s", stdout.String())
	}
}
