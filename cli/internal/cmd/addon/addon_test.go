// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package addon

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// ---------- shared test substrate (same shape as cmd/agent-principal) ----------

type mockHandler = http.HandlerFunc

func mockBackplane(t *testing.T, routes map[string]mockHandler) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			h(w, r)
			return
		}
		t.Errorf("mockBackplane: unhandled route %s", key)
		w.WriteHeader(http.StatusNotFound)
	}))
}

func writeJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Errorf("writeJSON marshal: %v", err)
		w.WriteHeader(http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write(raw); err != nil {
		t.Errorf("writeJSON write: %v", err)
	}
}

func writeDetail(status int, detail string) mockHandler {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"detail":"` + detail + `"}`))
	}
}

const (
	stubID       = "11111111-1111-1111-1111-111111111111"
	stubTenantID = "22222222-2222-2222-2222-222222222222"
	// stubSecret is a made-up secret. It must appear exactly once in the
	// pair output and nowhere else.
	stubSecret = "made-up-secret-7f3a"
)

func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("mustUUID(%q): %v", s, err)
	}
	return id
}

func newPairingRead(t *testing.T, name string, lastSeen *time.Time) api.PairedAddonRead {
	t.Helper()
	pairedAt := time.Date(2026, 10, 24, 9, 30, 0, 0, time.UTC)
	return api.PairedAddonRead{
		Id:                       mustUUID(t, stubID),
		TenantId:                 mustUUID(t, stubTenantID),
		Name:                     name,
		KeycloakClientId:         "addon:" + name,
		OwnerSub:                 "owner-sub",
		ContractVersion:          1,
		AddonContractVersion:     2,
		AddonMinBackplaneVersion: 1,
		CreatedBySub:             "creator-sub",
		PairedAt:                 pairedAt,
		LastSeenAt:               lastSeen,
		UpdatedAt:                pairedAt,
	}
}

func newPairResult(t *testing.T, name string) api.PairAddonResult {
	t.Helper()
	return api.PairAddonResult{
		Pairing:                   newPairingRead(t, name, nil),
		ClientId:                  "addon:" + name,
		ClientSecret:              stubSecret,
		BackplaneContractVersion:  1,
		NegotiatedContractVersion: 1,
	}
}

func newCapturingCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{Use: "x"}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetContext(context.Background())
	return cmd, stdout, stderr
}

// primeConfig points the CLI's config + token store at a temp dir and
// stores a bearer for srv. Returns the config dir so a test can check
// that a verb wrote no file there.
func primeConfig(t *testing.T, srv *httptest.Server) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfgDir := filepath.Join(dir, "meho")
	if err := os.MkdirAll(cfgDir, 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": srv.URL})
	if err := os.WriteFile(filepath.Join(cfgDir, "config.json"), cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: srv.URL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	return dir
}

func runVerbWithMock(
	t *testing.T,
	srv *httptest.Server,
	runner func(cmd *cobra.Command) error,
) (*bytes.Buffer, *bytes.Buffer, error) {
	t.Helper()
	primeConfig(t, srv)
	cmd, stdout, stderr := newCapturingCmd(t)
	return stdout, stderr, runner(cmd)
}

// snapshotFiles reads every file under root into a path -> content map.
func snapshotFiles(t *testing.T, root string) map[string]string {
	t.Helper()
	files := map[string]string{}
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		raw, rerr := os.ReadFile(path)
		if rerr != nil {
			return rerr
		}
		files[path] = string(raw)
		return nil
	})
	if err != nil {
		t.Fatalf("snapshotFiles: %v", err)
	}
	return files
}

func assertRenderedErrorCode(t *testing.T, stderr *bytes.Buffer, exitErr error, wantCode, wantDetailSubstr string) {
	t.Helper()
	if exitErr == nil {
		t.Fatalf("expected RunE to return non-nil error; got nil. stderr=%q", stderr.String())
	}
	var envelope map[string]interface{}
	if err := json.NewDecoder(stderr).Decode(&envelope); err != nil {
		t.Fatalf("decode error envelope %q: %v", stderr.String(), err)
	}
	gotCode, _ := envelope["error"].(string)
	if gotCode != wantCode {
		t.Errorf("error: got %q want %q (envelope=%+v)", gotCode, wantCode, envelope)
	}
	detail, _ := envelope["detail"].(string)
	if wantDetailSubstr != "" && !strings.Contains(detail, wantDetailSubstr) {
		t.Errorf("detail %q missing substring %q (envelope=%+v)", detail, wantDetailSubstr, envelope)
	}
}

func readJSONBody[T any](t *testing.T, r *http.Request) T {
	t.Helper()
	var v T
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	return v
}

func pairOpts(srv *httptest.Server, jsonOut bool) pairOptions {
	return pairOptions{
		Name:                "automation",
		ContractVersion:     2,
		MinBackplaneVersion: 1,
		JSONOut:             jsonOut,
		BackplaneOverride:   srv.URL,
	}
}

// ---------- helper tests ----------

func TestDecodeDetailStringPullsDetailField(t *testing.T) {
	cases := []struct{ in, want string }{
		{`{"detail":"addon_not_paired"}`, "addon_not_paired"},
		{`not-json`, "not-json"},
		{`{"unrelated":"x"}`, `{"unrelated":"x"}`},
		{``, ``},
	}
	for _, tc := range cases {
		if got := decodeDetailString(tc.in); got != tc.want {
			t.Errorf("decodeDetailString(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
}

func TestRootCmdHasFourVerbs(t *testing.T) {
	root := NewRootCmd()
	got := map[string]bool{}
	for _, c := range root.Commands() {
		got[c.Name()] = true
	}
	for _, want := range []string{"pair", "list", "show", "unpair"} {
		if !got[want] {
			t.Errorf("meho addon is missing the %q verb; got %v", want, got)
		}
	}
	if root.Name() != "addon" {
		t.Errorf("root name: got %q want %q", root.Name(), "addon")
	}
}

// ---------- pair ----------

// TestPairSendsTypedBodyAndPrintsSecretOnce: the wire body is the generated
// PairAddonRequest, owner_sub is null when --owner-sub is unset, and the
// text output carries the client id, the secret exactly once, and the
// one-time note. Nothing goes to stderr, and no file is written or
// changed under the config dir.
func TestPairSendsTypedBodyAndPrintsSecretOnce(t *testing.T) {
	var seen api.PairAddonRequest
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/addons/pairings": func(w http.ResponseWriter, r *http.Request) {
			seen = readJSONBody[api.PairAddonRequest](t, r)
			writeJSON(t, w, http.StatusCreated, newPairResult(t, seen.Name))
		},
	})
	defer srv.Close()

	cfgRoot := primeConfig(t, srv)
	before := snapshotFiles(t, cfgRoot)
	cmd, stdout, stderr := newCapturingCmd(t)
	if err := runPair(cmd, pairOpts(srv, false)); err != nil {
		t.Fatalf("runPair: %v", err)
	}

	if seen.Name != "automation" || seen.AddonContractVersion != 2 || seen.AddonMinBackplaneVersion != 1 {
		t.Errorf("body: got %+v", seen)
	}
	if seen.OwnerSub != nil {
		t.Errorf("unset --owner-sub must send owner_sub null; got %q", *seen.OwnerSub)
	}

	out := stdout.String()
	for _, want := range []string{
		`paired add-on "automation"`,
		"client_id:                    addon:automation",
		"negotiated_contract_version:  1",
		"client_secret:                " + stubSecret,
		secretNote,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in:\n%s", want, out)
		}
	}
	if n := strings.Count(out, stubSecret); n != 1 {
		t.Errorf("secret must appear exactly once on stdout; got %d", n)
	}
	if stderr.Len() != 0 {
		t.Errorf("stderr must be empty on success; got %q", stderr.String())
	}
	assertNoSecretInFiles(t, before, snapshotFiles(t, cfgRoot))
}

func assertNoSecretInFiles(t *testing.T, before, after map[string]string) {
	t.Helper()
	if len(before) != len(after) {
		t.Errorf("file set changed: before=%d after=%d", len(before), len(after))
	}
	for path, content := range after {
		if _, ok := before[path]; !ok {
			t.Errorf("new file written: %s", path)
		}
		if strings.Contains(content, stubSecret) {
			t.Errorf("secret written to file %s", path)
		}
	}
}

func TestPairSendsOwnerSubWhenSet(t *testing.T) {
	var seen api.PairAddonRequest
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/addons/pairings": func(w http.ResponseWriter, r *http.Request) {
			seen = readJSONBody[api.PairAddonRequest](t, r)
			writeJSON(t, w, http.StatusCreated, newPairResult(t, seen.Name))
		},
	})
	defer srv.Close()

	opts := pairOpts(srv, true)
	opts.OwnerSub = "owner-x"
	_, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runPair(cmd, opts)
	})
	if err != nil {
		t.Fatalf("runPair: %v", err)
	}
	if seen.OwnerSub == nil || *seen.OwnerSub != "owner-x" {
		t.Errorf("body OwnerSub: got %+v want pointer to %q", seen.OwnerSub, "owner-x")
	}
}

// TestPairJSONPrintsWholeAnswer: --json prints the server's answer with
// client_secret, exactly once, to stdout only.
func TestPairJSONPrintsWholeAnswer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/addons/pairings": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusCreated, newPairResult(t, "automation"))
		},
	})
	defer srv.Close()

	cfgRoot := primeConfig(t, srv)
	before := snapshotFiles(t, cfgRoot)
	cmd, stdout, stderr := newCapturingCmd(t)
	if err := runPair(cmd, pairOpts(srv, true)); err != nil {
		t.Fatalf("runPair: %v", err)
	}
	var got api.PairAddonResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout is not PairAddonResult JSON: %v\n%s", err, stdout.String())
	}
	if got.ClientSecret != stubSecret || got.ClientId != "addon:automation" {
		t.Errorf("decoded result: %+v", got)
	}
	if n := strings.Count(stdout.String(), stubSecret); n != 1 {
		t.Errorf("secret must appear exactly once on stdout; got %d", n)
	}
	if stderr.Len() != 0 {
		t.Errorf("stderr must be empty on success; got %q", stderr.String())
	}
	assertNoSecretInFiles(t, before, snapshotFiles(t, cfgRoot))
}

// TestPairNeverEchoesSecretIntoErrors: an answer that is 2xx but not 201
// might carry credentials. The error message must not contain the body.
func TestPairNeverEchoesSecretIntoErrors(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/addons/pairings": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newPairResult(t, "automation"))
		},
	})
	defer srv.Close()

	for _, jsonOut := range []bool{false, true} {
		stdout, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
			return runPair(cmd, pairOpts(srv, jsonOut))
		})
		if err == nil {
			t.Fatalf("json=%v: a 200 answer must be an error", jsonOut)
		}
		if strings.Contains(stderr.String(), stubSecret) {
			t.Errorf("json=%v: secret leaked to stderr: %s", jsonOut, stderr.String())
		}
		if stdout.Len() != 0 {
			t.Errorf("json=%v: stdout must be empty on error; got %q", jsonOut, stdout.String())
		}
		if !strings.Contains(stderr.String(), "HTTP 200\"") && !strings.Contains(stderr.String(), "HTTP 200\n") {
			t.Errorf("json=%v: error should name the status; got %q", jsonOut, stderr.String())
		}
	}
}

// TestPairRequiresVersionFlags drives the cobra command itself, so the
// check runs before any network call and prints a usage_error.
func TestPairRequiresVersionFlags(t *testing.T) {
	cases := []struct {
		args []string
		want string
	}{
		{[]string{"pair", "automation", "--json"}, "--contract-version is required"},
		{[]string{"pair", "automation", "--contract-version", "1", "--json"}, "--min-backplane-version is required"},
		{[]string{"pair", "automation", "--contract-version", "0", "--min-backplane-version", "1", "--json"},
			"--contract-version must be 1 or higher"},
		{[]string{"pair", "automation", "--contract-version", "1", "--min-backplane-version", "-3", "--json"},
			"--min-backplane-version must be 1 or higher"},
	}
	for _, tc := range cases {
		root := NewRootCmd()
		stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
		root.SetOut(stdout)
		root.SetErr(stderr)
		root.SetArgs(tc.args)
		err := root.Execute()
		assertRenderedErrorCode(t, stderr, err, output.ErrCodeUsage, tc.want)
		if stdout.Len() != 0 {
			t.Errorf("%v: stdout must be empty; got %q", tc.args, stdout.String())
		}
	}
}

func TestPairRequiresVersionFlagsTextOutput(t *testing.T) {
	root := NewRootCmd()
	stderr := &bytes.Buffer{}
	root.SetOut(&bytes.Buffer{})
	root.SetErr(stderr)
	root.SetArgs([]string{"pair", "automation"})
	if err := root.Execute(); err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(stderr.String(), "meho: usage_error: --contract-version is required") {
		t.Errorf("stderr: %q", stderr.String())
	}
}

// TestPairRejectsBadNameBeforeAnyRequest drives the cobra command with
// names the server would answer 422 to. The check runs before the
// backplane URL is resolved, so no login and no request are needed, and
// the error is a plain usage_error that names the allowed characters.
func TestPairRejectsBadNameBeforeAnyRequest(t *testing.T) {
	cases := []struct {
		name string
		want string
	}{
		{"my addon", "allowed: letters, digits, hyphen, underscore, dot"},
		{"addon/one", "allowed: letters, digits, hyphen, underscore, dot"},
		{"", "<name> must not be empty"},
		{strings.Repeat("a", 129), "at most 128 characters"},
	}
	for _, tc := range cases {
		root := NewRootCmd()
		stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
		root.SetOut(stdout)
		root.SetErr(stderr)
		root.SetArgs([]string{"pair", tc.name,
			"--contract-version", "1", "--min-backplane-version", "1", "--json"})
		err := root.Execute()
		assertRenderedErrorCode(t, stderr, err, output.ErrCodeUsage, tc.want)
		if strings.Contains(stderr.String(), "network_unreachable") {
			t.Errorf("name %q: must fail as a usage error, not a network error; got %q", tc.name, stderr.String())
		}
		if stdout.Len() != 0 {
			t.Errorf("name %q: stdout must be empty; got %q", tc.name, stdout.String())
		}
	}
}

func TestPairRejectsBadNameTextOutput(t *testing.T) {
	root := NewRootCmd()
	stderr := &bytes.Buffer{}
	root.SetOut(&bytes.Buffer{})
	root.SetErr(stderr)
	root.SetArgs([]string{"pair", "my addon", "--contract-version", "1", "--min-backplane-version", "1"})
	if err := root.Execute(); err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(stderr.String(), "meho: usage_error: <name> \"my addon\" contains characters outside the safe set") {
		t.Errorf("stderr: %q", stderr.String())
	}
}

func TestPairAcceptsEveryAllowedNameCharacter(t *testing.T) {
	if msg := checkAddonName("Auto-mation_v2.0"); msg != "" {
		t.Errorf("expected no usage message; got %q", msg)
	}
}

// ---------- error advice (step 6 of the task) ----------

// TestErrorAdvice covers every server code the task lists: the error
// names the server's code and one line that says what to do.
func TestErrorAdvice(t *testing.T) {
	cases := []struct {
		name       string
		route      string
		status     int
		detail     string
		run        func(cmd *cobra.Command, srv *httptest.Server) error
		wantCode   string
		wantDetail []string
	}{
		{
			name: "409 addon too old", route: "POST /api/v1/addons/pairings",
			status: http.StatusConflict, detail: "addon_contract_below_backplane_minimum",
			run:      runPairJSON,
			wantCode: output.ErrCodeUnexpected,
			wantDetail: []string{"addon_contract_below_backplane_minimum",
				"the add-on is too old for this MEHO. Upgrade the add-on."},
		},
		{
			name: "409 MEHO too old", route: "POST /api/v1/addons/pairings",
			status: http.StatusConflict, detail: "backplane_contract_below_addon_minimum",
			run:      runPairJSON,
			wantCode: output.ErrCodeUnexpected,
			wantDetail: []string{"backplane_contract_below_addon_minimum",
				"this MEHO is too old for the add-on. Upgrade MEHO."},
		},
		{
			name: "409 already paired", route: "POST /api/v1/addons/pairings",
			status: http.StatusConflict, detail: "addon_already_paired",
			run:      runPairJSON,
			wantCode: output.ErrCodeUnexpected,
			wantDetail: []string{"addon_already_paired",
				"run `meho addon unpair automation` first, then pair again."},
		},
		{
			name: "404 not paired", route: "GET /api/v1/addons/pairings/automation",
			status: http.StatusNotFound, detail: "addon_not_paired",
			run:        runShowJSON,
			wantCode:   output.ErrCodeUnexpected,
			wantDetail: []string{"addon_not_paired", `no add-on named "automation" is paired.`},
		},
		{
			name: "403 role", route: "DELETE /api/v1/addons/pairings/automation",
			status: http.StatusForbidden, detail: "tenant_admin role required",
			run:      runUnpairJSON,
			wantCode: output.ErrCodeInsufficientRole,
			wantDetail: []string{"tenant_admin role required",
				"pair and unpair need the tenant_admin role"},
		},
		{
			name: "403 role on a read", route: "GET /api/v1/addons/pairings",
			status: http.StatusForbidden, detail: "insufficient_role",
			run:        runListJSON,
			wantCode:   output.ErrCodeInsufficientRole,
			wantDetail: []string{"insufficient_role", "list and show need the operator role"},
		},
		{
			name: "503 keycloak admin not configured", route: "POST /api/v1/addons/pairings",
			status: http.StatusServiceUnavailable, detail: "keycloak_admin_not_configured",
			run:      runPairJSON,
			wantCode: output.ErrCodeUnexpected,
			wantDetail: []string{"keycloak_admin_not_configured",
				"this install cannot create logins for add-ons. The Keycloak admin client is not set up."},
		},
		{
			name: "502 keycloak failed", route: "DELETE /api/v1/addons/pairings/automation",
			status: http.StatusBadGateway, detail: "keycloak_admin_error:KeycloakAdminError",
			run:      runUnpairJSON,
			wantCode: output.ErrCodeUnexpected,
			wantDetail: []string{"keycloak_admin_error:KeycloakAdminError",
				"the call to Keycloak failed. Try again. If it fails again, check Keycloak."},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := mockBackplane(t, map[string]mockHandler{tc.route: writeDetail(tc.status, tc.detail)})
			defer srv.Close()
			_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
				return tc.run(cmd, srv)
			})
			for _, want := range tc.wantDetail {
				assertRenderedErrorCode(t, bytes.NewBuffer(stderr.Bytes()), err, tc.wantCode, want)
			}
		})
	}
}

func runPairJSON(cmd *cobra.Command, srv *httptest.Server) error {
	return runPair(cmd, pairOpts(srv, true))
}

func runListJSON(cmd *cobra.Command, srv *httptest.Server) error {
	return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
}

func runShowJSON(cmd *cobra.Command, srv *httptest.Server) error {
	return runShow(cmd, showOptions{Name: "automation", JSONOut: true, BackplaneOverride: srv.URL})
}

func runUnpairJSON(cmd *cobra.Command, srv *httptest.Server) error {
	return runUnpair(cmd, unpairOptions{Name: "automation", JSONOut: true, BackplaneOverride: srv.URL})
}

// TestErrorAdviceTextOutput pins the human line: code first, then the advice.
func TestErrorAdviceTextOutput(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/addons/pairings": writeDetail(http.StatusConflict, "addon_already_paired"),
	})
	defer srv.Close()
	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runPair(cmd, pairOpts(srv, false))
	})
	if err == nil {
		t.Fatal("expected an error")
	}
	want := "meho: unexpected_response: addon_already_paired: run `meho addon unpair automation` first, then pair again.\n"
	if stderr.String() != want {
		t.Errorf("stderr:\n got %q\nwant %q", stderr.String(), want)
	}
}

func TestUnknownStatusKeepsRawDetail(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/addons/pairings": writeDetail(http.StatusInternalServerError, "boom"),
	})
	defer srv.Close()
	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "HTTP 500: boom")
}

func TestListMaps401ToAuthExpired(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/addons/pairings": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
		},
	})
	defer srv.Close()
	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeAuthExpired, "no refresh_token")
}

// ---------- list ----------

func TestListRendersTable(t *testing.T) {
	seen := time.Date(2026, 10, 25, 8, 0, 0, 0, time.UTC)
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/addons/pairings": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, api.PairedAddonListResponse{
				Items: []api.PairedAddonRead{
					newPairingRead(t, "automation", &seen),
					newPairingRead(t, "ssp", nil),
				},
			})
		},
	})
	defer srv.Close()
	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runList: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"NAME", "CLIENT ID", "CONTRACT", "PAIRED AT", "LAST SEEN",
		"automation", "addon:automation", "2026-10-24T09:30:00Z", "2026-10-25T08:00:00Z",
		"ssp", "addon:ssp", "never",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printListTable missing %q in:\n%s", want, out)
		}
	}
}

func TestListJSONPrintsServerAnswer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/addons/pairings": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, api.PairedAddonListResponse{
				Items: []api.PairedAddonRead{newPairingRead(t, "automation", nil)},
			})
		},
	})
	defer srv.Close()
	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runList: %v", err)
	}
	var got api.PairedAddonListResponse
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout is not PairedAddonListResponse JSON: %v", err)
	}
	if len(got.Items) != 1 || got.Items[0].Name != "automation" {
		t.Errorf("decoded: %+v", got)
	}
}

func TestListRendersEmptyTable(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, &api.PairedAddonListResponse{})
	if !strings.Contains(buf.String(), "no add-ons paired in this tenant") {
		t.Errorf("empty list: got %q", buf.String())
	}
}

// ---------- show ----------

func TestShowRendersAllFields(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/addons/pairings/automation": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newPairingRead(t, "automation", nil))
		},
	})
	defer srv.Close()
	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runShow(cmd, showOptions{Name: "automation", BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runShow: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		`add-on pairing "automation"`,
		"id:                           " + stubID,
		"tenant_id:                    " + stubTenantID,
		"name:                         automation",
		"keycloak_client_id:           addon:automation",
		"owner_sub:                    owner-sub",
		"contract_version:             1",
		"addon_contract_version:       2",
		"addon_min_backplane_version:  1",
		"created_by_sub:               creator-sub",
		"paired_at:                    2026-10-24T09:30:00Z",
		"last_seen_at:                 never",
		"updated_at:                   2026-10-24T09:30:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("show missing %q in:\n%s", want, out)
		}
	}
}

func TestShowJSONPrintsServerAnswer(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/addons/pairings/automation": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newPairingRead(t, "automation", nil))
		},
	})
	defer srv.Close()
	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runShow(cmd, showOptions{Name: "automation", JSONOut: true, BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runShow: %v", err)
	}
	var got api.PairedAddonRead
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout is not PairedAddonRead JSON: %v", err)
	}
	if got.KeycloakClientId != "addon:automation" {
		t.Errorf("decoded: %+v", got)
	}
}

func TestPrintPairingFieldsNilSafe(t *testing.T) {
	var buf bytes.Buffer
	printPairingFields(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("nil entry should write nothing; got %q", buf.String())
	}
}

// ---------- unpair ----------

func TestUnpairSendsDeleteAndPrintsThreeFacts(t *testing.T) {
	var seenPath string
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/addons/pairings/automation": func(w http.ResponseWriter, r *http.Request) {
			seenPath = r.URL.Path
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runUnpair(cmd, unpairOptions{Name: "automation", BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runUnpair: %v", err)
	}
	if seenPath != "/api/v1/addons/pairings/automation" {
		t.Errorf("path: got %q", seenPath)
	}
	want := "unpaired add-on \"automation\": the pairing is removed.\n" +
		"The add-on's login no longer works.\n" +
		"The surfaces the add-on declared are gone.\n"
	if stdout.String() != want {
		t.Errorf("stdout:\n got %q\nwant %q", stdout.String(), want)
	}
}

func TestUnpairJSON(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/addons/pairings/automation": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runUnpair(cmd, unpairOptions{Name: "automation", JSONOut: true, BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runUnpair: %v", err)
	}
	var got unpairResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout is not JSON: %v", err)
	}
	if got.Name != "automation" || !got.Unpaired {
		t.Errorf("decoded: %+v", got)
	}
}

func TestUnpairMaps404ToNotPaired(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/addons/pairings/ghost": writeDetail(http.StatusNotFound, "addon_not_paired"),
	})
	defer srv.Close()
	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runUnpair(cmd, unpairOptions{Name: "ghost", JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, `no add-on named "ghost" is paired.`)
}
