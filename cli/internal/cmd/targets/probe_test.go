// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func probeOKBody() []byte {
	b, _ := json.Marshal(map[string]any{
		"ok":         true,
		"reason":     nil,
		"latency_ms": 12.5,
		"probed_at":  "2026-01-01T00:00:00Z",
	})
	return b
}

func probeFailedBody() []byte {
	b, _ := json.Marshal(map[string]any{
		"ok":         false,
		"reason":     "connection refused",
		"latency_ms": nil,
		"probed_at":  "2026-01-01T00:00:00Z",
	})
	return b
}

func fakeProbeServer(t *testing.T, name string, body []byte, status int) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/targets/"+name+"/probe", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(body)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv.URL
}

func TestProbe_HumanHappyPath(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeProbeServer(t, "alpha", probeOKBody(), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, stderr, err := runCobraCmd(t, newProbeCmd(), "alpha")
	if err != nil {
		t.Fatalf("probe returned error: %v\nstderr:\n%s", err, stderr)
	}
	out := stdout.String()
	if !strings.Contains(out, "ok") {
		t.Errorf("expected 'ok' in output, got:\n%s", out)
	}
	if strings.Contains(out, jwtMarker) {
		t.Errorf("JWT marker leaked into stdout:\n%s", out)
	}
}

func TestProbe_JSONOutput(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeProbeServer(t, "alpha", probeOKBody(), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, _, err := runCobraCmd(t, newProbeCmd(), "alpha", "--json")
	if err != nil {
		t.Fatalf("probe --json returned error: %v", err)
	}
	var decoded map[string]any
	if jerr := json.Unmarshal([]byte(strings.TrimSpace(stdout.String())), &decoded); jerr != nil {
		t.Fatalf("not valid JSON: %v\n%s", jerr, stdout)
	}
	if decoded["ok"] != true {
		t.Errorf("expected ok=true, got %v", decoded["ok"])
	}
}

func TestProbe_FailedProbe_StillReturnsOK(t *testing.T) {
	// A failed probe (ok=false) is a 200 response; the connector
	// reported the target is unreachable, not that the route failed.
	xdg := withTempXDG(t)
	url := fakeProbeServer(t, "alpha", probeFailedBody(), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, _, err := runCobraCmd(t, newProbeCmd(), "alpha")
	if err != nil {
		t.Fatalf("probe returned error on failed probe: %v", err)
	}
	if !strings.Contains(stdout.String(), "failed") {
		t.Errorf("expected 'failed' status in output, got:\n%s", stdout)
	}
}

func TestProbe_501_PrintsFriendlyMessage(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeProbeServer(t, "alpha",
		[]byte(`{"detail":"no connector registered for product=\"rke2\""}`),
		http.StatusNotImplemented)
	seedCreds(t, xdg, url)

	_, stderr, err := runCobraCmd(t, newProbeCmd(), "alpha")
	if err == nil {
		t.Fatal("expected error on 501")
	}
	se := stderr.String()
	if !strings.Contains(se, "connector") {
		t.Errorf("expected connector message in stderr, got: %q", se)
	}
	if !strings.Contains(se, "G3") {
		t.Errorf("expected G3 reference in stderr, got: %q", se)
	}
}

func TestProbe_NotFound(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeProbeServer(t, "no-such",
		[]byte(`{"detail":"no_target"}`),
		http.StatusNotFound)
	seedCreds(t, xdg, url)

	_, _, err := runCobraCmd(t, newProbeCmd(), "no-such")
	if err == nil {
		t.Fatal("expected error on 404")
	}
}

func TestProbe_NoCreds(t *testing.T) {
	_ = withTempXDG(t)
	_, stderr, err := runCobraCmd(t, newProbeCmd(), "alpha")
	if err == nil {
		t.Fatal("expected error for no-creds path")
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint, got: %q", stderr)
	}
}
