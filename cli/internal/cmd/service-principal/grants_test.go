// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package serviceprincipal

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

const testGrantID = "11111111-1111-1111-1111-111111111111"

func TestNewGrantsCmdRegistersAllVerbs(t *testing.T) {
	cmd := NewGrantsCmd()
	names := map[string]bool{}
	for _, child := range cmd.Commands() {
		names[child.Name()] = true
	}
	for _, want := range []string{"list", "show", "create", "revoke"} {
		if !names[want] {
			t.Errorf("missing %s", want)
		}
	}
}

func TestBuildGrantCreateBody(t *testing.T) {
	body, err := buildGrantCreateBody("svc:automation", "vmware.vm.power", "vmware-rest-9.0", testGrantID, "", "", "scheduled power-on", "2026-10-01T00:00:00Z")
	if err != nil {
		t.Fatalf("buildGrantCreateBody: %v", err)
	}
	if body.TargetId == nil || body.TargetId.String() != testGrantID || body.ExpiresAt == nil || body.Reason != "scheduled power-on" {
		t.Errorf("unexpected body: %+v", body)
	}
	if !body.ExpiresAt.Equal(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("expiry: %s", body.ExpiresAt)
	}
}

func TestBuildGrantCreateBodyTargetNameSelector(t *testing.T) {
	body, err := buildGrantCreateBody("svc:automation", "vmware.vm.power", "vmware-rest-9.0", "", "", "dc-*", "planned run", "")
	if err != nil {
		t.Fatal(err)
	}
	if body.TargetId != nil || body.TargetNamePattern == nil || *body.TargetNamePattern != "dc-*" {
		t.Errorf("selector body: %+v", body)
	}
}

func TestBuildGrantCreateBodyRejectsUnsafeInput(t *testing.T) {
	cases := []struct{ name, op, connector, target, product, pattern string }{
		{"glob operation", "vmware.*", "vmware-rest-9.0", "", "", ""},
		{"glob connector", "vmware.vm.power", "vmware-*", "", "", ""},
		{"delete operation", "DELETE:/vcenter/vm", "vmware-rest-9.0", "", "", ""},
		{"typed delete operation", "vmware.vm.delete", "vmware-rest-9.0", "", "", ""},
		{"composite operation", "vmware.composite.vm.power", "vmware-rest-9.0", "", "", ""},
		{"selector plus target", "vmware.vm.power", "vmware-rest-9.0", testGrantID, "vmware", "dc-*"},
		{"invalid target", "vmware.vm.power", "vmware-rest-9.0", "not-a-uuid", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := buildGrantCreateBody("svc:automation", tc.op, tc.connector, tc.target, tc.product, tc.pattern, "reason", ""); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestGrantListParams(t *testing.T) {
	params := grantListParams("svc:automation", false, true, 25, 10)
	if params.PrincipalSub == nil || *params.PrincipalSub != "svc:automation" || params.IncludeExpired == nil || *params.IncludeExpired || params.IncludeRevoked == nil || !*params.IncludeRevoked || params.Limit == nil || *params.Limit != 25 || params.Offset == nil || *params.Offset != 10 {
		t.Errorf("params: %+v", params)
	}
}

func TestBuildGrantCreateBodyAllowsLiteralQueryMarker(t *testing.T) {
	body, err := buildGrantCreateBody("svc:automation", "GET:/vcenter/vm?filter=powered_on", "vmware-rest-9.0", "", "", "", "reason", "")
	if err != nil || body.OpId != "GET:/vcenter/vm?filter=powered_on" {
		t.Fatalf("query-style op id rejected: body=%+v err=%v", body, err)
	}
}

func TestPrintGrantListEmpty(t *testing.T) {
	var output bytes.Buffer
	printGrantList(&output, nil)
	if !strings.Contains(output.String(), "no service-principal grants") {
		t.Errorf("output: %q", output.String())
	}
}

func TestRevokeDeclinesWithoutConfirmation(t *testing.T) {
	cmd := NewGrantsCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"revoke", testGrantID, "--json"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("revoke decline: %v stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"status": "declined"`) {
		t.Errorf("output: %s", stdout.String())
	}
}

func seedCredentials(t *testing.T, url string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(url)
	if err := store.Save(service, user, auth.StoredToken{BackplaneURL: url, AccessToken: "test-token", TokenType: "Bearer", Expiry: time.Now().Add(time.Hour)}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(dir, "meho", "config.json"), auth.Config{BackplaneURL: url}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
}

func TestListWiresFiltersAndPaging(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/service-principals/grants" {
			t.Fatalf("route: %s %s", r.Method, r.URL.Path)
		}
		if got := r.URL.Query(); got.Get("principal_sub") != "svc:automation" || got.Get("include_expired") != "false" || got.Get("include_revoked") != "true" || got.Get("limit") != "25" || got.Get("offset") != "10" {
			t.Fatalf("query: %s", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"grants":[]}`))
	}))
	defer srv.Close()
	seedCredentials(t, srv.URL)
	cmd := NewGrantsCmd()
	cmd.SetArgs([]string{"list", "--principal", "svc:automation", "--include-revoked", "--limit", "25", "--offset", "10", "--json"})
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
}

func TestListIncludeExpiredWiresTrue(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("include_expired"); got != "true" {
			t.Fatalf("include_expired: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"grants":[]}`))
	}))
	defer srv.Close()
	seedCredentials(t, srv.URL)
	cmd := NewGrantsCmd()
	cmd.SetArgs([]string{"list", "--include-expired", "--json"})
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
}

func TestCreateWiresTargetSelector(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/service-principals/grants" {
			t.Fatalf("route: %s %s", r.Method, r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["target_product"] != "vmware" || body["target_name_pattern"] != "dc-*" || body["target_id"] != nil {
			t.Fatalf("body: %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"11111111-1111-1111-1111-111111111111","tenant_id":"22222222-2222-2222-2222-222222222222","principal_sub":"svc:automation","op_id":"vmware.vm.power","connector_id":"vmware-rest-9.0","target_id":null,"target_product":"vmware","target_name_pattern":"dc-*","reason":"planned run","created_by_sub":"operator","created_at":"2026-09-17T00:00:00Z","expires_at":null,"revoked_at":null,"revoked_by_sub":null}`))
	}))
	defer srv.Close()
	seedCredentials(t, srv.URL)
	cmd := NewGrantsCmd()
	cmd.SetArgs([]string{"create", "--principal", "svc:automation", "--op-id", "vmware.vm.power", "--connector-id", "vmware-rest-9.0", "--target-product", "vmware", "--target-name-pattern", "dc-*", "--reason", "planned run", "--json"})
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
}
