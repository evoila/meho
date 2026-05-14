// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ---- YAML parsing unit tests -----------------------------------------------

func TestParseTargetsYAML_HappyPath(t *testing.T) {
	yaml := []byte(`
targets:
  - name: rdc-vcenter
    aliases: [rdc, host-vcenter]
    product: vcenter
    host: vc-dc.evba.lab
    port: 443
    secret_ref: secret/rdc/vsphere
    sso_realm: evba.lab
    vpn_required: true
    notes: "Host vCenter"
`)
	entries, err := parseTargetsYAML(yaml)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("expected 1 entry, got %d", len(entries))
	}
	e := entries[0]
	if e.Name != "rdc-vcenter" {
		t.Errorf("name: want rdc-vcenter, got %q", e.Name)
	}
	if e.Product != "vcenter" {
		t.Errorf("product: want vcenter, got %q", e.Product)
	}
	if e.Host != "vc-dc.evba.lab" {
		t.Errorf("host: want vc-dc.evba.lab, got %q", e.Host)
	}
	if e.Port == nil || *e.Port != 443 {
		t.Errorf("port: want 443, got %v", e.Port)
	}
	if e.SecretRef == nil || *e.SecretRef != "secret/rdc/vsphere" {
		t.Errorf("secret_ref: want secret/rdc/vsphere, got %v", e.SecretRef)
	}
	if !e.VPNRequired {
		t.Error("vpn_required: want true")
	}
	if e.Notes == nil || *e.Notes != "Host vCenter" {
		t.Errorf("notes: want 'Host vCenter', got %v", e.Notes)
	}
	if len(e.Aliases) != 2 {
		t.Errorf("aliases: want 2, got %d", len(e.Aliases))
	}
	// sso_realm spills into extras
	if e.Extras["sso_realm"] != "evba.lab" {
		t.Errorf("extras[sso_realm]: want evba.lab, got %v", e.Extras["sso_realm"])
	}
	// known fields must NOT appear in extras
	if _, ok := e.Extras["name"]; ok {
		t.Error("name must not appear in extras")
	}
}

func TestParseTargetsYAML_DefaultAuthModel(t *testing.T) {
	yaml := []byte(`
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
`)
	entries, err := parseTargetsYAML(yaml)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if entries[0].AuthModel != "shared_service_account" {
		t.Errorf("auth_model default: want shared_service_account, got %q", entries[0].AuthModel)
	}
}

func TestParseTargetsYAML_ExplicitAuthModel(t *testing.T) {
	yaml := []byte(`
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
    auth_model: impersonation
`)
	entries, err := parseTargetsYAML(yaml)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if entries[0].AuthModel != "impersonation" {
		t.Errorf("auth_model: want impersonation, got %q", entries[0].AuthModel)
	}
}

func TestParseTargetsYAML_MissingName(t *testing.T) {
	yaml := []byte(`
targets:
  - product: rke2
    host: 10.0.0.1
`)
	_, err := parseTargetsYAML(yaml)
	if err == nil {
		t.Fatal("expected error for missing name")
	}
	if !strings.Contains(err.Error(), "name") {
		t.Errorf("error should mention 'name', got: %v", err)
	}
}

func TestParseTargetsYAML_MissingProduct(t *testing.T) {
	yaml := []byte(`
targets:
  - name: alpha
    host: 10.0.0.1
`)
	_, err := parseTargetsYAML(yaml)
	if err == nil {
		t.Fatal("expected error for missing product")
	}
	if !strings.Contains(err.Error(), "product") {
		t.Errorf("error should mention 'product', got: %v", err)
	}
}

func TestParseTargetsYAML_MissingHost(t *testing.T) {
	yaml := []byte(`
targets:
  - name: alpha
    product: rke2
`)
	_, err := parseTargetsYAML(yaml)
	if err == nil {
		t.Fatal("expected error for missing host")
	}
	if !strings.Contains(err.Error(), "host") {
		t.Errorf("error should mention 'host', got: %v", err)
	}
}

func TestParseTargetsYAML_MalformedYAML(t *testing.T) {
	_, err := parseTargetsYAML([]byte(":\t:bad yaml:::"))
	if err == nil {
		t.Fatal("expected error for malformed YAML")
	}
}

func TestParseTargetsYAML_UnknownFieldsGoToExtras(t *testing.T) {
	yaml := []byte(`
targets:
  - name: rke2-prod
    product: kubernetes
    host: 10.5.50.1
    kubeconfig_field: kubeconfig
    account: my-sa@project.iam.gserviceaccount.com
    project_id: gcp-prod-123
`)
	entries, err := parseTargetsYAML(yaml)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	e := entries[0]
	if e.Extras["kubeconfig_field"] != "kubeconfig" {
		t.Errorf("extras[kubeconfig_field]: got %v", e.Extras["kubeconfig_field"])
	}
	if e.Extras["account"] != "my-sa@project.iam.gserviceaccount.com" {
		t.Errorf("extras[account]: got %v", e.Extras["account"])
	}
	if e.Extras["project_id"] != "gcp-prod-123" {
		t.Errorf("extras[project_id]: got %v", e.Extras["project_id"])
	}
}

