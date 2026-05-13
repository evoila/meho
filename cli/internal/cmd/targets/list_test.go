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
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

func fakeListServer(t *testing.T, body []byte, status int) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(body)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv.URL
}

func twoTargetsBody() []byte {
	targets := []map[string]any{
		{"id": "aaa", "name": "alpha", "aliases": []string{"a"}, "product": "rke2", "host": "10.0.0.1"},
		{"id": "bbb", "name": "beta", "aliases": []string{}, "product": "vcenter", "host": "10.0.0.2"},
	}
	b, _ := json.Marshal(targets)
	return b
}

func TestList_HumanHappyPath(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeListServer(t, twoTargetsBody(), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, stderr, err := runCobraCmd(t, newListCmd())
	if err != nil {
		t.Fatalf("list returned error: %v\nstderr:\n%s", err, stderr)
	}
	out := stdout.String()
	for _, want := range []string{"alpha", "beta", "rke2", "vcenter", "10.0.0.1"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in output:\n%s", want, out)
		}
	}
	if strings.Contains(out, jwtMarker) {
		t.Errorf("JWT marker leaked into stdout:\n%s", out)
	}
}

func TestList_JSONOutput(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeListServer(t, twoTargetsBody(), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, _, err := runCobraCmd(t, newListCmd(), "--json")
	if err != nil {
		t.Fatalf("list --json returned error: %v", err)
	}
	var decoded []map[string]any
	if jerr := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &decoded); jerr != nil {
		t.Fatalf("--json output is not valid JSON: %v\n%s", jerr, stdout)
	}
	if len(decoded) != 2 {
		t.Errorf("expected 2 targets, got %d", len(decoded))
	}
}

func TestList_ProductFilter_SentAsQueryParam(t *testing.T) {
	xdg := withTempXDG(t)
	var gotProduct string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		gotProduct = r.URL.Query().Get("product")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("[]"))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedCreds(t, xdg, srv.URL)

	_, _, err := runCobraCmd(t, newListCmd(), "--product", "vcenter")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if gotProduct != "vcenter" {
		t.Errorf("expected product=vcenter in query, got %q", gotProduct)
	}
}

func TestList_EmptyTenant(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeListServer(t, []byte("[]"), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, _, err := runCobraCmd(t, newListCmd())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout.String(), "No targets") {
		t.Errorf("expected empty-list message, got:\n%s", stdout)
	}
}

func TestList_NoCreds(t *testing.T) {
	_ = withTempXDG(t) // no seeded creds

	_, stderr, err := runCobraCmd(t, newListCmd())
	if err == nil {
		t.Fatal("expected error for no-creds path")
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint, got: %q", stderr)
	}
}

func TestList_BackplaneOverride(t *testing.T) {
	xdg := withTempXDG(t)
	live := fakeListServer(t, []byte("[]"), http.StatusOK)
	deadURL := "http://192.0.2.1:1"
	seedCreds(t, xdg, deadURL)
	// Also seed creds for the live server.
	store, _ := auth.NewFileStore()
	svc, user := auth.KeyForBackplane(live)
	_ = store.Save(svc, user, auth.StoredToken{
		BackplaneURL: live,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})

	_, _, err := runCobraCmd(t, newListCmd(), "--backplane", live)
	if err != nil {
		t.Fatalf("override path returned error: %v", err)
	}
}
