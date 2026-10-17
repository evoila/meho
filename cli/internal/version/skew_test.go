// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package version

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func strPtr(s string) *string { return &s }

func TestSkewWarning(t *testing.T) {
	cases := []struct {
		name    string
		cli     string
		bp      *string
		wantSub string // "" means no warning
	}{
		{"same minor", "v0.35.0", strPtr("0.35.12"), ""},
		{"cli one minor behind", "v0.34.9", strPtr("0.35.12"), ""},
		{"cli far behind", "v0.12.0", strPtr("0.35.12"), "CLI v0.12.0 vs backplane 0.35.12 — this CLI is too old"},
		{"cli two minors behind", "v0.33.0", strPtr("0.35.0"), "too old"},
		{"cli newer", "v0.36.0", strPtr("0.35.12"), "this CLI is newer than the backplane"},
		{"major differs", "v1.0.0", strPtr("0.35.0"), "newer than the backplane"},
		{"cli release candidate", "v0.35.0-rc.1", strPtr("0.35.0"), ""},
		{"backplane with v prefix", "v0.12.0", strPtr("v0.35.12"), "releases/tag/v0.35.12"},
		{"dev cli", "dev", strPtr("0.35.12"), ""},
		{"no chart version", "v0.12.0", nil, ""},
		{"main branch backplane", "v0.12.0", strPtr("0.1.20260917-abc1234"), ""},
		{"unknown backplane", "v0.12.0", strPtr("unknown"), ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := SkewWarning(tc.cli, Backplane{ChartVersion: tc.bp})
			if tc.wantSub == "" {
				if got != "" {
					t.Fatalf("want no warning, got %q", got)
				}
				return
			}
			if !strings.Contains(got, tc.wantSub) {
				t.Fatalf("want warning containing %q, got %q", tc.wantSub, got)
			}
			if strings.Contains(got, "\n") {
				t.Fatalf("warning must be one line, got %q", got)
			}
		})
	}
}

func TestSkewWarningNamesReleaseURL(t *testing.T) {
	got := SkewWarning("v0.12.0", Backplane{ChartVersion: strPtr("0.35.12")})
	want := ReleaseURLBase + "v0.35.12"
	if !strings.HasSuffix(got, want) {
		t.Fatalf("want warning ending in %q, got %q", want, got)
	}
}

func TestFetchBackplane(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/version" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"git_sha":"abc","build_date":"2026-09-01T00:00:00Z","chart_version":"0.35.12"}`))
	}))
	defer srv.Close()

	bp, err := FetchBackplane(context.Background(), srv.Client(), srv.URL)
	if err != nil {
		t.Fatalf("FetchBackplane: %v", err)
	}
	if bp.ChartVersion == nil || *bp.ChartVersion != "0.35.12" || bp.GitSHA != "abc" {
		t.Fatalf("unexpected decode: %+v", bp)
	}
}

func TestFetchBackplaneNullChartVersion(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"git_sha":"unknown","build_date":"unknown","chart_version":null}`))
	}))
	defer srv.Close()

	bp, err := FetchBackplane(context.Background(), srv.Client(), srv.URL)
	if err != nil {
		t.Fatalf("FetchBackplane: %v", err)
	}
	if bp.ChartVersion != nil {
		t.Fatalf("want nil chart_version, got %q", *bp.ChartVersion)
	}
}

func TestFetchBackplaneNon200(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	defer srv.Close()

	if _, err := FetchBackplane(context.Background(), srv.Client(), srv.URL); err == nil {
		t.Fatal("want error on HTTP 404")
	}
}
