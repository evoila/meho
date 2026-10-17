// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/version"
)

// withCLIVersion pins the CLI build version for one test.
func withCLIVersion(t *testing.T, v string) {
	t.Helper()
	prev := version.Version
	version.Version = v
	t.Cleanup(func() { version.Version = prev })
}

// fakeVersionedBackplane serves /api/v1/health (happy path) and
// /version with the given chart_version, like a current backplane.
func fakeVersionedBackplane(t *testing.T, chartVersion string) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(healthBody())
	})
	mux.HandleFunc("/version", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(mustJSON(map[string]any{
			"git_sha": "0123456789abcdef", "build_date": "2026-09-23T00:00:00Z", "chart_version": chartVersion,
		}))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv.URL
}

func seedStatusCreds(t *testing.T, url string) {
	t.Helper()
	xdg := withTempXDG(t)
	seedCreds(t, xdg, url, auth.StoredToken{
		BackplaneURL: url,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})
}

// TestStatus_WarnsOnVersionSkew — an old CLI against a current
// backplane prints the one-line skew warning (with the release to
// install) on stderr, and the health summary still prints.
func TestStatus_WarnsOnVersionSkew(t *testing.T) {
	withCLIVersion(t, "v0.12.0")
	url := fakeVersionedBackplane(t, "0.35.12")
	seedStatusCreds(t, url)

	stdout, stderr, err := runStatus(t)
	if err != nil {
		t.Fatalf("status returned error: %v\nstderr:\n%s", err, stderr.String())
	}
	want := "warning: CLI v0.12.0 vs backplane 0.35.12 — this CLI is too old for this backplane. Upgrade: " +
		version.ReleaseURLBase + "v0.35.12\n"
	if stderr.String() != want {
		t.Errorf("stderr = %q, want %q", stderr.String(), want)
	}
	if !strings.Contains(stdout.String(), "Logged in as alice@example.com") {
		t.Errorf("health summary missing from stdout:\n%s", stdout.String())
	}
}

// TestStatus_JSON_SkewWarningStaysOffStdout — under --json the warning
// goes to stderr so stdout stays one valid JSON document.
func TestStatus_JSON_SkewWarningStaysOffStdout(t *testing.T) {
	withCLIVersion(t, "v0.12.0")
	url := fakeVersionedBackplane(t, "0.35.12")
	seedStatusCreds(t, url)

	stdout, stderr, err := runStatus(t, "--json")
	if err != nil {
		t.Fatalf("status --json returned error: %v\nstderr:\n%s", err, stderr.String())
	}
	var decoded map[string]any
	if jerr := json.Unmarshal(stdout.Bytes(), &decoded); jerr != nil {
		t.Fatalf("--json stdout is not valid JSON: %v\n%s", jerr, stdout.String())
	}
	if !strings.Contains(stderr.String(), "CLI v0.12.0 vs backplane 0.35.12") {
		t.Errorf("skew warning missing from stderr: %q", stderr.String())
	}
}

// TestStatus_NoWarningWithinSupportedSkew — same minor or N-1 is quiet.
func TestStatus_NoWarningWithinSupportedSkew(t *testing.T) {
	for _, cli := range []string{"v0.35.0", "v0.34.2"} {
		t.Run(cli, func(t *testing.T) {
			withCLIVersion(t, cli)
			url := fakeVersionedBackplane(t, "0.35.12")
			seedStatusCreds(t, url)

			_, stderr, err := runStatus(t)
			if err != nil {
				t.Fatalf("status returned error: %v", err)
			}
			if stderr.Len() != 0 {
				t.Errorf("want no warning, got %q", stderr.String())
			}
		})
	}
}

// TestPrintVersionSkew_IgnoresMissingRoute — a backplane without a
// usable /version (404) never breaks login or status: no output.
func TestPrintVersionSkew_IgnoresMissingRoute(t *testing.T) {
	withCLIVersion(t, "v0.12.0")
	srv := httptest.NewServer(http.NotFoundHandler())
	t.Cleanup(srv.Close)

	var buf bytes.Buffer
	printVersionSkew(context.Background(), srv.Client(), srv.URL, &buf)
	if buf.Len() != 0 {
		t.Errorf("want no output, got %q", buf.String())
	}
}
