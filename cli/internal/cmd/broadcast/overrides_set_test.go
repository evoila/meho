// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestRunOverridesSetPOSTsCreateRequest -- the request body shape
// matches the BroadcastOverrideCreate Pydantic model.
func TestRunOverridesSetPOSTsCreateRequest(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		var req CreateRequest
		if err := json.Unmarshal(body, &req); err != nil {
			t.Fatalf("decode body: %v\n%s", err, body)
		}
		if req.OpIDPattern != "k8s.configmap.info" {
			t.Errorf("op_id_pattern: got %q; want k8s.configmap.info", req.OpIDPattern)
		}
		if req.ScopeField == nil || *req.ScopeField != "namespace" {
			t.Errorf("scope_field: got %v; want \"namespace\"", req.ScopeField)
		}
		if req.ScopeValue == nil || *req.ScopeValue != "kube-system" {
			t.Errorf("scope_value: got %v; want \"kube-system\"", req.ScopeValue)
		}
		if req.Detail != "aggregate" {
			t.Errorf("detail: got %q; want aggregate", req.Detail)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"11111111-1111-1111-1111-111111111111",` +
			`"tenant_id":"22222222-2222-2222-2222-222222222222",` +
			`"op_id_pattern":"k8s.configmap.info","scope_field":"namespace",` +
			`"scope_value":"kube-system","detail":"aggregate",` +
			`"created_by_sub":"op-admin",` +
			`"created_at":"2026-05-19T12:00:00Z","updated_at":"2026-05-19T12:00:00Z"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runOverridesSet(cmd, overridesSetOptions{
		OpIDPattern:       "k8s.configmap.info",
		ScopeField:        "namespace",
		ScopeValue:        "kube-system",
		Detail:            "aggregate",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesSet: %v", err)
	}
	if !strings.Contains(stdout.String(), "k8s.configmap.info") {
		t.Errorf("summary missing op_id_pattern: %q", stdout.String())
	}
}

// TestRunOverridesSetOpWideOmitsScopeFields -- when --scope-field /
// --scope-value are both empty, the request body omits them entirely
// (so Pydantic's model_validator sees None for both, not "" + "").
func TestRunOverridesSetOpWideOmitsScopeFields(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if strings.Contains(string(body), `"scope_field"`) {
			t.Errorf("op-wide POST should not send scope_field key: %s", body)
		}
		if strings.Contains(string(body), `"scope_value"`) {
			t.Errorf("op-wide POST should not send scope_value key: %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"11111111-1111-1111-1111-111111111111",` +
			`"tenant_id":"22222222-2222-2222-2222-222222222222",` +
			`"op_id_pattern":"vault.kv.*","scope_field":null,"scope_value":null,` +
			`"detail":"aggregate","created_by_sub":"op-admin",` +
			`"created_at":"2026-05-19T12:00:00Z","updated_at":"2026-05-19T12:00:00Z"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runOverridesSet(cmd, overridesSetOptions{
		OpIDPattern:       "vault.kv.*",
		Detail:            "aggregate",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesSet: %v", err)
	}
}

// TestRunOverridesSetHalfSetScopeRejectedClientSide -- the CLI
// validates the scope pair before issuing the HTTP request so the
// operator gets an immediate, clear error message rather than a 422
// round-trip.
func TestRunOverridesSetHalfSetScopeRejectedClientSide(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runOverridesSet(cmd, overridesSetOptions{
		OpIDPattern:       "vault.kv.*",
		ScopeField:        "namespace",
		// ScopeValue intentionally empty
		Detail:            "aggregate",
		BackplaneOverride: "http://unreached.test",
	})
	// The runner returns nil even when it renders an error envelope
	// (the StructuredError shape goes to stderr); the test verifies
	// stderr contains the expected message.
	if err != nil {
		// The runner may surface the structured-error return; either
		// outcome is acceptable as long as the operator sees the
		// rejection.
		_ = err
	}
	if !strings.Contains(stderr.String(), "both be set or both be omitted") {
		t.Errorf("stderr should explain the scope-pair rule: %q", stderr.String())
	}
}

// TestRunOverridesSetJSON -- --json emits the created Entry as JSON.
func TestRunOverridesSetJSON(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/broadcast/overrides", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"11111111-1111-1111-1111-111111111111",` +
			`"tenant_id":"22222222-2222-2222-2222-222222222222",` +
			`"op_id_pattern":"vault.kv.*","scope_field":null,"scope_value":null,` +
			`"detail":"full","created_by_sub":"op-admin",` +
			`"created_at":"2026-05-19T12:00:00Z","updated_at":"2026-05-19T12:00:00Z"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runOverridesSet(cmd, overridesSetOptions{
		OpIDPattern:       "vault.kv.*",
		Detail:            "full",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runOverridesSet --json: %v", err)
	}
	var decoded Entry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.Detail != "full" {
		t.Errorf("decoded detail: got %q; want full", decoded.Detail)
	}
}

// TestNewOverridesSetCmdMarksRequiredFlags -- --op-id-pattern and
// --detail are mandatory; cobra prints a clear "required flag(s)
// not set" error if either is missing.
func TestNewOverridesSetCmdMarksRequiredFlags(t *testing.T) {
	cmd := newOverridesSetCmd()
	for _, name := range []string{"op-id-pattern", "detail"} {
		ann := cmd.Flag(name).Annotations
		required, ok := ann[cobraRequiredAnnotation]
		if !ok || len(required) == 0 || required[0] != "true" {
			t.Errorf("flag --%s should be marked required", name)
		}
	}
}

// cobraRequiredAnnotation is the constant cobra uses internally to
// mark a flag required (see spf13/cobra's
// `command.go::MarkFlagRequired`). Exposed via the Flag.Annotations
// map; pinning the constant string here keeps the test independent
// of cobra internals.
const cobraRequiredAnnotation = "cobra_annotation_bash_completion_one_required_flag"
