// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunDescribeRejectsEmptyQuery — cobra's ExactArgs(1) blocks the
// command-line zero-arg case; the runner still must reject empty
// strings (e.g. the operator wrote `meho targets describe ""`).
func TestRunDescribeRejectsEmptyQuery(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runDescribe(cmd, describeOptions{Query: ""})
	if err == nil {
		t.Fatalf("expected error for empty query")
	}
	if !strings.Contains(stderr.String(), "non-empty") {
		t.Errorf("stderr missing non-empty hint; got %q", stderr.String())
	}
}

// TestPrintTargetSummaryHappyPath — full target with every optional
// field set renders the canonical key-value summary.
func TestPrintTargetSummaryHappyPath(t *testing.T) {
	port := 443
	fqdn := "vc.example.com"
	secret := "vault://vsphere/rdc"
	notes := "primary"
	preferred := "vsphere-rest-9.0"
	fingerprint := map[string]interface{}{
		"vendor":       "vmware",
		"product":      "vcenter",
		"version":      "9.0.0",
		"reachable":    true,
		"probed_at":    "2026-05-15T08:00:00Z",
		"probe_method": "rest",
	}
	tgt := &api.Target{
		Id:              mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		TenantId:        mustUUID(t, "22222222-2222-2222-2222-222222222222"),
		Name:            "rdc-vcenter",
		Aliases:         []string{"vc-prod"},
		Product:         "vcenter",
		Host:            "vc.example",
		Port:            &port,
		Fqdn:            &fqdn,
		SecretRef:       &secret,
		AuthModel:       "shared_service_account",
		VpnRequired:     true,
		Notes:           &notes,
		PreferredImplId: &preferred,
		Fingerprint:     &fingerprint,
		Extras: map[string]interface{}{
			"region":   "eu-central",
			"replicas": float64(3),
		},
		CreatedAt: time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC),
		UpdatedAt: time.Date(2026, 5, 15, 8, 0, 0, 0, time.UTC),
	}
	var buf bytes.Buffer
	printTargetSummary(&buf, tgt)
	out := buf.String()
	for _, want := range []string{
		"name:", "rdc-vcenter",
		"aliases:", "vc-prod",
		"product:", "vcenter",
		"host:", "vc.example",
		"port:", "443",
		"fqdn:", "vc.example.com",
		"auth_model:", "shared_service_account",
		"vpn_required:", "true",
		"preferred_impl_id:", "vsphere-rest-9.0",
		"fingerprint:", "vmware/vcenter", "9.0.0", "reachable=true",
		"extras:", "region=eu-central", "replicas=3",
		"notes:", "primary",
		"created_at:", "2026-05-01T00:00:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printTargetSummary missing %q in %q", want, out)
		}
	}
}

// TestPrintTargetSummaryNoFingerprint — never-probed target renders
// the "(none — never probed)" placeholder for fingerprint and a
// dashed placeholder for preferred_impl_id, so operators see the
// difference between "field absent" and "field empty".
func TestPrintTargetSummaryNoFingerprint(t *testing.T) {
	tgt := &api.Target{
		Name:        "fresh-target",
		Aliases:     nil,
		AuthModel:   "shared_service_account",
		Fingerprint: nil,
	}
	var buf bytes.Buffer
	printTargetSummary(&buf, tgt)
	out := buf.String()
	if !strings.Contains(out, "fingerprint:") {
		t.Errorf("expected fingerprint line; got %q", out)
	}
	if !strings.Contains(out, "never probed") {
		t.Errorf("expected never-probed hint; got %q", out)
	}
	if !strings.Contains(out, "preferred_impl_id:") || !strings.Contains(out, "preferred_impl_id: -") {
		t.Errorf("expected dashed preferred_impl_id line; got %q", out)
	}
	if !strings.Contains(out, "aliases:") {
		t.Errorf("expected aliases line; got %q", out)
	}
}

// TestFormatFingerprintNilEmpty — nil / empty map renders the
// no-probe placeholder; tests the boundary of the human render.
func TestFormatFingerprintNilEmpty(t *testing.T) {
	if got := formatFingerprint(nil); !strings.Contains(got, "never probed") {
		t.Errorf("nil fp: got %q", got)
	}
	empty := map[string]interface{}{}
	if got := formatFingerprint(&empty); !strings.Contains(got, "never probed") {
		t.Errorf("empty fp: got %q", got)
	}
}

