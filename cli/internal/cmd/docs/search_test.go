// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// ptrFloat32 / ptrStr are small fixture helpers for the optional
// *float32 / *string fields on api.DocsChunk.
func ptrFloat32(v float32) *float32 { return &v }
func ptrStr(v string) *string       { return &v }

// makeJWT builds an unsigned-looking JWT (header.payload.signature)
// whose payload carries the given capabilities claim. The signature
// segment is a throwaway — the CLI decodes the claim unverified, so
// the fixture never needs a real signature.
func makeJWT(t *testing.T, capabilities []string) string {
	t.Helper()
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none","typ":"JWT"}`))
	payloadJSON, err := json.Marshal(map[string]any{
		"sub":          "operator-1",
		"capabilities": capabilities,
	})
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	payload := base64.RawURLEncoding.EncodeToString(payloadJSON)
	return header + "." + payload + ".sig"
}

// newRunCmd returns a bare cobra.Command wired with capture buffers
// and a bounded context — the harness for driving runSearch directly.
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

// seedXDGAndToken writes a config + token for backplaneURL into a
// temp XDG home with the given access token, and returns the dir.
func seedXDGAndToken(t *testing.T, backplaneURL, accessToken string) string {
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
		AccessToken:  accessToken,
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

func readJSONBodyOf(t *testing.T, raw []byte, into any) {
	t.Helper()
	if err := json.Unmarshal(raw, into); err != nil {
		t.Fatalf("decode body: %v\n%s", err, raw)
	}
}

// exitCodeOf extracts the propagated exit code from an error that
// satisfies output.ExitCoder (the shape RenderError returns).
func exitCodeOf(t *testing.T, err error) int {
	t.Helper()
	if err == nil {
		t.Fatalf("expected an error carrying an exit code; got nil")
	}
	ec, ok := err.(output.ExitCoder)
	if !ok {
		t.Fatalf("error %v does not satisfy ExitCoder", err)
	}
	return ec.ExitCode()
}

// --- capability decode -----------------------------------------------

func TestCapabilitiesFromJWTExtractsClaim(t *testing.T) {
	tok := makeJWT(t, []string{"meho-docs", "other-cap"})
	caps, err := capabilitiesFromJWT(tok)
	if err != nil {
		t.Fatalf("capabilitiesFromJWT: %v", err)
	}
	if _, ok := caps["meho-docs"]; !ok {
		t.Errorf("expected meho-docs in caps; got %v", caps)
	}
	if _, ok := caps["other-cap"]; !ok {
		t.Errorf("expected other-cap in caps; got %v", caps)
	}
}

func TestCapabilitiesFromJWTAbsentClaimIsEmptySet(t *testing.T) {
	// A token with no capabilities claim decodes to the empty set
	// (no error) — fail-closed without erroring the whole gate.
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	payload := base64.RawURLEncoding.EncodeToString([]byte(`{"sub":"x"}`))
	caps, err := capabilitiesFromJWT(header + "." + payload + ".sig")
	if err != nil {
		t.Fatalf("absent claim should not error; got %v", err)
	}
	if len(caps) != 0 {
		t.Errorf("expected empty set; got %v", caps)
	}
}

func TestCapabilitiesFromJWTRejectsNonJWT(t *testing.T) {
	if _, err := capabilitiesFromJWT("not-a-jwt"); err == nil {
		t.Errorf("expected error for non-JWT token")
	}
	if _, err := capabilitiesFromJWT("a.!!!.c"); err == nil {
		t.Errorf("expected error for undecodable payload segment")
	}
}

func TestTenantHasDocsCapabilityProvisioned(t *testing.T) {
	defer stubLoadStoredToken(t, makeJWT(t, []string{"meho-docs"}), nil)()
	defer stubConfig(t, "https://bp.example")()
	if !tenantHasDocsCapability() {
		t.Errorf("expected provisioned tenant to report the capability")
	}
}

func TestTenantHasDocsCapabilityUnprovisioned(t *testing.T) {
	defer stubLoadStoredToken(t, makeJWT(t, []string{"some-other-cap"}), nil)()
	defer stubConfig(t, "https://bp.example")()
	if tenantHasDocsCapability() {
		t.Errorf("expected unprovisioned tenant to lack the capability")
	}
}

// stubLoadStoredToken swaps loadStoredToken for a deterministic value
// and returns a cleanup restoring the production seam.
func stubLoadStoredToken(t *testing.T, accessToken string, err error) func() {
	t.Helper()
	prev := loadStoredToken
	loadStoredToken = func(string) (auth.StoredToken, error) {
		return auth.StoredToken{AccessToken: accessToken}, err
	}
	return func() { loadStoredToken = prev }
}

// stubConfig writes a config.json with the given backplane URL into a
// temp XDG home so auth.LoadConfig resolves a non-empty URL.
func stubConfig(t *testing.T, backplaneURL string) func() {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return func() {}
}

// --- gating shape ----------------------------------------------------

func TestNewRootCmdHiddenWhenUnprovisioned(t *testing.T) {
	cmd := newRootCmdWithGate(false)
	if !cmd.Hidden {
		t.Errorf("expected docs parent Hidden when unprovisioned")
	}
}

func TestNewRootCmdVisibleWhenProvisioned(t *testing.T) {
	cmd := newRootCmdWithGate(true)
	if cmd.Hidden {
		t.Errorf("expected docs parent visible when provisioned")
	}
}

func TestRunSearchRefusesWhenUnprovisioned(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query:       "x",
		Collections: []string{"vmware"},
		Provisioned: false,
	})
	if err == nil {
		t.Fatalf("expected refusal for unprovisioned tenant")
	}
	if got := exitCodeOf(t, err); got != output.ExitInsufficientRole {
		t.Errorf("expected exit %d; got %d", output.ExitInsufficientRole, got)
	}
	if !strings.Contains(stderr.String(), "addon_not_provisioned") {
		t.Errorf("expected addon_not_provisioned code; got %q", stderr.String())
	}
}