func TestParseTargetsYAML_EmptyAliases(t *testing.T) {
	yaml := []byte(`
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
`)
	entries, err := parseTargetsYAML(yaml)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if entries[0].Aliases == nil {
		t.Error("aliases should be empty slice, not nil")
	}
	if len(entries[0].Aliases) != 0 {
		t.Errorf("aliases: want 0, got %d", len(entries[0].Aliases))
	}
}

func TestParseTargetsYAML_EmptyFile(t *testing.T) {
	entries, err := parseTargetsYAML([]byte("targets: []"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(entries) != 0 {
		t.Errorf("expected 0 entries, got %d", len(entries))
	}
}

// ---- HTTP integration tests (fake server) ----------------------------------

func fakeImportServer(t *testing.T, existing []map[string]any) string {
	t.Helper()
	mux := http.NewServeMux()

	// GET /api/v1/targets — returns existing list
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "application/json")
			b, _ := json.Marshal(existing)
			_, _ = w.Write(b)
			return
		}
		if r.Method == http.MethodPost {
			// Accept any POST as a 201 Created
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			// Echo back a minimal target from the request body
			b, _ := json.Marshal(map[string]any{
				"id": "new-id", "tenant_id": "ttt",
				"name": "created", "aliases": []string{},
				"product": "rke2", "host": "10.0.0.1",
				"auth_model": "shared_service_account",
				"vpn_required": false, "extras": map[string]any{},
				"created_at": "2026-01-01T00:00:00Z",
				"updated_at": "2026-01-01T00:00:00Z",
			})
			_, _ = w.Write(b)
			return
		}
		w.WriteHeader(http.StatusMethodNotAllowed)
	})

	// PATCH /api/v1/targets/{name} — accept update
	mux.HandleFunc("/api/v1/targets/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPatch {
			w.Header().Set("Content-Type", "application/json")
			b, _ := json.Marshal(map[string]any{
				"id": "existing-id", "tenant_id": "ttt",
				"name": "patched", "aliases": []string{},
				"product": "rke2", "host": "10.0.0.1",
				"auth_model": "shared_service_account",
				"vpn_required": false, "extras": map[string]any{},
				"created_at": "2026-01-01T00:00:00Z",
				"updated_at": "2026-01-01T00:00:00Z",
			})
			_, _ = w.Write(b)
			return
		}
		w.WriteHeader(http.StatusMethodNotAllowed)
	})

	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv.URL
}

func writeTempYAML(t *testing.T, content string) string {
	t.Helper()
	f := filepath.Join(t.TempDir(), "targets.yaml")
	if err := os.WriteFile(f, []byte(content), 0o600); err != nil {
		t.Fatalf("write temp yaml: %v", err)
	}
	return f
}

func TestImport_DryRun_NoWrites(t *testing.T) {
	xdg := withTempXDG(t)
	// empty existing targets
	url := fakeImportServer(t, nil)
	seedCreds(t, xdg, url)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
  - name: beta
    product: vault
    host: 10.0.0.2
`)
	stdout, _, err := runCobraCmd(t, newImportCmd(), f, "--dry-run")
	if err != nil {
		t.Fatalf("dry-run returned error: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, "CREATE") {
		t.Errorf("expected CREATE in dry-run output, got:\n%s", out)
	}
	if !strings.Contains(out, "alpha") {
		t.Errorf("expected alpha in dry-run output, got:\n%s", out)
	}
}

func TestImport_DryRun_JSON(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeImportServer(t, nil)
	seedCreds(t, xdg, url)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
`)
	stdout, _, err := runCobraCmd(t, newImportCmd(), f, "--dry-run", "--json")
	if err != nil {
		t.Fatalf("dry-run --json returned error: %v", err)
	}
	var plan map[string]any
	if jerr := json.Unmarshal([]byte(strings.TrimSpace(stdout.String())), &plan); jerr != nil {
		t.Fatalf("not valid JSON: %v\n%s", jerr, stdout)
	}
	creates, _ := plan["create"].([]any)
	if len(creates) != 1 {
		t.Errorf("expected 1 create, got %v", plan["create"])
	}
	if creates[0] != "alpha" {
		t.Errorf("expected alpha in create, got %v", creates[0])
	}
	// skip array must be present (even if empty)
	if _, ok := plan["skip"]; !ok {
		t.Error("json plan must include 'skip' key")
	}
}

