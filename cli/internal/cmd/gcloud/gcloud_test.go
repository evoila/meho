// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
)

// ---------- helper tests (pure-function) ----------

// TestConnectorIDIsFrozen pins the pre-baked connector_id constant.
// Every verb file dispatches against this value; a regression here
// would silently rebind every alias verb to a different connector.
// The id encodes the registry-v2 natural key triple
// `(product="gcloud", version="1.0", impl_id="gcloud-rest")`.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "gcloud-rest-1.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "gcloud-rest-1.0")
	}
}

// TestTruncatePassthroughAndCut covers the rune-aware truncate
// helper. Same shape as the bind9 / k8s siblings.
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

// TestNormaliseURLBasic mirrors the bind9 sibling — trailing-slash
// trimming + reject-empty are the load-bearing properties.
func TestNormaliseURLBasic(t *testing.T) {
	got, err := normaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
	if _, err := normaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty should reject; got %v", err)
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound maps
// to AuthExpired; everything else maps to Unexpected.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrapped := &errNoBackplaneConfigured{inner: auth.ErrConfigNotFound}
	se := classifyBackplaneError(wrapped)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("wrapped ErrConfigNotFound should classify as auth_expired; got %+v", se)
	}
	se = classifyBackplaneError(errors.New("parse failure"))
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected_response; got %+v", se)
	}
}

// ---------- decode helpers ----------

func TestDecodeFlatResultHappy(t *testing.T) {
	raw := json.RawMessage(`{"project_id":"my-project","project_number":"123"}`)
	got, err := decodeFlatResult(raw)
	if err != nil {
		t.Fatalf("decodeFlatResult: %v", err)
	}
	if got["project_id"] != "my-project" {
		t.Fatalf("decoded: %+v", got)
	}
}

func TestDecodeFlatResultNullAndEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		got, err := decodeFlatResult(raw)
		if err != nil {
			t.Fatalf("decodeFlatResult empty: %v", err)
		}
		if got != nil {
			t.Fatalf("empty should be nil map; got %+v", got)
		}
	}
}

func TestDecodeRowsResultHappy(t *testing.T) {
	raw := json.RawMessage(`{"rows":[{"name":"compute.googleapis.com","state":"ENABLED","title":"Compute Engine API"}],"total":1}`)
	rows, err := decodeRowsResult(raw)
	if err != nil {
		t.Fatalf("decodeRowsResult: %v", err)
	}
	if len(rows) != 1 || rows[0]["name"] != "compute.googleapis.com" {
		t.Fatalf("decoded: %+v", rows)
	}
}

// TestDecodeRowsResultEmptyVsAbsent pins the load-bearing distinction:
// a legitimately-empty `rows` list is a clean empty result, while an
// envelope with no `rows` key at all is malformed and reported via the
// errRowsKeyAbsent sentinel so callers route it to the fallback raw
// render instead of a misleading "(0 rows)" line.
func TestDecodeRowsResultEmptyVsAbsent(t *testing.T) {
	// Empty list: nil error, zero-length, non-nil slice (renders "0 rows").
	rows, err := decodeRowsResult(json.RawMessage(`{"rows":[],"total":0}`))
	if err != nil {
		t.Fatalf("empty rows should not error; got %v", err)
	}
	if rows == nil {
		t.Fatalf("empty rows should be a non-nil empty slice; got nil")
	}
	if len(rows) != 0 {
		t.Fatalf("empty rows should have len 0; got %d", len(rows))
	}

	// Absent key: errRowsKeyAbsent sentinel, no rows.
	rows, err = decodeRowsResult(json.RawMessage(`{"total":0}`))
	if !errors.Is(err, errRowsKeyAbsent) {
		t.Fatalf("absent rows key should report errRowsKeyAbsent; got %v", err)
	}
	if rows != nil {
		t.Fatalf("absent rows key should return nil rows; got %+v", rows)
	}
}

