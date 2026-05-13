// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func fullTargetBody(name string) []byte {
	t := map[string]any{
		"id": "aaa", "tenant_id": "ttt",
		"name": name, "aliases": []string{"a-alias"},
		"product": "rke2", "host": "10.0.0.1",
		"auth_model": "shared_service_account",
		"vpn_required": false, "extras": map[string]any{},
		"created_at": "2026-01-01T00:00:00Z",
		"updated_at": "2026-01-01T00:00:00Z",
	}
	b, _ := json.Marshal(t)
	return b
}

func notFoundBody(query string) []byte {
	envelope := map[string]any{
		"detail": map[string]any{
			"error": "no_target",
			"query": query,
			"matches": []map[string]any{
				{"id": "xx", "name": "alpha-prod", "aliases": []string{}, "product": "rke2", "host": "10.0.0.1"},
			},
		},
	}
	b, _ := json.Marshal(envelope)
	return b
}

func fakeDescribeServer(t *testing.T, name string, body []byte, status int) string {
	t.Helper()
	return fakeServer(t, "/api/v1/targets/"+name, jsonHandler(body, status))
}

func TestDescribe_HumanHappyPath(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeDescribeServer(t, "alpha", fullTargetBody("alpha"), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, stderr, err := runCobraCmd(t, newDescribeCmd(), "alpha")
	if err != nil {
		t.Fatalf("describe returned error: %v\nstderr:\n%s", err, stderr)
	}
	out := stdout.String()
	for _, want := range []string{"Name:", "alpha", "Product:", "rke2", "Host:"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in output:\n%s", want, out)
		}
	}
	if strings.Contains(out, jwtMarker) {
		t.Errorf("JWT marker leaked into stdout:\n%s", out)
	}
}

func TestDescribe_JSONOutput(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeDescribeServer(t, "alpha", fullTargetBody("alpha"), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, _, err := runCobraCmd(t, newDescribeCmd(), "alpha", "--json")
	if err != nil {
		t.Fatalf("describe --json returned error: %v", err)
	}
	var decoded map[string]any
	if jerr := json.Unmarshal([]byte(strings.TrimSpace(stdout.String())), &decoded); jerr != nil {
		t.Fatalf("not valid JSON: %v\n%s", jerr, stdout)
	}
	if decoded["name"] != "alpha" {
		t.Errorf("expected name=alpha, got %v", decoded["name"])
	}
}

func TestDescribe_NotFound_ShowsNearMisses(t *testing.T) {
	xdg := withTempXDG(t)
	url := fakeDescribeServer(t, "alph", notFoundBody("alph"), http.StatusNotFound)
	seedCreds(t, xdg, url)

	_, stderr, err := runCobraCmd(t, newDescribeCmd(), "alph")
	if err == nil {
		t.Fatal("expected error on 404")
	}
	se := stderr.String()
	if !strings.Contains(se, "not found") {
		t.Errorf("expected 'not found' in stderr, got: %q", se)
	}
	if !strings.Contains(se, "alpha-prod") {
		t.Errorf("expected near-miss 'alpha-prod' in stderr, got: %q", se)
	}
}

func TestDescribe_AliasResolution_WorksTransparently(t *testing.T) {
	xdg := withTempXDG(t)
	// The resolver on the backplane handles alias → canonical name;
	// the CLI just passes the arg verbatim in the URL path.
	url := fakeDescribeServer(t, "a-alias", fullTargetBody("alpha"), http.StatusOK)
	seedCreds(t, xdg, url)

	stdout, _, err := runCobraCmd(t, newDescribeCmd(), "a-alias")
	if err != nil {
		t.Fatalf("describe via alias returned error: %v", err)
	}
	if !strings.Contains(stdout.String(), "alpha") {
		t.Errorf("expected alpha in output, got:\n%s", stdout)
	}
}

func TestDescribe_NoCreds(t *testing.T) {
	_ = withTempXDG(t)
	_, stderr, err := runCobraCmd(t, newDescribeCmd(), "alpha")
	if err == nil {
		t.Fatal("expected error for no-creds path")
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint, got: %q", stderr)
	}
}