func TestImport_HappyPath_AllNew(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeImportServer(t, nil)
	seedCreds(t, xdg, url)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
`)
	stdout, _, err := runCobraCmd(t, newImportCmd(), f)
	if err != nil {
		t.Fatalf("import returned error: %v", err)
	}
	if !strings.Contains(stdout.String(), "created") {
		t.Errorf("expected 'created' in output, got:\n%s", stdout)
	}
}

func TestImport_Conflict_AbortsByDefault(t *testing.T) {
	xdg := withTempXDG(t)
	existing := []map[string]any{
		{"id": "aaa", "name": "alpha", "aliases": []string{}, "product": "rke2", "host": "10.0.0.1"},
	}
	url := fakeImportServer(t, existing)
	seedCreds(t, xdg, url)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
`)
	_, stderr, err := runCobraCmd(t, newImportCmd(), f)
	if err == nil {
		t.Fatal("expected error when target already exists without --update")
	}
	if !strings.Contains(stderr.String(), "alpha") {
		t.Errorf("expected alpha in conflict error, got:\n%s", stderr)
	}
	if !strings.Contains(strings.ToLower(stderr.String()), "--update") {
		t.Errorf("expected --update hint in stderr, got:\n%s", stderr)
	}
}

func TestImport_Update_PatchesExisting(t *testing.T) {
	xdg := withTempXDG(t)
	existing := []map[string]any{
		{"id": "aaa", "name": "alpha", "aliases": []string{}, "product": "rke2", "host": "10.0.0.1"},
	}
	url := fakeImportServer(t, existing)
	seedCreds(t, xdg, url)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
    notes: "updated notes"
`)
	stdout, _, err := runCobraCmd(t, newImportCmd(), f, "--update")
	if err != nil {
		t.Fatalf("import --update returned error: %v", err)
	}
	if !strings.Contains(stdout.String(), "updated") {
		t.Errorf("expected 'updated' in output, got:\n%s", stdout)
	}
}

func TestImport_Update_PatchBodyIsSparse(t *testing.T) {
	// Verify that PATCH body only contains fields explicitly set in the YAML.
	// Fields absent from YAML (vpn_required, auth_model, aliases) must not be
	// sent so the backend's exclude_unset logic leaves them untouched.
	xdg := withTempXDG(t)
	var patchBody map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		b, _ := json.Marshal([]map[string]any{
			{"id": "aaa", "name": "alpha", "aliases": []string{}, "product": "rke2", "host": "10.0.0.1"},
		})
		_, _ = w.Write(b)
	})
	mux.HandleFunc("/api/v1/targets/alpha", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPatch {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&patchBody); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		b, _ := json.Marshal(map[string]any{
			"id": "aaa", "tenant_id": "ttt",
			"name": "alpha", "aliases": []string{},
			"product": "rke2", "host": "10.0.0.1",
			"auth_model": "shared_service_account",
			"vpn_required": false, "extras": map[string]any{},
			"created_at": "2026-01-01T00:00:00Z",
			"updated_at": "2026-01-01T00:00:00Z",
		})
		_, _ = w.Write(b)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedCreds(t, xdg, srv.URL)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
    notes: "only this field"
`)
	_, _, err := runCobraCmd(t, newImportCmd(), f, "--update")
	if err != nil {
		t.Fatalf("import --update returned error: %v", err)
	}
	if _, ok := patchBody["vpn_required"]; ok {
		t.Errorf("PATCH body must not include vpn_required when absent from YAML, got: %v", patchBody)
	}
	if _, ok := patchBody["auth_model"]; ok {
		t.Errorf("PATCH body must not include auth_model when absent from YAML, got: %v", patchBody)
	}
	if _, ok := patchBody["aliases"]; ok {
		t.Errorf("PATCH body must not include aliases when absent from YAML, got: %v", patchBody)
	}
	if patchBody["notes"] != "only this field" {
		t.Errorf("PATCH body should include notes='only this field', got: %v", patchBody)
	}
	if patchBody["host"] != "10.0.0.1" {
		t.Errorf("PATCH body should always include host, got: %v", patchBody)
	}
}

func TestImport_NoCreds(t *testing.T) {
	_ = withTempXDG(t)
	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
`)
	_, stderr, err := runCobraCmd(t, newImportCmd(), f)
	if err == nil {
		t.Fatal("expected error for no-creds path")
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint, got: %q", stderr)
	}
}

func TestImport_FileNotFound(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeImportServer(t, nil)
	seedCreds(t, xdg, url)

	_, _, err := runCobraCmd(t, newImportCmd(), "/no/such/file.yaml")
	if err == nil {
		t.Fatal("expected error for missing file")
	}
}

func TestImport_DryRun_ShowsUpdateForExisting(t *testing.T) {
	xdg := withTempXDG(t)
	existing := []map[string]any{
		{"id": "aaa", "name": "alpha", "aliases": []string{}, "product": "rke2", "host": "10.0.0.1"},
	}
	url := fakeImportServer(t, existing)
	seedCreds(t, xdg, url)

	f := writeTempYAML(t, `
targets:
  - name: alpha
    product: rke2
    host: 10.0.0.1
  - name: beta
    product: vault
    host: 10.0.0.2
`)
	stdout, _, err := runCobraCmd(t, newImportCmd(), f, "--dry-run", "--update")
	if err != nil {
		t.Fatalf("dry-run --update returned error: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, "UPDATE") {
		t.Errorf("expected UPDATE for existing target, got:\n%s", out)
	}
	if !strings.Contains(out, "CREATE") {
		t.Errorf("expected CREATE for new target, got:\n%s", out)
	}
}