// TestDecodeRowsResultNullResult — a JSON null (or empty) result is the
// "nothing to render" case: (nil, nil), not an error. Distinct from an
// absent `rows` key inside a non-null object envelope.
func TestDecodeRowsResultNullResult(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		rows, err := decodeRowsResult(raw)
		if err != nil {
			t.Fatalf("null/empty result should not error; got %v", err)
		}
		if rows != nil {
			t.Fatalf("null/empty result should return nil rows; got %+v", rows)
		}
	}
}

// TestDecodeRowsResultMalformedEnvelope — a non-object top-level (e.g. a
// bare array) is malformed and returns a decode error, routing the
// caller to the fallback render.
func TestDecodeRowsResultMalformedEnvelope(t *testing.T) {
	if _, err := decodeRowsResult(json.RawMessage(`[1,2,3]`)); err == nil {
		t.Fatalf("non-object envelope should error")
	}
}

func TestStringFieldMissingAndWrongType(t *testing.T) {
	e := map[string]any{"name": "x", "count": 3.0}
	if got := stringField(e, "name"); got != "x" {
		t.Errorf("stringField(name): %q", got)
	}
	if got := stringField(e, "count"); got != "" {
		t.Errorf("stringField(count, float): expected empty; got %q", got)
	}
	if got := stringField(e, "missing"); got != "" {
		t.Errorf("stringField(missing): expected empty; got %q", got)
	}
}

func TestBoolField(t *testing.T) {
	e := map[string]any{"disabled": true, "count": 3.0}
	if !boolField(e, "disabled") {
		t.Errorf("boolField(disabled): expected true")
	}
	if boolField(e, "count") {
		t.Errorf("boolField(count, float): expected false")
	}
	if boolField(e, "missing") {
		t.Errorf("boolField(missing): expected false")
	}
}

// ---------- errOpError sentinel ----------

func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatalf("errOpError should be non-nil")
	}
	if errors.Is(errOpError, errors.New("other")) {
		t.Fatalf("errOpError should not match arbitrary errors")
	}
}

// ---------- renderer unit tests ----------

// TestPrintAboutHumanFormat — happy-path render with all identity fields.
func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "gcloud.about",
		Result:     json.RawMessage(`{"project_id":"my-project","project_number":"987654321","lifecycle_state":"ACTIVE","organization":"112233445566"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"status=ok", ConnectorID, "my-project", "987654321", "ACTIVE", "112233445566",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintAboutErrorRendersErrorString — status=error with a
// non-nil Error pointer surfaces the error string.
func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "impersonation failed: caller lacks iam.serviceAccounts.actAs"
	r := &CallResult{
		Status:     "error",
		OpID:       "gcloud.about",
		Error:      &errMsg,
		Extras:     json.RawMessage(`{"reason":"permission_denied"}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "impersonation failed", "extras:", "permission_denied"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintServicesListTable — happy-path render with 2 services.
func TestPrintServicesListTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.services.list",
		Result: json.RawMessage(`{"rows":[{"name":"compute.googleapis.com","title":"Compute Engine API","state":"ENABLED"},{"name":"iam.googleapis.com","title":"Identity and Access Management (IAM) API","state":"ENABLED"}],"total":2}`),
	}
	var buf bytes.Buffer
	printServicesList(&buf, r)
	out := buf.String()
	for _, want := range []string{"compute.googleapis.com", "iam.googleapis.com", "ENABLED"} {
		if !strings.Contains(out, want) {
			t.Errorf("printServicesList missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintServicesListEmpty — zero services renders the count line.
func TestPrintServicesListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", OpID: "gcloud.services.list", Result: json.RawMessage(`{"rows":[],"total":0}`)}
	var buf bytes.Buffer
	printServicesList(&buf, r)
	if !strings.Contains(buf.String(), "(0 services)") {
		t.Errorf("empty list should announce 0 services; got:\n%s", buf.String())
	}
}

// TestPrintIamSaListTable — happy-path render with 2 service accounts.
func TestPrintIamSaListTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.iam.service_accounts.list",
		Result: json.RawMessage(`{"rows":[{"email":"meho@my-project.iam.gserviceaccount.com","unique_id":"123456","display_name":"MEHO SA","description":"","disabled":false},{"email":"old@my-project.iam.gserviceaccount.com","unique_id":"789","display_name":"Old SA","description":"","disabled":true}],"total":2}`),
	}
	var buf bytes.Buffer
	printIamSaList(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"meho@my-project.iam.gserviceaccount.com",
		"old@my-project.iam.gserviceaccount.com",
		"MEHO SA",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printIamSaList missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintIamPolicyReadTable — happy-path render with 2 bindings.
func TestPrintIamPolicyReadTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.iam.policy.read",
		Result: json.RawMessage(`{"version":1,"etag":"BwXabcDef","bindings":[{"role":"roles/editor","members":["serviceAccount:meho@my-project.iam.gserviceaccount.com"]},{"role":"roles/viewer","members":["user:alice@example.com"]}]}`),
	}
	var buf bytes.Buffer
	printIamPolicyRead(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"BwXabcDef", "roles/editor", "serviceAccount:meho@my-project.iam.gserviceaccount.com",
		"roles/viewer", "alice@example.com",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printIamPolicyRead missing %q in output:\n%s", want, out)
		}
	}
}

