// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/output"
)

const tenantA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

// settingsExport is what GET /api/v1/settings/export sends: PyYAML's
// quoting, the key order of TARGET_KEYS, and a target with no version.
const settingsExport = `apiVersion: meho.evoila.com/v1
kind: Settings
metadata:
  exportedAt: '2026-11-06T12:00:00Z'
spec:
  tenants:
  - id: ` + tenantA + `
    policies:
      announce_gate_enabled: false
    conventions: []
    targets:
    - name: vc-01
      product: vsphere
      version: '8.0'
      host: 10.1.0.1
      port: null
      fqdn: null
      aliases: []
      auth_model: shared_service_account
      secret_field_map: {}
      verify_tls: true
      tls_ca_pin: null
      tls_server_name: null
      vpn_required: false
      preferred_impl_id: null
      flight_recorder_capture: null
      notes: null
      extras:
        sso_realm: corp
    - name: bare
      product: vsphere
      version: null
      host: 10.1.0.2
      secret_ref: null
      extras: {}
`

const twoTenants = `apiVersion: meho.evoila.com/v1
kind: Settings
spec:
  tenants:
  - id: ` + tenantA + `
    targets: []
  - id: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
    targets: []
`

func exportServer(t *testing.T, body string, seenTenant *string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/settings/export", func(w http.ResponseWriter, r *http.Request) {
		if seenTenant != nil {
			*seenTenant = r.URL.Query().Get("tenant_id")
		}
		_, _ = io.WriteString(w, body)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func runExport(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	cmd := NewRootCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs(append([]string{"export"}, args...))
	err := cmd.Execute()
	return stdout.String(), stderr.String(), err
}

func decodeYAML(t *testing.T, text string) map[string]any {
	t.Helper()
	var out map[string]any
	if err := yaml.Unmarshal([]byte(text), &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestExportEntriesEqualTheSettingsFile(t *testing.T) {
	srv := exportServer(t, settingsExport, nil)
	seedXDGAndToken(t, srv.URL)
	stdout, _, err := runExport(t)
	if err != nil {
		t.Fatal(err)
	}
	spec := decodeYAML(t, settingsExport)["spec"].(map[string]any)
	want := spec["tenants"].([]any)[0].(map[string]any)["targets"]
	if got := decodeYAML(t, stdout)["targets"]; !reflect.DeepEqual(got, want) {
		t.Errorf("entries:\n%v\nwant:\n%v", got, want)
	}
	// Key order and quoting survive: `version: 8.0` would read back as a number.
	if !strings.Contains(stdout, `version: "8.0"`) && !strings.Contains(stdout, `version: '8.0'`) {
		t.Errorf("version is not quoted:\n%s", stdout)
	}
	if strings.Index(stdout, "name: vc-01") > strings.Index(stdout, "product: vsphere") {
		t.Errorf("key order changed:\n%s", stdout)
	}
	if strings.Contains(stdout, "spec:") || strings.Contains(stdout, "policies") {
		t.Errorf("more than the targets:\n%s", stdout)
	}
}

// The round-trip acceptance criterion: an export reads back through
// `meho targets import`, `version` stays a column, and `extras: {}` and
// `version: null` do not break it (#3649).
func TestExportThenImportRoundTrips(t *testing.T) {
	srv := exportServer(t, settingsExport, nil)
	seedXDGAndToken(t, srv.URL)
	stdout, _, err := runExport(t)
	if err != nil {
		t.Fatal(err)
	}

	entries, err := parseTargetsYAML([]byte(stdout))
	if err != nil {
		t.Fatalf("parseTargetsYAML: %v", err)
	}
	created, warnings := entryToCreateBody(entries[0])
	if len(warnings) != 0 {
		t.Errorf("warnings: %v", warnings)
	}
	if created["version"] != "8.0" {
		t.Errorf("version = %v", created["version"])
	}
	if extras := created["extras"].(map[string]any); extras["sso_realm"] != "corp" || len(extras) != 1 {
		t.Errorf("extras = %v", extras)
	}

	bare, _ := entryToUpdateBody(entries[1])
	if extras, ok := bare["extras"].(map[string]any); !ok || len(extras) != 0 {
		t.Errorf("an explicit empty extras must reach the PATCH: %v", bare)
	}
	if v, ok := bare["version"]; !ok || v != nil {
		t.Errorf("version: null must reach the PATCH: %v", bare)
	}
	if v, ok := bare["secret_ref"]; !ok || v != nil {
		t.Errorf("secret_ref: null must stay explicit: %v", bare)
	}

	raw, err := json.Marshal(created)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"version":"8.0"`) {
		t.Errorf("body: %s", raw)
	}
}

func TestExportWritesOutFile(t *testing.T) {
	srv := exportServer(t, settingsExport, nil)
	seedXDGAndToken(t, srv.URL)
	path := filepath.Join(t.TempDir(), "targets.yaml")
	stdout, _, err := runExport(t, "--out", path)
	if err != nil {
		t.Fatal(err)
	}
	if stdout != "" {
		t.Errorf("stdout = %q", stdout)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parseTargetsYAML(data); err != nil {
		t.Errorf("parse: %v", err)
	}
}

func TestExportOfSeveralTenantsAsksForOne(t *testing.T) {
	var seen string
	srv := exportServer(t, twoTenants, &seen)
	seedXDGAndToken(t, srv.URL)
	_, stderr, err := runExport(t)
	var coder output.ExitCoder
	if !errors.As(err, &coder) || coder.ExitCode() != output.ExitUsage {
		t.Fatalf("err = %v", err)
	}
	if !strings.Contains(stderr, "--tenant") || !strings.Contains(stderr, tenantA) {
		t.Errorf("stderr: %s", stderr)
	}

	srv2 := exportServer(t, settingsExport, &seen)
	seedXDGAndToken(t, srv2.URL)
	if _, _, err := runExport(t, "--tenant", tenantA); err != nil {
		t.Fatal(err)
	}
	if seen != tenantA {
		t.Errorf("tenant_id = %q", seen)
	}
}

func TestExportRefusesATenantThatIsNoUUID(t *testing.T) {
	srv := exportServer(t, settingsExport, nil)
	seedXDGAndToken(t, srv.URL)
	_, stderr, err := runExport(t, "--tenant", "acme")
	var coder output.ExitCoder
	if !errors.As(err, &coder) || coder.ExitCode() != output.ExitUsage {
		t.Fatalf("err = %v, stderr %s", err, stderr)
	}
}

func TestNewRootCmdListsExport(t *testing.T) {
	var found *cobra.Command
	for _, sub := range NewRootCmd().Commands() {
		if sub.Name() == "export" {
			found = sub
		}
	}
	if found == nil {
		t.Fatal("no export verb")
	}
}