func TestRunSearchRefusalIsBeforeNetwork(t *testing.T) {
	// An unprovisioned refusal must short-circuit before any HTTP
	// call. Point at an unroutable URL: if the refusal fired first,
	// no connection is attempted and the error is the typed refusal,
	// not an unreachable.
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query:             "x",
		Collections:       []string{"vmware"},
		Provisioned:       false,
		BackplaneOverride: "http://127.0.0.1:0",
	})
	if err == nil {
		t.Fatalf("expected refusal")
	}
	if !strings.Contains(stderr.String(), "addon_not_provisioned") {
		t.Errorf("expected refusal before network; got %q", stderr.String())
	}
}

// --- flag validation -------------------------------------------------

func TestRunSearchRejectsEmptyQuery(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "", Collections: []string{"vmware"}, Provisioned: true})
	if err == nil {
		t.Fatalf("expected error for empty query")
	}
	if !strings.Contains(stderr.String(), "non-empty <query>") {
		t.Errorf("expected query hint; got %q", stderr.String())
	}
}

func TestRunSearchRejectsMissingCollection(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "x", Provisioned: true})
	if err == nil {
		t.Fatalf("expected error for missing --collection")
	}
	if got := exitCodeOf(t, err); got != output.ExitUnexpected {
		t.Errorf("expected exit %d; got %d", output.ExitUnexpected, got)
	}
	if !strings.Contains(stderr.String(), "--collection") {
		t.Errorf("expected collection hint; got %q", stderr.String())
	}
}

func TestRunSearchRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "x", Collections: []string{"vmware"}, Limit: 51, Provisioned: true})
	if err == nil {
		t.Fatalf("expected error for over-budget limit")
	}
	if !strings.Contains(stderr.String(), "between 1 and 50") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

func TestRunSearchRejectsExplicitZeroLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query: "x", Collections: []string{"vmware"},
		Limit: 0, Changed: true, Provisioned: true,
	})
	if err == nil {
		t.Fatalf("expected error for explicit --limit=0")
	}
	if !strings.Contains(stderr.String(), "between 1 and 50") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// --- happy paths -----------------------------------------------------

