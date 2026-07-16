// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// TestBuildListParamsOmitsEmptyFilters — the empty options shape sends
// no query string so the backplane sees a clean /api/v1/targets.
func TestBuildListParamsOmitsEmptyFilters(t *testing.T) {
	p := buildListParams(listOptions{})
	if p.Product != nil {
		t.Errorf("empty Product should marshal as nil pointer; got %q", *p.Product)
	}
	if p.Limit != nil {
		t.Errorf("empty Limit should marshal as nil pointer; got %d", *p.Limit)
	}
	if p.Cursor != nil {
		t.Errorf("empty Cursor should marshal as nil pointer; got %q", *p.Cursor)
	}
}

// TestBuildListParamsSetsProduct — --product P populates *Product
// without leaking the value into other fields.
func TestBuildListParamsSetsProduct(t *testing.T) {
	p := buildListParams(listOptions{Product: "vcenter"})
	if p.Product == nil || *p.Product != "vcenter" {
		t.Fatalf("Product: got %+v; want pointer to %q", p.Product, "vcenter")
	}
	if p.Limit != nil || p.Cursor != nil {
		t.Errorf("non-Product fields should stay nil; got Limit=%+v Cursor=%+v", p.Limit, p.Cursor)
	}
}

// TestBuildListParamsSetsAllFilters — every option lands on the
// typed params struct.
func TestBuildListParamsSetsAllFilters(t *testing.T) {
	p := buildListParams(listOptions{Product: "k8s", Limit: 25, Cursor: "rke2-meho"})
	if p.Product == nil || *p.Product != "k8s" {
		t.Errorf("Product: got %+v", p.Product)
	}
	if p.Limit == nil || *p.Limit != 25 {
		t.Errorf("Limit: got %+v", p.Limit)
	}
	if p.Cursor == nil || *p.Cursor != "rke2-meho" {
		t.Errorf("Cursor: got %+v", p.Cursor)
	}
}

// TestPrintTargetsTableEmpty — zero-target tenant renders the
// no-targets line without the header row.
func TestPrintTargetsTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printTargetsTable(&buf, nil)
	out := buf.String()
	if !strings.Contains(out, "no targets registered") {
		t.Errorf("empty render missing no-targets hint; got %q", out)
	}
	if strings.Contains(out, "NAME") {
		t.Errorf("empty render should skip header; got %q", out)
	}
}

// TestPrintTargetsTableRendersColumns — happy-path render with two
// rows: header line + each target's name / aliases / product / host.
func TestPrintTargetsTableRendersColumns(t *testing.T) {
	rows := []api.TargetSummary{
		{Id: mustUUID(t, "11111111-1111-1111-1111-111111111111"), Name: "rdc-vcenter", Aliases: []string{"vc-prod"}, Product: "vcenter", Host: "vc.example"},
		{Id: mustUUID(t, "22222222-2222-2222-2222-222222222222"), Name: "rke2-meho", Aliases: nil, Product: "k8s", Host: "k.example"},
	}
	var buf bytes.Buffer
	printTargetsTable(&buf, rows)
	out := buf.String()
	for _, want := range []string{"NAME", "ALIASES", "PRODUCT", "HOST", "rdc-vcenter", "vc-prod", "vcenter", "vc.example", "rke2-meho", "k8s"} {
		if !strings.Contains(out, want) {
			t.Errorf("printTargetsTable missing %q in %q", want, out)
		}
	}
}

// TestRunListRejectsOutOfRangeLimit — runList must fail fast on a
// --limit outside the API's ge=1, le=500 contract so operators see
// the CLI-side hint rather than a 422 round-trip.
func TestRunListRejectsOutOfRangeLimit(t *testing.T) {
	cmd := &cobra.Command{}
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)
	err := runList(cmd, listOptions{Limit: 501})
	if err == nil {
		t.Fatalf("expected error for over-budget --limit")
	}
	if !strings.Contains(stderr.String(), "between 1 and 500") {
		t.Errorf("stderr missing range hint; got %q", stderr.String())
	}
}

// TestRunListRejectsNegativeLimit — symmetrical case for --limit=-1.
func TestRunListRejectsNegativeLimit(t *testing.T) {
	cmd := &cobra.Command{}
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)
	err := runList(cmd, listOptions{Limit: -1})
	if err == nil {
		t.Fatalf("expected error for negative --limit")
	}
}

// TestRunListHappyPath drives runList end-to-end through the auth
// + transport stack against an httptest server. Asserts the typed
// `product` query param round-trips on the wire.
func TestRunListHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("product"); got != "vcenter" {
			t.Errorf("query product: got %q; want %q", got, "vcenter")
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.TargetListResponse{Items: []api.TargetSummary{
			{Id: mustUUID(t, "11111111-1111-1111-1111-111111111111"), Name: "rdc-vcenter", Aliases: []string{"vc-prod"}, Product: "vcenter", Host: "vc.example"},
		}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{Product: "vcenter", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"NAME", "rdc-vcenter", "vcenter", "vc.example"} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in %q", want, out)
		}
	}
}

// TestRunListJSONHappyPath — --json round-trips the raw response
// shape; operators piping through jq get a stable contract.
func TestRunListJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.TargetListResponse{Items: []api.TargetSummary{
			{Id: mustUUID(t, "11111111-1111-1111-1111-111111111111"), Name: "rdc-vcenter", Aliases: []string{"vc-prod"}, Product: "vcenter", Host: "vc.example"},
		}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runList --json: %v; stderr=%s", err, stderr.String())
	}
	// Parse stdout as JSON to confirm the operator sees a structured
	// envelope, not the table.
	var decoded []api.TargetSummary
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded) != 1 || decoded[0].Name != "rdc-vcenter" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestRunList401SurfacesAuthExpired — exhausting the refresh budget
// (no refresh_token present) must render as auth_expired with the
// `meho login` hint.
func TestRunList401SurfacesAuthExpired(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"detail":"token expired"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL) // stored token has no refresh_token

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error; stdout=%s", stdout.String())
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired in stderr; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint in stderr; got %q", stderr.String())
	}
}

// TestRunList403SurfacesInsufficientRole — RBAC denial renders with
// the required-role string the backend supplied.
func TestRunList403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: operator required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role in stderr; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "operator required") {
		t.Errorf("expected backend role hint passed through; got %q", stderr.String())
	}
	// Check exit code lands at 5.
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// ----- shared helpers below -----

// seedXDGAndToken redirects XDG_CONFIG_HOME / MEHO_KEYRING_DISABLE
// and seeds a token + config for the supplied backplane URL.
// Mirrors withTempXDG + seedCreds from cmd/status_test.go; kept
// independent because the cmd package can't be imported here.
func seedXDGAndToken(t *testing.T, backplaneURL string) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return dir
}

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers
// attached. The runXxx helpers consume cmd.OutOrStdout /
// cmd.ErrOrStderr; tests inspect the buffers afterward.
func newRunCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

// mustUUID parses a UUID string in a test; fatal on failure. Used to
// build `api.TargetSummary` / `api.Target` fixtures whose Id /
// TenantId fields are `openapi_types.UUID` (a type alias for
// `uuid.UUID`).
func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	u, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("uuid.Parse(%q): %v", s, err)
	}
	return u
}
