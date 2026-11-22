// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func outsideFolderServer(t *testing.T, list api.OutsideTenantFolderList, gotTenant *string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/checks/outside-tenant-folder", func(w http.ResponseWriter, r *http.Request) {
		if gotTenant != nil {
			*gotTenant = r.URL.Query().Get("tenant_id")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(list)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestRunOutsideFolderListsTargets(t *testing.T) {
	list := api.OutsideTenantFolderList{Items: []api.OutsideTenantFolderItem{
		{Name: "vcenter-lab", SecretRef: "targets/vcenter-lab", Reason: "the path is outside the caller's tenant"},
	}}
	srv := outsideFolderServer(t, list, nil)
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runOutsideFolder(cmd, outsideFolderOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runOutsideFolder: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"NAME", "SECRET_REF", "vcenter-lab", "targets/vcenter-lab", "outside"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunOutsideFolderSaysTheCheckIsOff(t *testing.T) {
	msg := "The folder check is off on this install"
	srv := outsideFolderServer(t, api.OutsideTenantFolderList{CheckOff: true, Message: &msg, Items: []api.OutsideTenantFolderItem{}}, nil)
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runOutsideFolder(cmd, outsideFolderOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runOutsideFolder: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), msg) {
		t.Errorf("stdout missing the check-off message: %q", stdout.String())
	}
}

func TestRunOutsideFolderSendsTenantAndEmitsJSON(t *testing.T) {
	var gotTenant string
	srv := outsideFolderServer(t, api.OutsideTenantFolderList{AllTenants: false, Items: []api.OutsideTenantFolderItem{}}, &gotTenant)
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	tenant := "00000000-0000-0000-0000-00000000b0b0"
	err := runOutsideFolder(cmd, outsideFolderOptions{Tenant: tenant, JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runOutsideFolder: %v; stderr=%s", err, stderr.String())
	}
	if gotTenant != tenant {
		t.Errorf("tenant_id query: got %q; want %q", gotTenant, tenant)
	}
	var decoded api.OutsideTenantFolderList
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout is not JSON: %v", err)
	}
}

func TestRunOutsideFolderRejectsBadTenant(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)
	if err := runOutsideFolder(cmd, outsideFolderOptions{Tenant: "nope"}); err == nil {
		t.Fatalf("expected an error for a tenant that is not a UUID")
	}
	if !strings.Contains(stderr.String(), "UUID") {
		t.Errorf("stderr missing hint: %q", stderr.String())
	}
}