func TestRunSearchHappyPath(t *testing.T) {
	var bodyOnWire api.SearchDocsRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{
			Chunks: []api.DocsChunk{
				{
					ChunkId:    "chunk-1",
					Content:    "NSX configuration maximums for vSphere 9.0 are …",
					DocumentId: "nsx-config-maximums-9.0",
					Score:      ptrFloat32(0.95),
					SourceUrl:  ptrStr("https://docs.example/nsx"),
				},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query: "nsx config maximums", Collections: []string{"vmware"}, Product: "nsx", Version: "9.0",
		Provisioned: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runSearch: %v; stderr=%s", err, stderr.String())
	}
	if bodyOnWire.Collection == nil || *bodyOnWire.Collection != "vmware" {
		t.Errorf("expected collection=vmware on wire; got %+v", bodyOnWire)
	}
	if bodyOnWire.Product == nil || *bodyOnWire.Product != "nsx" {
		t.Errorf("expected product=nsx on wire; got %+v", bodyOnWire)
	}
	if bodyOnWire.Version == nil || *bodyOnWire.Version != "9.0" {
		t.Errorf("expected version=9.0 on wire; got %+v", bodyOnWire)
	}
	if bodyOnWire.Query != "nsx config maximums" {
		t.Errorf("expected query in body; got %+v", bodyOnWire)
	}
	if bodyOnWire.Limit != nil {
		t.Errorf("expected limit nil at zero (omitempty); got %+v", *bodyOnWire.Limit)
	}
	for _, want := range []string{"RANK", "SCORE", "DOCUMENT", "nsx-config-maximums-9.0", "0.9500"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

// TestRunSearchOmitsAbsentRefinements proves --product / --version are
// optional refinements: with --collection set but neither refinement
// supplied, the wire body carries collection only (the omitempty pointer
// tags keep product/version absent so the backend treats them as unset).
func TestRunSearchOmitsAbsentRefinements(t *testing.T) {
	var bodyOnWire api.SearchDocsRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{Chunks: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{
		Query: "x", Collections: []string{"vmware"},
		Provisioned: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runSearch: %v; stderr=%s", err, stderr.String())
	}
	if bodyOnWire.Collection == nil || *bodyOnWire.Collection != "vmware" {
		t.Errorf("expected collection=vmware on wire; got %+v", bodyOnWire)
	}
	if bodyOnWire.Product != nil {
		t.Errorf("expected product absent on wire; got %q", *bodyOnWire.Product)
	}
	if bodyOnWire.Version != nil {
		t.Errorf("expected version absent on wire; got %q", *bodyOnWire.Version)
	}
}

func TestRunSearchSendsLimitWhenSet(t *testing.T) {
	var bodyOnWire api.SearchDocsRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{Chunks: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{
		Query: "x", Collections: []string{"vmware"},
		Limit: 25, Provisioned: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	if bodyOnWire.Limit == nil || *bodyOnWire.Limit != 25 {
		t.Errorf("expected limit=25; got %+v", bodyOnWire.Limit)
	}
}

func TestRunSearchZeroHits(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{Chunks: []api.DocsChunk{}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{
		Query: "obscure", Collections: []string{"vmware"},
		Provisioned: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	if !strings.Contains(stdout.String(), "no docs hits") {
		t.Errorf("expected no-hits line; got %q", stdout.String())
	}
}

func TestRunSearchJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{
			Chunks: []api.DocsChunk{
				{
					ChunkId:    "chunk-1",
					Content:    "body",
					DocumentId: "doc-1",
					Score:      ptrFloat32(0.5),
					SourceUrl:  ptrStr("https://docs.example/x"),
				},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{
		Query: "x", Collections: []string{"vmware"},
		JSONOut: true, Provisioned: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	var got api.SearchDocsResponse
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode json output: %v\n%s", err, stdout.String())
	}
	if len(got.Chunks) != 1 || got.Chunks[0].ChunkId != "chunk-1" {
		t.Errorf("expected the raw chunk in json output; got %+v", got)
	}
}

// formatScore renders an absent corpus score as "-" so the table
// doesn't misrepresent it as 0.0000.
func TestRunSearchRendersAbsentScoreAsDash(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{
			Chunks: []api.DocsChunk{
				{ChunkId: "c", Content: "b", DocumentId: "doc-1", Score: nil},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{
		Query: "x", Collections: []string{"vmware"},
		Provisioned: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	line := firstChunkRow(t, stdout.String())
	if !strings.Contains(line, " - ") {
		t.Errorf("expected absent score rendered as '-'; got row %q", line)
	}
}

func firstChunkRow(t *testing.T, table string) string {
	t.Helper()
	for _, l := range strings.Split(table, "\n") {
		if strings.HasPrefix(l, "1 ") || strings.HasPrefix(l, "1    ") {
			return l
		}
	}
	t.Fatalf("no rank-1 row in table %q", table)
	return ""
}

// --- HTTP status mapping ---------------------------------------------

func TestRunSearchMaps403(t *testing.T) {
	// A plain-string 403 detail is a role / entitlement miss → insufficient_role.
	assertStatusMapping(t, http.StatusForbidden,
		`{"detail":"operator role required"}`,
		output.ExitInsufficientRole, "operator role required")
}

func TestRunSearchMaps403DisabledCollection(t *testing.T) {
	// A 403 carrying the structured `collection_disabled` marker is a
	// terminal readiness rejection (an operator hid the collection), not a
	// role/entitlement miss — surfaced as unexpected (#1567), distinct from
	// the insufficient_role path above.
	assertStatusMapping(t, http.StatusForbidden,
		`{"detail":{"error":"collection_disabled","collection":"vmware","retryable":false}}`,
		output.ExitUnexpected, "collection is disabled")
}

func TestRunSearchMaps422(t *testing.T) {
	assertStatusMapping(t, http.StatusUnprocessableEntity,
		`{"detail":"unknown doc collection 'nope'"}`,
		output.ExitUnexpected, "unknown doc collection")
}

func TestRunSearchMaps409NotReady(t *testing.T) {
	// A 409 is the transient (provisioning / rebuilding) not-ready state —
	// retryable, distinct from the terminal disabled 403 above (#1567).
	assertStatusMapping(t, http.StatusConflict,
		`{"detail":"doc collection 'vmware' is not ready (status='rebuilding')"}`,
		output.ExitUnexpected, "not ready")
}

func TestRunSearchMaps503(t *testing.T) {
	assertStatusMapping(t, http.StatusServiceUnavailable,
		`{"detail":"backend unreachable"}`,
		output.ExitUnexpected, "backend is unavailable")
}

func assertStatusMapping(t *testing.T, status int, body string, wantExit int, wantSubstr string) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query: "x", Collections: []string{"vmware"},
		Provisioned: true, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for HTTP %d", status)
	}
	if got := exitCodeOf(t, err); got != wantExit {
		t.Errorf("HTTP %d: expected exit %d; got %d (stderr=%q)", status, wantExit, got, stderr.String())
	}
	if !strings.Contains(stderr.String(), wantSubstr) {
		t.Errorf("HTTP %d: expected stderr to contain %q; got %q", status, wantSubstr, stderr.String())
	}
}

// --- command wiring --------------------------------------------------

func TestSearchCmdRequiresExactlyOneArg(t *testing.T) {
	cmd := newSearchCmd(true)
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{}) // no query
	if err := cmd.Execute(); err == nil {
		t.Errorf("expected error with no <query> arg")
	}
}

// --- cross-collection fan-out (#1554) --------------------------------

// TestBuildSearchBodySingleCollection proves one --collection maps to the
// single `collection` wire field (not the fan-out `collections` list).
func TestBuildSearchBodySingleCollection(t *testing.T) {
	body := buildSearchBody(searchOptions{Query: "q", Collections: []string{"vmware"}})
	if body.Collection == nil || *body.Collection != "vmware" {
		t.Errorf("expected collection=vmware; got %+v", body)
	}
	if body.Collections != nil {
		t.Errorf("expected collections nil on the single path; got %+v", *body.Collections)
	}
}

// TestBuildSearchBodyAllSentinel proves --collection all maps to the
// `collection: "all"` sentinel (not the `collections` list).
func TestBuildSearchBodyAllSentinel(t *testing.T) {
	body := buildSearchBody(searchOptions{Query: "q", Collections: []string{"all"}})
	if body.Collection == nil || *body.Collection != "all" {
		t.Errorf("expected collection=all sentinel; got %+v", body)
	}
	if body.Collections != nil {
		t.Errorf("expected collections nil for the all sentinel; got %+v", *body.Collections)
	}
}

// TestBuildSearchBodyExplicitFanout proves two-or-more --collection values
// map to the `collections` list and that product/version are dropped (a
// fan-out is a merge across pre-scoped corpora).
func TestBuildSearchBodyExplicitFanout(t *testing.T) {
	body := buildSearchBody(searchOptions{
		Query:       "q",
		Collections: []string{"vmware", "netapp"},
		Product:     "nsx",
		Version:     "9.0",
	})
	if body.Collections == nil {
		t.Fatalf("expected collections list on the fan-out path; got nil")
	}
	if got := *body.Collections; len(got) != 2 || got[0] != "vmware" || got[1] != "netapp" {
		t.Errorf("expected [vmware netapp]; got %+v", got)
	}
	if body.Collection != nil {
		t.Errorf("expected single collection nil on the fan-out path; got %q", *body.Collection)
	}
	if body.Product != nil || body.Version != nil {
		t.Errorf("expected product/version dropped on fan-out; got %+v", body)
	}
}

// TestRunSearchRejectsAllMixedWithKeys proves the whole-scope sentinel
// cannot be combined with explicit keys (fail-fast before the round-trip).
func TestRunSearchRejectsAllMixedWithKeys(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query:       "q",
		Collections: []string{"all", "vmware"},
		Provisioned: true,
	})
	if err == nil {
		t.Fatalf("expected error mixing 'all' with explicit keys")
	}
	if !strings.Contains(stderr.String(), "cannot be combined") {
		t.Errorf("expected mix-rejection hint; got %q", stderr.String())
	}
}

// TestRunSearchFanoutRendersProvenanceColumn proves a fan-out result (chunks
// tagged with a source collection) renders the extra COLLECTION column.
func TestRunSearchFanoutRendersProvenanceColumn(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/search_docs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SearchDocsResponse{
			Chunks: []api.DocsChunk{
				{ChunkId: "c1", Content: "vmware hit", DocumentId: "dv1", Collection: ptrStr("vmware")},
				{ChunkId: "c2", Content: "netapp hit", DocumentId: "dn1", Collection: ptrStr("netapp")},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")

	cmd, stdout, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{
		Query:             "q",
		Collections:       []string{"vmware", "netapp"},
		Provisioned:       true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runSearch: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"COLLECTION", "vmware", "netapp"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}
