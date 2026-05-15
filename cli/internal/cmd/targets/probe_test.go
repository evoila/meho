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
)

// TestBuildProbePathSimpleName — operator-typical names produce a
// clean POST path.
func TestBuildProbePathSimpleName(t *testing.T) {
	if got := buildProbePath("rdc-vcenter"); got != "/api/v1/targets/rdc-vcenter/probe" {
		t.Fatalf("probe path: got %q", got)
	}
}

// TestBuildProbePathEscapesSpecial — names with slashes / spaces are
// path-escaped so the URL stays a single segment.
func TestBuildProbePathEscapesSpecial(t *testing.T) {
	if got := buildProbePath("a/b c"); got != "/api/v1/targets/a%2Fb%20c/probe" {
		t.Fatalf("escape: got %q", got)
	}
}

// TestRunProbeRejectsEmptyQuery — defensive guard against the
// argv-empty case that ExactArgs(1) lets through.
func TestRunProbeRejectsEmptyQuery(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runProbe(cmd, probeOptions{Query: ""})
	if err == nil {
		t.Fatalf("expected error for empty query")
	}
	if !strings.Contains(stderr.String(), "non-empty") {
		t.Errorf("expected non-empty hint; got %q", stderr.String())
	}
}

// TestPrintFingerprintRendersAllFields — happy-path render lays out
// vendor / product / version / build / edition / reachable /
// probed_at / probe_method on separate lines.
func TestPrintFingerprintRendersAllFields(t *testing.T) {
	ver := "9.0.0"
	build := "12345"
	edition := "enterprise"
	fp := &FingerprintResult{
		Vendor:      "vmware",
		Product:     "vcenter",
		Version:     &ver,
		Build:       &build,
		Edition:     &edition,
		Reachable:   true,
		ProbedAt:    "2026-05-15T08:00:00Z",
		ProbeMethod: "rest",
		Extras:      map[string]any{"datacenter_count": float64(2)},
	}
	var buf bytes.Buffer
	printFingerprint(&buf, fp)
	out := buf.String()
	for _, want := range []string{
		"vendor:", "vmware",
		"product:", "vcenter",
		"version:", "9.0.0",
		"build:", "12345",
		"edition:", "enterprise",
		"reachable:", "true",
		"probed_at:", "2026-05-15T08:00:00Z",
		"probe_method:", "rest",
		"extras:", "datacenter_count=2",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printFingerprint missing %q in %q", want, out)
		}
	}
}

// TestPrintFingerprintOmitsNilOptionals — nil version / build /
// edition lines should not appear so operators don't see a wall of
// empty fields when the connector reported only the required set.
func TestPrintFingerprintOmitsNilOptionals(t *testing.T) {
	fp := &FingerprintResult{
		Vendor:      "vmware",
		Product:     "vcenter",
		Reachable:   true,
		ProbedAt:    "2026-05-15T08:00:00Z",
		ProbeMethod: "rest",
	}
	var buf bytes.Buffer
	printFingerprint(&buf, fp)
	out := buf.String()
	for _, mustNot := range []string{"version:", "build:", "edition:"} {
		if strings.Contains(out, mustNot) {
			t.Errorf("expected %q omitted when nil; got %q", mustNot, out)
		}
	}
}

// TestRunProbeHappyPath — backend returns a FingerprintResult; CLI
// renders it. Confirms the post-#477 contract (FingerprintResult,
// not ProbeResult).
func TestRunProbeHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/rdc-vcenter/probe", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			t.Errorf("expected POST; got %s", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		ver := "9.0.0"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(FingerprintResult{
			Vendor:      "vmware",
			Product:     "vcenter",
			Version:     &ver,
			Reachable:   true,
			ProbedAt:    "2026-05-15T08:00:00Z",
			ProbeMethod: "rest",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runProbe(cmd, probeOptions{Query: "rdc-vcenter", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runProbe: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"vendor:", "vmware", "product:", "vcenter", "9.0.0", "reachable:", "true"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("probe stdout missing %q in %q", want, stdout.String())
		}
	}
}

// TestRunProbeJSONRoundTrip — --json emits the raw envelope; verify
// it parses back to a FingerprintResult unchanged.
func TestRunProbeJSONRoundTrip(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/rdc-vcenter/probe", func(w http.ResponseWriter, _ *http.Request) {
		ver := "9.0.0"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(FingerprintResult{
			Vendor:      "vmware",
			Product:     "vcenter",
			Version:     &ver,
			Reachable:   true,
			ProbedAt:    "2026-05-15T08:00:00Z",
			ProbeMethod: "rest",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runProbe(cmd, probeOptions{Query: "rdc-vcenter", JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runProbe --json: %v", err)
	}
	var decoded FingerprintResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.Vendor != "vmware" || strDeref(decoded.Version) != "9.0.0" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestRunProbe501RendersGracefulMessage — backend returns 501 when
// the target's product has no connector yet; CLI must surface the
// backend detail string + a G3 pointer so operators know what's
// missing.
func TestRunProbe501RendersGracefulMessage(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/rdc-vcenter/probe", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotImplemented)
		fmt.Fprint(w, `{"detail":"no connector registered for product='vcenter'"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runProbe(cmd, probeOptions{Query: "rdc-vcenter", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 501")
	}
	for _, want := range []string{"no connector registered", "vcenter", "Goal G3", "unexpected_response"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("501 stderr missing %q in %q", want, stderr.String())
		}
	}
}

// TestRunProbe500SurfacesConnectorException — per #477's accepted
// trade-off, a connector that raises propagates as a 500. CLI must
// surface the underlying detail so the operator can act on it
// (retry / file bug / check connectivity) rather than seeing an
// opaque "something failed".
func TestRunProbe500SurfacesConnectorException(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/rdc-vcenter/probe", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprint(w, `{"detail":"Internal Server Error"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runProbe(cmd, probeOptions{Query: "rdc-vcenter", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 500")
	}
	if !strings.Contains(stderr.String(), "HTTP 500") {
		t.Errorf("expected HTTP 500 surfaced in stderr; got %q", stderr.String())
	}
}