// iamPolicyWithMembers builds a single-binding policy result with the
// given member count (member:N for N in 1..count). Helper for the
// footer-honesty tests.
func iamPolicyWithMembers(count int) json.RawMessage {
	members := make([]string, 0, count)
	for i := 1; i <= count; i++ {
		members = append(members, fmt.Sprintf("user:m%d@example.com", i))
	}
	b, err := json.Marshal(members)
	if err != nil {
		panic(err)
	}
	return json.RawMessage(fmt.Sprintf(
		`{"version":1,"etag":"E","bindings":[{"role":"roles/viewer","members":%s}]}`, b))
}

// countMembersShown reports how many "user:mN@example.com" member lines
// appear in the render. Used to assert the cap was actually applied.
func countMembersShown(out string) int {
	return strings.Count(out, "@example.com")
}

// TestPrintIamPolicyReadFooterHonestyUnderCap — a binding with no more
// than the cap of members prints every member and emits NO "…" footer.
// This is the bug the Task targets: the old code printed every member
// and *then* appended a misleading "… (N total)" that implied a
// truncation that never happened.
func TestPrintIamPolicyReadFooterHonestyUnderCap(t *testing.T) {
	// Exactly maxPolicyMembersShown members: every one printed, no footer.
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.iam.policy.read",
		Result: iamPolicyWithMembers(maxPolicyMembersShown),
	}
	var buf bytes.Buffer
	printIamPolicyRead(&buf, r)
	out := buf.String()

	if strings.Contains(out, "…") {
		t.Errorf("no member should be elided at the cap; output must not contain an ellipsis:\n%s", out)
	}
	if strings.Contains(out, "total)") {
		t.Errorf("no footer should print when nothing is hidden:\n%s", out)
	}
	if got := countMembersShown(out); got != maxPolicyMembersShown {
		t.Errorf("all %d members should be printed; got %d:\n%s", maxPolicyMembersShown, got, out)
	}
}

// TestPrintIamPolicyReadFooterHonestyOverCap — a binding with more than
// the cap of members truncates to the cap and reports an honest footer
// stating how many were hidden and the true total. The "…" now reflects
// a real elision.
func TestPrintIamPolicyReadFooterHonestyOverCap(t *testing.T) {
	total := maxPolicyMembersShown + 3 // 3 hidden
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.iam.policy.read",
		Result: iamPolicyWithMembers(total),
	}
	var buf bytes.Buffer
	printIamPolicyRead(&buf, r)
	out := buf.String()

	// Exactly the cap of member lines is printed (truncation happened).
	if got := countMembersShown(out); got != maxPolicyMembersShown {
		t.Errorf("expected %d member lines (capped); got %d:\n%s", maxPolicyMembersShown, got, out)
	}
	// Footer reports the hidden count and the true total honestly.
	hidden := total - maxPolicyMembersShown
	wantFooter := fmt.Sprintf("… (%d more, %d total)", hidden, total)
	if !strings.Contains(out, wantFooter) {
		t.Errorf("expected honest footer %q in output:\n%s", wantFooter, out)
	}
}

