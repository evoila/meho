// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
)

// fakeVersionBackplane serves GET /version with the given connector
// packages. A nil list leaves the field out, like an older backplane.
func fakeVersionBackplane(t *testing.T, packages []map[string]any) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/version" {
			http.NotFound(w, r)
			return
		}
		body := map[string]any{
			"git_sha": "0123456789abcdef", "build_date": "2026-10-01T00:00:00Z", "chart_version": "0.40.0",
		}
		if packages != nil {
			body["connector_packages"] = packages
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(mustJSON(body))
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

func runVersion(t *testing.T, argv ...string) (stdout, stderr *bytes.Buffer) {
	t.Helper()
	stdout, stderr = &bytes.Buffer{}, &bytes.Buffer{}
	root := newRootCmd()
	root.SetOut(stdout)
	root.SetErr(stderr)
	root.SetArgs(append([]string{"version"}, argv...))
	if err := root.Execute(); err != nil {
		t.Fatalf("execute version: %v (stderr: %q)", err, stderr.String())
	}
	return stdout, stderr
}

func TestVersion_PrintsBackplaneConnectorPackages(t *testing.T) {
	withTempXDG(t)
	url := fakeVersionBackplane(t, []map[string]any{{
		"name": "example-connectors", "version": "1.2.0",
		"wheel_digest": "sha256:" + strings.Repeat("a", 64), "folders": []string{"example_widget"},
	}})

	stdout, stderr := runVersion(t, "--backplane", url)

	out := stdout.String()
	for _, want := range []string{
		"backplane 0.40.0 (commit 0123456789abcdef, built 2026-10-01T00:00:00Z) at " + url + "\n",
		"Connector packages:\n",
		"  example-connectors 1.2.0 (sha256:" + strings.Repeat("a", 64) + ")\n",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("stdout missing %q:\n%s", want, out)
		}
	}
	if stderr.Len() != 0 {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
}

func TestVersion_StoredBackplaneWithNoPackagesPrintsNone(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeVersionBackplane(t, []map[string]any{})
	if err := auth.SaveConfigAt(filepath.Join(xdg, "meho", "config.json"),
		auth.Config{BackplaneURL: url}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}

	stdout, _ := runVersion(t)

	if !strings.HasSuffix(stdout.String(), "Connector packages: none\n") {
		t.Fatalf("want the empty list, got:\n%s", stdout.String())
	}
}

func TestVersion_OlderBackplaneWithoutTheFieldPrintsNoList(t *testing.T) {
	withTempXDG(t)
	url := fakeVersionBackplane(t, nil)

	stdout, _ := runVersion(t, "--backplane", url)

	if strings.Contains(stdout.String(), "Connector packages") {
		t.Fatalf("an older backplane sends no list; got:\n%s", stdout.String())
	}
	if !strings.Contains(stdout.String(), "backplane 0.40.0") {
		t.Fatalf("backplane line missing:\n%s", stdout.String())
	}
}

func TestVersion_UnreachableBackplaneWarnsAndSucceeds(t *testing.T) {
	withTempXDG(t)
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()

	stdout, stderr := runVersion(t, "--backplane", url)

	if strings.Count(stdout.String(), "\n") != 1 || !strings.HasPrefix(stdout.String(), "meho ") {
		t.Fatalf("want only the CLI line on stdout, got:\n%s", stdout.String())
	}
	if !strings.Contains(stderr.String(), "warning: could not read the backplane version from "+url) {
		t.Fatalf("want a warning on stderr, got %q", stderr.String())
	}
}
