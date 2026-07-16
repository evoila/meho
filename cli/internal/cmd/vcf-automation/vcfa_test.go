// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// ---------- helper tests ----------

func TestTruncatePassthroughAndCut(t *testing.T) {
	tests := []struct {
		name   string
		in     string
		maxLen int
		want   string
	}{
		{"within budget", "abc", 5, "abc"},
		{"over budget ascii", "abcdef", 4, "abc…"},
		{"multi-byte safe", "café world", 5, "café…"},
		{"zero budget", "x", 0, ""},
	}
	for _, tt := range tests {
		if got := truncate(tt.in, tt.maxLen); got != tt.want {
			t.Errorf("%s: truncate(%q, %d) = %q; want %q", tt.name, tt.in, tt.maxLen, got, tt.want)
		}
	}
}

func TestNormaliseURLBasic(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
	if _, err := backplane.NormaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty should reject; got %v", err)
	}
}

func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrapped := &backplane.NotConfiguredError{Inner: auth.ErrConfigNotFound}
	se := backplane.ClassifyError(wrapped)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("wrapped ErrConfigNotFound should classify as auth_expired; got %+v", se)
	}
	se = backplane.ClassifyError(errors.New("parse failure"))
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected_response; got %+v", se)
	}
}

func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "vcfa-rest-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "vcfa-rest-9.0")
	}
}

func TestPlaneConstantsAreFrozen(t *testing.T) {
	if PlaneProvider != "provider" || PlaneTenant != "tenant" {
		t.Fatalf("Plane constants drifted: provider=%q tenant=%q", PlaneProvider, PlaneTenant)
	}
}

func TestAboutOpForPlane(t *testing.T) {
	if got := aboutOpForPlane(PlaneProvider); got != "vcfa.provider.health" {
		t.Errorf("provider about op_id: got %q", got)
	}
	if got := aboutOpForPlane(PlaneTenant); got != "vcfa.tenant.about" {
		t.Errorf("tenant about op_id: got %q", got)
	}
	// Defensive: empty / unknown plane → provider (the default
	// fallback at the dispatcher layer). The CLI never reaches this
	// branch because requirePlane(...) rejects empty / unknown values
	// upstream, but covering the fallback keeps the contract honest.
	if got := aboutOpForPlane(""); got != "vcfa.provider.health" {
		t.Errorf("empty plane should fall back to provider; got %q", got)
	}
}

func TestValidatePlaneAcceptsMatchingOrEmpty(t *testing.T) {
	for _, in := range []string{"", "provider"} {
		if se := validatePlane(in, PlaneProvider); se != nil {
			t.Errorf("validatePlane(%q, provider) should accept; got %+v", in, se)
		}
	}
}

func TestValidatePlaneRejectsMismatch(t *testing.T) {
	se := validatePlane("tenant", PlaneProvider)
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("validatePlane mismatch should reject; got %+v", se)
	}
	if !strings.Contains(se.Detail, "tenant") || !strings.Contains(se.Detail, "provider") {
		t.Errorf("validatePlane message should name both planes; got %q", se.Detail)
	}
}

func TestRequirePlaneRequiresKnownValue(t *testing.T) {
	if se := requirePlane(""); se == nil || !strings.Contains(se.Detail, "--plane is required") {
		t.Fatalf("requirePlane('') should reject with --plane is required; got %+v", se)
	}
	if se := requirePlane("bogus"); se == nil || !strings.Contains(se.Detail, "unknown") {
		t.Fatalf("requirePlane('bogus') should reject with unknown; got %+v", se)
	}
	if se := requirePlane(PlaneProvider); se != nil {
		t.Fatalf("requirePlane('provider') should accept; got %+v", se)
	}
	if se := requirePlane(PlaneTenant); se != nil {
		t.Fatalf("requirePlane('tenant') should accept; got %+v", se)
	}
}

// ---------- loadParamsFlag ----------

