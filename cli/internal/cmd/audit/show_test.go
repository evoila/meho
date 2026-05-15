// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildShowPathEscapesUUID — UUIDs are URL-safe by spec, but
// pathEscape is the cheap defence against a typo'd argument with a
// slash / space leaking into the URL.
func TestBuildShowPathEscapesUUID(t *testing.T) {
	got := buildShowPath("00000000-0000-0000-0000-000000000001")
	want := "/api/v1/audit/show/00000000-0000-0000-0000-000000000001"
	if got != want {
		t.Errorf("buildShowPath: got %q; want %q", got, want)
	}
}

// TestBuildShowPathHandlesSpecialChars — a typo'd argument with a
// slash gets percent-encoded so the URL doesn't collapse the path
// segment.
func TestBuildShowPathHandlesSpecialChars(t *testing.T) {
	got := buildShowPath("weird/value")
	if !strings.Contains(got, "weird%2Fvalue") {
		t.Errorf("buildShowPath did not URL-encode slash: %q", got)
	}
}

// TestRunShowRequiresAuditIDArg — the empty-argument path goes
// through the same renderError surface as a backplane refusal, so
// operators see one consistent error class.
func TestRunShowRequiresAuditIDArg(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{AuditID: ""})
	if err == nil {
		t.Fatalf("expected error for empty audit-id")
	}
	if !strings.Contains(stderr.String(), "non-empty <audit-id>") {
		t.Errorf("stderr missing arg-required hint: %s", stderr.String())
	}
}

// TestRunShowHappyPath — the verb hits GET /api/v1/audit/show/{id}
// and renders the operator-friendly key-value summary with every
// pinned field.
func TestRunShowHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		expected := "/api/v1/audit/show/11111111-1111-1111-1111-111111111111"
		if r.URL.Path != expected {
			t.Errorf("path: got %q; want %q", r.URL.Path, expected)
		}
		tname := "rdc-vcenter"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(Entry{
			ID:           "11111111-1111-1111-1111-111111111111",
			TS:           "2026-05-13T15:42:11Z",
			PrincipalSub: "damir",
			TargetName:   &tname,
			Method:       "GET",
			Path:         "/api/v1/vsphere/vm/list",
			StatusCode:   200,
			OpID:         "vsphere.vm.list",
			OpClass:      "read",
			ResultStatus: "ok",
			Payload:      map[string]any{"hit_count": float64(7)},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"id:", "11111111-1111-1111-1111-111111111111",
		"principal_sub:", "damir",
		"target_name:", "rdc-vcenter",
		"op_id:", "vsphere.vm.list",
		"op_class:", "read",
		"result_status:", "ok",
		"payload:", "hit_count=7",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("summary missing %q in %s", want, out)
		}
	}
}

// TestRunShow404SurfacesNotFound — the backend returns 404 both
// when an audit_id genuinely doesn't exist and when it exists but
// belongs to another tenant. The CLI surfaces a single message
// rather than two so existence never leaks.
func TestRunShow404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail": "audit row not found"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "audit row not found") {
		t.Errorf("stderr missing not-found hint: %s", stderr.String())
	}
}

// TestRunShow422SurfacesValidationDetail — a malformed UUID
// argument is rejected by FastAPI's validation before reaching the
// handler. The CLI passes the validation envelope through so the
// operator sees what was malformed.
func TestRunShow422SurfacesValidationDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":[{"loc":["path","audit_id"],"msg":"value is not a valid uuid"}]}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "not-a-uuid",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 422")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("stderr missing validation hint: %s", stderr.String())
	}
}

// TestPrintEntrySummaryShowsDashForNullFields — optional
// fields render as "-" so the summary stays grep-friendly across
// rows with different optional-field populations.
func TestPrintEntrySummaryShowsDashForNullFields(t *testing.T) {
	var buf bytes.Buffer
	printEntrySummary(&buf, &Entry{
		ID:           "abc",
		TS:           "2026-05-13T00:00:00Z",
		PrincipalSub: "damir",
		Method:       "GET",
		Path:         "/x",
		StatusCode:   200,
		OpID:         "x.y",
		OpClass:      "read",
		ResultStatus: "ok",
		Payload:      map[string]any{},
	})
	out := buf.String()
	for _, want := range []string{
		"tenant_id:         -",
		"principal_name:    -",
		"target_id:         -",
		"target_name:       -",
		"request_id:        -",
		"duration_ms:       -",
		"parent_audit_id:   -",
		"agent_session_id:  -",
		"broadcast_event_id: -",
		"payload:           -",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("summary missing %q in %s", want, out)
		}
	}
}

// TestFormatPayloadSortedKeys — payload renderer keys are sorted so
// snapshot tests stay deterministic and operator diffs across runs
// don't churn on map-iteration order.
func TestFormatPayloadSortedKeys(t *testing.T) {
	got := formatPayload(map[string]any{
		"zoo":      true,
		"alpha":    1,
		"midpoint": "x",
	})
	// Sorted: alpha, midpoint, zoo
	if !strings.HasPrefix(got, "alpha=") {
		t.Errorf("formatPayload keys not sorted: %s", got)
	}
	if !strings.Contains(got, "midpoint=x") {
		t.Errorf("formatPayload missing middle key: %s", got)
	}
}
