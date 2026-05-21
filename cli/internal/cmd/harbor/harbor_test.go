// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

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

func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "harbor-rest-2.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "harbor-rest-2.x")
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
	got, err := loadParamsFlag(`{"project_name":"library"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["project_name"] != "library" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"project_name":"myproject"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["project_name"] != "myproject" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeHarborList ----------

func TestDecodeHarborListBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"name":"library"},{"name":"dev"}]`)
	items, err := decodeHarborList(raw)
	if err != nil {
		t.Fatalf("decodeHarborList: %v", err)
	}
	if len(items) != 2 || items[0]["name"] != "library" {
		t.Fatalf("bare-array decode: got %+v", items)
	}
}

func TestDecodeHarborListEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		items, err := decodeHarborList(raw)
		if err != nil || items != nil {
			t.Fatalf("decodeHarborList empty: err=%v items=%v", err, items)
		}
	}
}

func TestDecodeHarborListEmptyArray(t *testing.T) {
	items, err := decodeHarborList(json.RawMessage(`[]`))
	if err != nil {
		t.Fatalf("decodeHarborList []: %v", err)
	}
	if len(items) != 0 {
		t.Fatalf("expected empty slice; got %v", items)
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/api/v2.0/systeminfo",
		Result:     json.RawMessage(`{"harbor_version":"v2.11.0","auth_mode":"db_auth","registry_url":"https://harbor.test"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "harbor-rest-2.x", "v2.11.0", "db_auth", "harbor.test"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "session expired"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/api/v2.0/systeminfo",
		Error:      &errMsg,
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "session expired"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintHealthHumanFormat(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"status":"healthy","components":[{"name":"database","status":"healthy"},{"name":"redis","status":"healthy"}]}`),
	}
	var buf bytes.Buffer
	printHealth(&buf, r)
	out := buf.String()
	for _, want := range []string{"healthy", "database", "redis"} {
		if !strings.Contains(out, want) {
			t.Errorf("printHealth missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintProjectList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"name":"library","owner_name":"admin","repo_count":5,"metadata":{"public":"true"}},{"name":"dev","owner_name":"bob","repo_count":2,"metadata":{"public":"false"}}]`),
		DurationMs: 10.0,
	}
	var buf bytes.Buffer
	printProjectList(&buf, r)
	out := buf.String()
	for _, want := range []string{"library", "admin", "dev", "bob"} {
		if !strings.Contains(out, want) {
			t.Errorf("printProjectList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintProjectListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`[]`)}
	var buf bytes.Buffer
	printProjectList(&buf, r)
	if !strings.Contains(buf.String(), "(0 projects)") {
		t.Errorf("empty list should announce 0 projects; got:\n%s", buf.String())
	}
}

func TestPrintRepositoryList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"name":"library/ubuntu","artifact_count":12,"pull_count":1000}]`),
		DurationMs: 8.0,
	}
	var buf bytes.Buffer
	printRepositoryList(&buf, r)
	out := buf.String()
	for _, want := range []string{"library/ubuntu", "12", "1000"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRepositoryList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintRobotList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"id":1,"name":"robot$myproject+ci-push","disable":false,"expires_at":-1},{"id":2,"name":"robot$dev+deploy","disable":true,"expires_at":1800000000}]`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printRobotList(&buf, r)
	out := buf.String()
	for _, want := range []string{"robot$myproject+ci-push", "never", "robot$dev+deploy", "false"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRobotList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintRobotListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`[]`)}
	var buf bytes.Buffer
	printRobotList(&buf, r)
	if !strings.Contains(buf.String(), "(0 robot accounts)") {
		t.Errorf("empty list should announce 0 robot accounts; got:\n%s", buf.String())
	}
}

func TestPrintRobotCreate(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"id":42,"name":"robot$myproject+ci-push","secret":"s3cr3t"}`),
		DurationMs: 15.0,
	}
	var buf bytes.Buffer
	printRobotCreate(&buf, r)
	out := buf.String()
	for _, want := range []string{"42", "robot$myproject+ci-push", "s3cr3t", "store the secret now"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRobotCreate missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List Harbor projects"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/api/v2.0/projects", Summary: &summary, FusedScore: 0.987},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list projects", r)
	out := buf.String()
	for _, want := range []string{"harbor-rest-2.x", "list projects", "1 hit(s)", "GET:/api/v2.0/projects", "List Harbor projects"} {
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

// TestDispatchOpBakesConnectorID — pins that connector_id="harbor-rest-2.x"
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
			if body.ConnectorID != "harbor-rest-2.x" {
				t.Errorf("connector_id: got %q want harbor-rest-2.x", body.ConnectorID)
			}
			if body.OpID != "GET:/api/v2.0/systeminfo" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/api/v2.0/systeminfo",
				Result: json.RawMessage(`{"harbor_version":"v2.11.0","auth_mode":"db_auth"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "GET:/api/v2.0/systeminfo", "prod-harbor", nil)
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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "GET:/api/v2.0/systeminfo"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/api/v2.0/systeminfo", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchProjectInfoSendsParams — project info passes project_name in params.
func TestDispatchProjectInfoSendsParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			projectName, _ := body.Params["project_name"].(string)
			if projectName != "library" {
				t.Errorf("project_name: got %q want %q", projectName, "library")
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	const opID = "GET:/api/v2.0/projects/{project_name}"
	params := map[string]any{"project_name": "library"}
	if _, err := dispatchOp(context.Background(), srv.URL, opID, "prod-harbor", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchRobotCreateSendsAllParams — robot create passes name/project/duration.
func TestDispatchRobotCreateSendsAllParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "harbor.robot.create" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			name, _ := body.Params["name"].(string)
			project, _ := body.Params["project"].(string)
			duration, _ := body.Params["duration"].(float64)
			if name != "ci-push" {
				t.Errorf("name: got %q", name)
			}
			if project != "myproject" {
				t.Errorf("project: got %q", project)
			}
			if int(duration) != 90 {
				t.Errorf("duration: got %v", duration)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "harbor.robot.create",
				Result: json.RawMessage(`{"id":42,"name":"robot$myproject+ci-push","secret":"s3cr3t"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"name": "ci-push", "project": "myproject", "duration": 90}
	r, err := dispatchOp(context.Background(), srv.URL, "harbor.robot.create", "prod-harbor", params)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("status: %s", r.Status)
	}
}

// TestErrOpErrorIsSentinel — pins the exported sentinel.
func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
	}
}