// TestPrintComputeInstancesListTable — happy-path render with 1 VM.
func TestPrintComputeInstancesListTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.compute.instances.list",
		Result: json.RawMessage(`{"rows":[{"zone":"europe-west3-a","name":"my-vm","machine_type":"e2-standard-4","status":"RUNNING","internal_ips":["10.156.0.2"],"external_ips":[],"creation_timestamp":"2026-01-01T00:00:00Z"}],"total":1}`),
	}
	var buf bytes.Buffer
	printComputeInstancesList(&buf, r)
	out := buf.String()
	for _, want := range []string{"europe-west3-a", "my-vm", "e2-standard-4", "RUNNING", "10.156.0.2"} {
		if !strings.Contains(out, want) {
			t.Errorf("printComputeInstancesList missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintComputeNetworksListTable — happy-path render with 1 network.
func TestPrintComputeNetworksListTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.compute.networks.list",
		Result: json.RawMessage(`{"rows":[{"name":"default","auto_create_subnetworks":true,"routing_mode":"REGIONAL_MANAGED","mtu":1460,"creation_timestamp":"2026-01-01T00:00:00Z"}],"total":1}`),
	}
	var buf bytes.Buffer
	printComputeNetworksList(&buf, r)
	out := buf.String()
	for _, want := range []string{"default", "true", "REGIONAL_MANAGED"} {
		if !strings.Contains(out, want) {
			t.Errorf("printComputeNetworksList missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintComputeSubnetsListTable — happy-path render with 1 subnet.
func TestPrintComputeSubnetsListTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "gcloud.compute.subnetworks.list",
		Result: json.RawMessage(`{"rows":[{"region":"europe-west3","name":"default","cidr_range":"10.156.0.0/20","network":"projects/my-project/global/networks/default","purpose":"PRIVATE","private_ip_google_access":false,"creation_timestamp":"2026-01-01T00:00:00Z"}],"total":1}`),
	}
	var buf bytes.Buffer
	printComputeSubnetsList(&buf, r)
	out := buf.String()
	for _, want := range []string{"europe-west3", "default", "10.156.0.0/20", "PRIVATE"} {
		if !strings.Contains(out, want) {
			t.Errorf("printComputeSubnetsList missing %q in output:\n%s", want, out)
		}
	}
}

// ---------- HTTP wire shape (mocked) ----------

type mockHandler = http.HandlerFunc

// mockBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>` keys. Same shape as the bind9 sibling's helper.
func mockBackplane(t *testing.T, routes map[string]mockHandler) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			h(w, r)
			return
		}
		if h, ok := routes[""]; ok {
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

// primeToken installs an in-memory token store with a usable bearer
// for the mocked backplane URL. Mirrors the bind9 sibling.
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

// TestDispatchOpBakesConnectorID — every verb dispatches with
// connector_id="gcloud-rest-1.0" pre-baked. This test pins that the
// dispatcher helper writes the canonical connector_id into the
// CallOperationBody — a regression here would silently rebind every
// alias verb to a different connector.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "gcloud-rest-1.0" {
				t.Errorf("connector_id: got %q want gcloud-rest-1.0", body.ConnectorID)
			}
			if body.OpID != "gcloud.about" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "gcloud.about",
				Result: json.RawMessage(`{"project_id":"my-project","project_number":"123","lifecycle_state":"ACTIVE","organization":null}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "gcloud.about", "rdc-gcp-dev", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpTargetSlugWrappedAsName — a non-empty target slug
// must surface as `{"name": "<slug>"}` so the dispatcher's resolver
// pulls it under the canonical TargetRef shape.
func TestDispatchOpTargetSlugWrappedAsName(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var raw map[string]any
			if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			tgt, _ := raw["target"].(map[string]any)
			if tgt == nil || tgt["name"] != "rdc-gcp-dev" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "gcloud.about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "gcloud.about", "rdc-gcp-dev", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpServicesListSendsEnabledOnlyParam — pins the
// gcloud.services.list wire shape: enabled_only param lands under
// its canonical key.
func TestDispatchOpServicesListSendsEnabledOnlyParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "gcloud.services.list" {
				t.Errorf("op_id: got %q want gcloud.services.list", body.OpID)
			}
			// enabled_only=false is the "all services" path
			if v, ok := body.Params["enabled_only"]; !ok || v != false {
				t.Errorf("enabled_only: got %v want false", body.Params["enabled_only"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "gcloud.services.list",
				Result: json.RawMessage(`{"rows":[],"total":0}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"enabled_only": false}
	if _, err := dispatchOp(context.Background(), srv.URL, "gcloud.services.list", "rdc-gcp-dev", params); err != nil {
		t.Fatalf("dispatchOp services.list: %v", err)
	}
}

// TestDispatchOpComputeInstancesListSendsZoneParam — pins the
// gcloud.compute.instances.list wire shape: zone param lands correctly.
func TestDispatchOpComputeInstancesListSendsZoneParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "gcloud.compute.instances.list" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["zone"] != "europe-west3-a" {
				t.Errorf("zone: got %v", body.Params["zone"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "gcloud.compute.instances.list",
				Result: json.RawMessage(`{"rows":[],"total":0}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"zone": "europe-west3-a"}
	if _, err := dispatchOp(context.Background(), srv.URL, "gcloud.compute.instances.list", "rdc-gcp-dev", params); err != nil {
		t.Fatalf("dispatchOp instances.list: %v", err)
	}
}

// TestDispatchOpComputeSubnetsListSendsRegionParam — pins the
// gcloud.compute.subnetworks.list wire shape: region param lands correctly.
func TestDispatchOpComputeSubnetsListSendsRegionParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "gcloud.compute.subnetworks.list" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["region"] != "europe-west3" {
				t.Errorf("region: got %v", body.Params["region"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "gcloud.compute.subnetworks.list",
				Result: json.RawMessage(`{"rows":[],"total":0}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"region": "europe-west3"}
	if _, err := dispatchOp(context.Background(), srv.URL, "gcloud.compute.subnetworks.list", "rdc-gcp-dev", params); err != nil {
		t.Fatalf("dispatchOp subnetworks.list: %v", err)
	}
}

// TestDispatchOpAllEightOps — the eight gcloud op IDs all dispatch
// correctly (connector_id pre-baked; target slug wrapped).
func TestDispatchOpAllEightOps(t *testing.T) {
	opIDs := []string{
		"gcloud.about",
		"gcloud.project.describe",
		"gcloud.services.list",
		"gcloud.iam.service_accounts.list",
		"gcloud.compute.instances.list",
		"gcloud.compute.networks.list",
		"gcloud.compute.subnetworks.list",
		"gcloud.iam.policy.read",
	}
	for _, opID := range opIDs {
		opID := opID // capture
		t.Run(opID, func(t *testing.T) {
			srv := mockBackplane(t, map[string]mockHandler{
				"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
					var body callRequestBody
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Errorf("decode: %v", err)
						w.WriteHeader(400)
						return
					}
					if body.ConnectorID != ConnectorID {
						t.Errorf("[%s] connector_id: got %q want %q", opID, body.ConnectorID, ConnectorID)
					}
					if body.OpID != opID {
						t.Errorf("[%s] op_id: got %q want %q", opID, body.OpID, opID)
					}
					if tgt, _ := body.Target["name"].(string); tgt != "rdc-gcp-dev" {
						t.Errorf("[%s] target.name: got %q want rdc-gcp-dev", opID, tgt)
					}
					writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opID})
				},
			})
			defer srv.Close()
			primeToken(t, srv.URL)

			if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-gcp-dev", nil); err != nil {
				t.Fatalf("[%s] dispatchOp: %v", opID, err)
			}
		})
	}
}
