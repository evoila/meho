// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func TestCollectionsListRegisteredOnParent(t *testing.T) {
	cmd := newCollectionsCmd()
	var found bool
	for _, c := range cmd.Commands() {
		if c.Name() == "list" {
			found = true
		}
	}
	if !found {
		t.Errorf("expected `list` verb registered on the collections parent")
	}
}

func TestRunCollectionListRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	err := runCollectionList(cmd, listCollectionsOptions{
		Limit: 9000,
	})
	if exitCodeOf(t, err) != 4 {
		t.Errorf("expected exit 4 (unexpected_response) for out-of-range limit; got %d", exitCodeOf(t, err))
	}
}

func TestRunCollectionListHappyPathTable(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet {
				t.Errorf("expected GET; got %s", r.Method)
			}
			docCount := 17000
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode([]api.DocCollectionSummary{
				{
					CollectionKey: "vmware",
					Vendor:        "VMware by Broadcom",
					Products:      []string{"vsphere", "nsx"},
					Status:        "ready",
					DocCount:      &docCount,
				},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runCollectionList(cmd, listCollectionsOptions{
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"KEY", "vmware", "VMware by Broadcom", "vsphere,nsx", "ready", "17000"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunCollectionListEmptyRendersNotice(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode([]api.DocCollectionSummary{})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, _ := newRunCmd(t)
	err := runCollectionList(cmd, listCollectionsOptions{
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionList: %v", err)
	}
	if !strings.Contains(stdout.String(), "no doc collections") {
		t.Errorf("expected empty-list notice; got %q", stdout.String())
	}
}

func TestRunCollectionListJSONOutput(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode([]api.DocCollectionSummary{
				{CollectionKey: "vmware", Vendor: "VMware by Broadcom", Status: "ready"},
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, _ := newRunCmd(t)
	err := runCollectionList(cmd, listCollectionsOptions{
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCollectionList: %v", err)
	}
	var decoded []api.DocCollectionSummary
	readJSONBodyOf(t, stdout.Bytes(), &decoded)
	if len(decoded) != 1 || decoded[0].CollectionKey != "vmware" {
		t.Errorf("unexpected JSON payload: %q", stdout.String())
	}
}

func TestRunCollectionListForwardsVendorAndCursor(t *testing.T) {
	var gotVendor, gotCursor, gotLimit string
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, r *http.Request) {
			gotVendor = r.URL.Query().Get("vendor")
			gotCursor = r.URL.Query().Get("cursor")
			gotLimit = r.URL.Query().Get("limit")
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode([]api.DocCollectionSummary{})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, _ := newRunCmd(t)
	if err := runCollectionList(cmd, listCollectionsOptions{
		Vendor:            "NetApp",
		Cursor:            "vmware",
		Limit:             25,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCollectionList: %v", err)
	}
	if gotVendor != "NetApp" || gotCursor != "vmware" || gotLimit != "25" {
		t.Errorf("query params not forwarded: vendor=%q cursor=%q limit=%q", gotVendor, gotCursor, gotLimit)
	}
}

func TestBuildListCollectionsParamsOmitsZeroValues(t *testing.T) {
	params := buildListCollectionsParams(listCollectionsOptions{})
	if params.Vendor != nil || params.Cursor != nil || params.Limit != nil {
		t.Errorf("expected all params nil for zero options; got %+v", params)
	}
	params = buildListCollectionsParams(listCollectionsOptions{Vendor: "NetApp", Limit: 10, Cursor: "x"})
	if params.Vendor == nil || *params.Vendor != "NetApp" {
		t.Errorf("expected vendor=NetApp; got %+v", params.Vendor)
	}
	if params.Limit == nil || *params.Limit != 10 {
		t.Errorf("expected limit=10; got %+v", params.Limit)
	}
	if params.Cursor == nil || *params.Cursor != "x" {
		t.Errorf("expected cursor=x; got %+v", params.Cursor)
	}
}

func TestRunCollectionListForbiddenRole403(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc(
		"/api/v1/doc_collections",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(map[string]any{"detail": "operator required"})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, _ := newRunCmd(t)
	err := runCollectionList(cmd, listCollectionsOptions{
		BackplaneOverride: srv.URL,
	})
	if exitCodeOf(t, err) != 5 {
		t.Errorf("expected exit 5 (insufficient_role) for a 403; got %d", exitCodeOf(t, err))
	}
}