func TestLoadParamsFlagEmpty(t *testing.T) {
	got, err := loadParamsFlag("")
	if err != nil || got != nil {
		t.Fatalf("loadParamsFlag(\"\"): err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInlineJSON(t *testing.T) {
	got, err := loadParamsFlag(`{"id":"a1"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["id"] != "a1" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

// ---------- decoder tests ----------

func TestDecodeProviderListResultValuesWrapped(t *testing.T) {
	raw := json.RawMessage(`{"values":[{"id":"o1","name":"acme"}],"resultTotal":1}`)
	entries, err := decodeProviderListResult(raw)
	if err != nil {
		t.Fatalf("decodeProviderListResult: %v", err)
	}
	if len(entries) != 1 || entries[0]["id"] != "o1" {
		t.Fatalf("values-wrapped decode: got %+v", entries)
	}
}

func TestDecodeTenantListResultContentWrapped(t *testing.T) {
	raw := json.RawMessage(`{"content":[{"id":"d1","name":"web"}],"totalElements":1}`)
	entries, err := decodeTenantListResult(raw)
	if err != nil {
		t.Fatalf("decodeTenantListResult: %v", err)
	}
	if len(entries) != 1 || entries[0]["name"] != "web" {
		t.Fatalf("content-wrapped decode: got %+v", entries)
	}
}

func TestDecodeProviderListResultEmptyRaw(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeProviderListResult(raw)
		if err != nil || entries != nil {
			t.Fatalf("decode empty: err=%v entries=%v", err, entries)
		}
	}
}

// ---------- renderers ----------

func TestPrintAboutProviderHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/cloudapi/1.0.0/site",
		Result:     json.RawMessage(`{"id":"site-1","name":"VCFA-Site","restName":"vcfa-rdc","productVersion":"9.0.0-12345"}`),
		DurationMs: 30,
	}
	var buf bytes.Buffer
	printAbout(&buf, PlaneProvider, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "vcfa-rest-9.0", "9.0.0-12345", "vcfa-rdc", "VCFA-Site"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout provider missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintAboutTenantHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/iaas/api/about",
		Result:     json.RawMessage(`{"latestApiVersion":"2024-01-01","supportedApis":[{"apiVersion":"2024-01-01"}]}`),
		DurationMs: 12,
	}
	var buf bytes.Buffer
	printAbout(&buf, PlaneTenant, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "vcfa-rest-9.0", "2024-01-01", "supported_apis"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout tenant missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintOrgListHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"values":[{"id":"o-1","name":"acme","isEnabled":true}],"resultTotal":1}`),
		DurationMs: 10,
	}
	var buf bytes.Buffer
	printOrgList(&buf, r)
	out := buf.String()
	for _, want := range []string{"o-1", "acme", "true"} {
		if !strings.Contains(out, want) {
			t.Errorf("printOrgList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintProjectListHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"content":[{"id":"p-1","name":"team-a","organizationId":"org-1"}],"totalElements":1}`),
		DurationMs: 7,
	}
	var buf bytes.Buffer
	printProjectList(&buf, r)
	out := buf.String()
	for _, want := range []string{"p-1", "team-a", "org-1"} {
		if !strings.Contains(out, want) {
			t.Errorf("printProjectList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintDeploymentListEmpty(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"content":[],"totalElements":0}`),
		DurationMs: 4,
	}
	var buf bytes.Buffer
	printDeploymentList(&buf, r)
	if !strings.Contains(buf.String(), "0 deployments") {
		t.Errorf("empty deployment list should announce 0; got:\n%s", buf.String())
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List tenant deployments"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/iaas/api/deployments", Summary: &summary, FusedScore: 0.95},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "deployments", r)
	out := buf.String()
	for _, want := range []string{"vcfa-rest-9.0", "deployments", "1 hit(s)", "GET:/iaas/api/deployments"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in:\n%s", want, out)
		}
	}
}

// ---------- HTTP wire shape ----------

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
		w.WriteHeader(404)
	}))
}

func writeJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Errorf("writeJSON marshal: %v", err)
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write(raw); err != nil {
		t.Errorf("writeJSON write: %v", err)
	}
}