// TestFormatExtrasSortedDeterministic — the renderer must produce
// the same string for the same input map across runs (no map
// iteration order leak).
func TestFormatExtrasSortedDeterministic(t *testing.T) {
	m := map[string]interface{}{"b": 2, "a": 1, "c": "x"}
	got := formatExtras(m)
	if got != "a=1, b=2, c=x" {
		t.Errorf("non-deterministic extras render: got %q", got)
	}
}

// TestFormatScalarFloatVsInt — JSON decoded ints land as float64;
// the formatter must surface 3 as "3", not "3.000000".
func TestFormatScalarFloatVsInt(t *testing.T) {
	if got := formatScalar(float64(3)); got != "3" {
		t.Errorf("int-shaped float: got %q", got)
	}
	if got := formatScalar(float64(3.5)); got != "3.5" {
		t.Errorf("fractional float: got %q", got)
	}
	if got := formatScalar(true); got != "true" {
		t.Errorf("bool: got %q", got)
	}
	if got := formatScalar([]any{1, 2}); !strings.Contains(got, "[") {
		t.Errorf("array: got %q", got)
	}
}

// TestRunDescribeHappyPath — describe drives end-to-end through the
// auth stack; backend returns a Target shape with fingerprint +
// preferred_impl_id set. Confirms the typed-client path round-trips
// the api.Target envelope.
func TestRunDescribeHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/rdc-vcenter", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		port := 443
		preferred := "vsphere-rest-9.0"
		fp := map[string]interface{}{
			"vendor":       "vmware",
			"product":      "vcenter",
			"version":      "9.0.0",
			"reachable":    true,
			"probed_at":    "2026-05-15T08:00:00Z",
			"probe_method": "rest",
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.Target{
			Id:              mustUUID(t, "11111111-1111-1111-1111-111111111111"),
			TenantId:        mustUUID(t, "22222222-2222-2222-2222-222222222222"),
			Name:            "rdc-vcenter",
			Aliases:         []string{"vc-prod"},
			Product:         "vcenter",
			Host:            "vc.example",
			Port:            &port,
			AuthModel:       "shared_service_account",
			VpnRequired:     false,
			PreferredImplId: &preferred,
			Fingerprint:     &fp,
			Extras:          map[string]interface{}{},
			CreatedAt:       time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC),
			UpdatedAt:       time.Date(2026, 5, 15, 8, 0, 0, 0, time.UTC),
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runDescribe(cmd, describeOptions{Query: "rdc-vcenter", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runDescribe: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"name:", "rdc-vcenter",
		"aliases:", "vc-prod",
		"fingerprint:", "vmware/vcenter", "9.0.0",
		"preferred_impl_id:", "vsphere-rest-9.0",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("describe output missing %q in %q", want, out)
		}
	}
}

// TestRunDescribeAlias — describe accepts an alias and the backend's
// resolver returns the canonical row. The CLI should treat the path
// segment opaquely; resolution happens server-side.
func TestRunDescribeAlias(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/vc-prod", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.Target{
			Id:        mustUUID(t, "11111111-1111-1111-1111-111111111111"),
			TenantId:  mustUUID(t, "22222222-2222-2222-2222-222222222222"),
			Name:      "rdc-vcenter",
			Aliases:   []string{"vc-prod"},
			Product:   "vcenter",
			Host:      "vc.example",
			AuthModel: "shared_service_account",
			Extras:    map[string]interface{}{},
			CreatedAt: time.Now().UTC(),
			UpdatedAt: time.Now().UTC(),
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runDescribe(cmd, describeOptions{Query: "vc-prod", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("describe alias: %v", err)
	}
	if !strings.Contains(stdout.String(), "rdc-vcenter") {
		t.Errorf("alias describe should return canonical name; got %q", stdout.String())
	}
}

// TestRunDescribe404RendersNearMisses — 404 with structured detail
// must surface the candidate names so the operator fixes a typo on
// the next try. Pinned via a literal path so we don't depend on the
// typed-client's path-escape behaviour for the test name.
func TestRunDescribe404RendersNearMisses(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/rdc-vc", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":{"error":"no_target","query":"rdc-vc","matches":[
            {"id":"id1","name":"rdc-vcenter","aliases":["vc-prod"],"product":"vcenter","host":"vc.example"},
            {"id":"id2","name":"rdc-vsphere","aliases":[],"product":"vcenter","host":"vs.example"}
        ]}}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runDescribe(cmd, describeOptions{Query: "rdc-vc", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	for _, want := range []string{"Target not found", "rdc-vcenter", "rdc-vsphere", "did you mean"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr missing %q in %q", want, stderr.String())
		}
	}
}
