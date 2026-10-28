// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package serviceprincipal

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const testPrincipal = "svc:deploy-bot"

func TestNewReadScopeCmdRegistersAllVerbs(t *testing.T) {
	cmd := NewReadScopeCmd()
	names := map[string]bool{}
	for _, child := range cmd.Commands() {
		names[child.Name()] = true
	}
	for _, want := range []string{"set", "show", "clear"} {
		if !names[want] {
			t.Errorf("missing %s", want)
		}
	}
}

func TestBuildReadScopeBody(t *testing.T) {
	body, err := buildReadScopeBody([]string{testGrantID}, []string{"product=vmware,name=dc-*", "name=esx-*"}, true)
	if err != nil {
		t.Fatalf("buildReadScopeBody: %v", err)
	}
	if len(body.Entries) != 4 {
		t.Fatalf("entries: %+v", body.Entries)
	}
	if body.Entries[0].TargetId == nil || body.Entries[0].TargetId.String() != testGrantID {
		t.Errorf("target entry: %+v", body.Entries[0])
	}
	if body.Entries[1].TargetProduct == nil || *body.Entries[1].TargetProduct != "vmware" || body.Entries[1].TargetNamePattern == nil || *body.Entries[1].TargetNamePattern != "dc-*" {
		t.Errorf("selector entry: %+v", body.Entries[1])
	}
	if body.Entries[2].TargetProduct != nil || body.Entries[2].TargetNamePattern == nil || *body.Entries[2].TargetNamePattern != "esx-*" {
		t.Errorf("name selector entry: %+v", body.Entries[2])
	}
	if body.Entries[3].NoTarget == nil || !*body.Entries[3].NoTarget {
		t.Errorf("no-target entry: %+v", body.Entries[3])
	}
}

func TestBuildReadScopeBodyRejectsBadInput(t *testing.T) {
	cases := []struct {
		name      string
		targets   []string
		selectors []string
		noTarget  bool
		wantMsg   string
	}{
		{"empty", nil, nil, false, "at least one of"},
		{"bad uuid", []string{"not-a-uuid"}, nil, false, "not a valid UUID"},
		{"unknown key", nil, []string{"vendor=vmware"}, false, "unknown key"},
		{"no value", nil, []string{"product="}, false, "expected product="},
		{"product twice", nil, []string{"product=a,product=b"}, false, "given twice"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := buildReadScopeBody(tc.targets, tc.selectors, tc.noTarget)
			if err == nil {
				t.Fatal("expected validation error")
			}
			if !strings.Contains(err.Error(), tc.wantMsg) {
				t.Fatalf("error %q does not say %q", err.Error(), tc.wantMsg)
			}
		})
	}
}

func TestSetWiresBodyAndPath(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut || r.URL.Path != "/api/v1/service-principals/read-scopes/"+testPrincipal {
			t.Fatalf("route: %s %s", r.Method, r.URL.Path)
		}
		var body struct {
			Entries []map[string]any `json:"entries"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if len(body.Entries) != 2 || body.Entries[0]["target_id"] != testGrantID || body.Entries[1]["no_target"] != true {
			t.Fatalf("body: %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"tenant_id":"22222222-2222-2222-2222-222222222222","principal_sub":"svc:deploy-bot","entries":[{"target_id":"11111111-1111-1111-1111-111111111111"},{"no_target":true}],"set_by_sub":"operator","set_at":"2026-10-27T00:00:00Z"}`))
	}))
	defer srv.Close()
	seedCredentials(t, srv.URL)
	cmd := NewReadScopeCmd()
	cmd.SetArgs([]string{"set", testPrincipal, "--target", testGrantID, "--no-target"})
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout.String(), "target "+testGrantID) || !strings.Contains(stdout.String(), "calls without a target") {
		t.Errorf("output: %s", stdout.String())
	}
}

func TestShowReportsNoScopeOn404(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/service-principals/read-scopes/"+testPrincipal {
			t.Fatalf("route: %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"read_scope_not_found"}`))
	}))
	defer srv.Close()
	seedCredentials(t, srv.URL)
	cmd := NewReadScopeCmd()
	cmd.SetArgs([]string{"show", testPrincipal, "--json"})
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout.String(), `"status": "not_set"`) {
		t.Errorf("output: %s", stdout.String())
	}
}

func TestClearDeclinesWithoutConfirmation(t *testing.T) {
	cmd := NewReadScopeCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"clear", testPrincipal, "--json"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("clear decline: %v stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"status": "declined"`) {
		t.Errorf("output: %s", stdout.String())
	}
}

func TestClearWiresDelete(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/api/v1/service-principals/read-scopes/"+testPrincipal {
			t.Fatalf("route: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	seedCredentials(t, srv.URL)
	cmd := NewReadScopeCmd()
	cmd.SetArgs([]string{"clear", testPrincipal, "--confirm", "--json"})
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout.String(), `"status": "cleared"`) {
		t.Errorf("output: %s", stdout.String())
	}
}

func TestDescribeEntry(t *testing.T) {
	product, name := "vmware", "dc-*"
	if got := describeEntry(api.ReadScopeEntry{TargetProduct: &product, TargetNamePattern: &name}); got != "selector product=vmware,name=dc-*" {
		t.Errorf("describeEntry: %q", got)
	}
}