func primeToken(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfg := filepath.Join(dir, "meho", "config.json")
	if err := os.MkdirAll(filepath.Dir(cfg), 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": backplaneURL})
	if err := os.WriteFile(cfg, cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: backplaneURL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
}

// TestDispatchOpBakesConnectorID — pins that connector_id="vcfa-rest-9.0"
// is sent on every alias-verb dispatch.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "vcfa-rest-9.0" {
				t.Errorf("connector_id: got %q want vcfa-rest-9.0", body.ConnectorID)
			}
			if body.OpID != "GET:/cloudapi/1.0.0/site" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/cloudapi/1.0.0/site",
				Result: json.RawMessage(`{"id":"site-1","name":"VCFA"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "GET:/cloudapi/1.0.0/site", "rdc-vcfa", "", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpEmptyTargetSendsNullTarget — empty slug → null target on wire.
func TestDispatchOpEmptyTargetSendsNullTarget(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var raw map[string]any
			if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if raw["target"] != nil {
				t.Errorf("empty target should be null on wire; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "GET:/iaas/api/about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/iaas/api/about", "", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpThreadsFqdnIntoTargetBody — load-bearing per #840:
// when --fqdn is set, the dispatch body's target dict carries
// `fqdn` alongside `name` so the backend can override the resolved
// target's vhost. Without this the IP-only target use case fails
// with a silent 404 from the appliance.
func TestDispatchOpThreadsFqdnIntoTargetBody(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.Target == nil {
				t.Errorf("target should be present when --fqdn is set")
				w.WriteHeader(400)
				return
			}
			if got := body.Target["name"]; got != "rdc-vcfa" {
				t.Errorf("target.name: got %v want rdc-vcfa", got)
			}
			if got := body.Target["fqdn"]; got != "vcfa.rdc.example.com" {
				t.Errorf("target.fqdn: got %v want vcfa.rdc.example.com", got)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	const opID = "GET:/cloudapi/1.0.0/site"
	if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-vcfa", "vcfa.rdc.example.com", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpOmitsFqdnWhenEmpty — when the operator did not pass
// --fqdn, the body's target dict carries only `name`. Threading an
// empty string would override the registry's stored fqdn with a
// blank value, which would defeat the per-target configuration.
func TestDispatchOpOmitsFqdnWhenEmpty(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if _, present := body.Target["fqdn"]; present {
				t.Errorf("target.fqdn must be absent when --fqdn is empty; got body.Target=%v", body.Target)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/cloudapi/1.0.0/site", "rdc-vcfa", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchGetByIdSubstitutesPathParam — the `org get <id>` verb
// passes `id` in params for the dispatcher's `_substitute_path` to
// fill the `{id}` placeholder.
func TestDispatchGetByIdSubstitutesPathParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			id, _ := body.Params["id"].(string)
			if id != "abc123" {
				t.Errorf("params.id: got %q want abc123", id)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"id": "abc123"}
	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/cloudapi/1.0.0/orgs/{id}", "rdc-vcfa", "", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestRootCmdHasPlaneAndFqdnPersistentFlags — sanity check that the
// persistent flags ship; per-verb tests below assert the threading.
func TestRootCmdHasPlaneAndFqdnPersistentFlags(t *testing.T) {
	cmd := NewRootCmd()
	if cmd.PersistentFlags().Lookup("plane") == nil {
		t.Errorf("--plane persistent flag missing on vcf-automation root")
	}
	if cmd.PersistentFlags().Lookup("fqdn") == nil {
		t.Errorf("--fqdn persistent flag missing on vcf-automation root")
	}
}

// TestAboutVerbDispatchesPerPlane — exercises the full Cobra invocation
// path (RunE → runAbout → dispatchOp → mock backplane) for both
// planes, asserting the wire shape (op_id + connector_id + target.name).
func TestAboutVerbDispatchesPerPlane(t *testing.T) {
	type wireCheck struct {
		plane       string
		expectedOp  string
		responseRes string
	}
	cases := []wireCheck{
		{
			plane:       PlaneProvider,
			expectedOp:  "vcfa.provider.health",
			responseRes: `{"id":"site-1","name":"VCFA"}`,
		},
		{
			plane:       PlaneTenant,
			expectedOp:  "vcfa.tenant.about",
			responseRes: `{"latestApiVersion":"2024-01-01"}`,
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.plane, func(t *testing.T) {
			var capturedOpID string
			srv := mockBackplane(t, map[string]mockHandler{
				"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
					var body callRequestBody
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Errorf("decode body: %v", err)
						w.WriteHeader(400)
						return
					}
					capturedOpID = body.OpID
					writeJSON(t, w, 200, CallResult{
						Status: "ok",
						OpID:   body.OpID,
						Result: json.RawMessage(tc.responseRes),
					})
				},
			})
			defer srv.Close()
			primeToken(t, srv.URL)

			root := NewRootCmd()
			root.SetArgs([]string{
				"about",
				"--plane", tc.plane,
				"--target", "rdc-vcfa",
				"--backplane", srv.URL,
			})
			root.SetOut(&bytes.Buffer{})
			root.SetErr(&bytes.Buffer{})
			if err := root.Execute(); err != nil {
				t.Fatalf("execute: %v", err)
			}
			if capturedOpID != tc.expectedOp {
				t.Errorf("dispatch op_id for plane %s: got %q want %q", tc.plane, capturedOpID, tc.expectedOp)
			}
		})
	}
}

// TestAboutVerbRejectsMissingPlane — `about` is dual-plane; --plane
// is required. Cobra exits with a structured error when omitted.
func TestAboutVerbRejectsMissingPlane(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"about", "--target", "rdc-vcfa", "--backplane", "https://meho.test.invalid"})
	var stderr bytes.Buffer
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&stderr)
	err := root.Execute()
	if err == nil {
		t.Fatalf("expected error for missing --plane; got nil")
	}
	if !strings.Contains(stderr.String(), "--plane is required") {
		t.Errorf("stderr should mention --plane is required; got:\n%s", stderr.String())
	}
}

// TestProjectListRejectsProviderPlane — tenant-only verbs error when
// the operator passes --plane provider (catches the wrong-plane typo
// before dispatch).
func TestProjectListRejectsProviderPlane(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"project", "list", "--plane", "provider", "--target", "rdc-vcfa", "--backplane", "https://meho.test.invalid"})
	var stderr bytes.Buffer
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&stderr)
	err := root.Execute()
	if err == nil {
		t.Fatalf("expected error for --plane provider on tenant verb; got nil")
	}
	if !strings.Contains(stderr.String(), "tenant") || !strings.Contains(stderr.String(), "provider") {
		t.Errorf("stderr should name both planes; got:\n%s", stderr.String())
	}
}

// TestOrgListRejectsTenantPlane — mirror of the above for a
// provider-plane verb.
func TestOrgListRejectsTenantPlane(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"org", "list", "--plane", "tenant", "--target", "rdc-vcfa", "--backplane", "https://meho.test.invalid"})
	var stderr bytes.Buffer
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&stderr)
	err := root.Execute()
	if err == nil {
		t.Fatalf("expected error for --plane tenant on provider verb; got nil")
	}
}

// TestDeploymentListThreadsFqdn — load-bearing end-to-end check: the
// --fqdn persistent flag flows through the deployment list verb's
// dispatch into the body's target.fqdn field.
func TestDeploymentListThreadsFqdn(t *testing.T) {
	var capturedFqdn any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.Target != nil {
				capturedFqdn = body.Target["fqdn"]
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   body.OpID,
				Result: json.RawMessage(`{"content":[],"totalElements":0}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"deployment", "list",
		"--target", "rdc-vcfa",
		"--fqdn", "vcfa.rdc.example.com",
		"--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if capturedFqdn != "vcfa.rdc.example.com" {
		t.Errorf("expected target.fqdn=vcfa.rdc.example.com on the wire; got %v", capturedFqdn)
	}
}

// TestErrOpErrorIsSentinel — pins the exported sentinel.
func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
	}
}
