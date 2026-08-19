// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package eventsource

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

const createdJSON = `{
  "id": "11111111-1111-1111-1111-111111111111",
  "tenant_id": "22222222-2222-2222-2222-222222222222",
  "name": "Prod Alertmanager",
  "slug": "prod-am",
  "kind": "alertmanager",
  "auth_strategy": "hmac-sha256",
  "secret_ref": "tenants/22222222-2222-2222-2222-222222222222/event-sources/prod-am",
  "status": "active",
  "extras": {},
  "created_by_sub": "admin-1",
  "created_at": "2026-08-18T12:00:00Z",
  "updated_at": "2026-08-18T12:00:00Z",
  "deleted_at": null
}`

// seedXDGAndToken seeds a fake token store + config pointed at backplaneURL.
func seedXDGAndToken(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
}

func runCmd(t *testing.T, cmd *cobra.Command, stdin string, args ...string) (string, string, error) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	if stdin != "" {
		cmd.SetIn(strings.NewReader(stdin))
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return stdout.String(), stderr.String(), err
}

func TestAddRegisteredWithCreateAlias(t *testing.T) {
	t.Parallel()
	root := NewRootCmd()
	var add *cobra.Command
	for _, c := range root.Commands() {
		if c.Name() == "add" {
			add = c
		}
	}
	if add == nil {
		t.Fatal("`event-source add` not registered")
	}
	found := false
	for _, a := range add.Aliases {
		if a == "create" {
			found = true
		}
	}
	if !found {
		t.Errorf("expected `create` alias; got %v", add.Aliases)
	}
}

func TestAddMissingRequiredFlags(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"missing name", []string{"prod-am", "--kind", "alertmanager", "--auth-strategy", "hmac-sha256"}, "name"},
		{"missing kind", []string{"prod-am", "--name", "N", "--auth-strategy", "hmac-sha256"}, "kind"},
		{"missing auth", []string{"prod-am", "--name", "N", "--kind", "alertmanager"}, "auth-strategy"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, _, err := runCmd(t, newAddCmd(), "", c.args...)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Errorf("expected error naming %q; got %v", c.want, err)
			}
		})
	}
}

func TestAddSuccessSendsBodyWithSecretFromEnv(t *testing.T) {
	var gotBody map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/event-sources", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("want POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(createdJSON))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	t.Setenv(SecretEnvVar, "hmac-signing-key")

	stdout, stderr, err := runCmd(t, newAddCmd(), "",
		"prod-am", "--name", "Prod Alertmanager", "--kind", "alertmanager",
		"--auth-strategy", "hmac-sha256", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr)
	}
	if gotBody["slug"] != "prod-am" || gotBody["name"] != "Prod Alertmanager" ||
		gotBody["kind"] != "alertmanager" || gotBody["auth_strategy"] != "hmac-sha256" {
		t.Errorf("body missing required fields: %v", gotBody)
	}
	if gotBody["secret"] != "hmac-signing-key" {
		t.Errorf("secret should be carried in the body from the env var: %v", gotBody["secret"])
	}
	if _, leaked := gotBody["tenant_id"]; leaked {
		t.Errorf("body must never carry tenant_id: %v", gotBody)
	}
	// Unset optionals are omitted.
	for _, k := range []string{"status", "extras"} {
		if _, present := gotBody[k]; present {
			t.Errorf("unset optional %q should be omitted: %v", k, gotBody)
		}
	}
	// The rendered output shows secret_ref (the path) but never the value.
	if strings.Contains(stdout, "hmac-signing-key") {
		t.Errorf("stdout must not echo the secret value:\n%s", stdout)
	}
	if !strings.Contains(stdout, "prod-am") {
		t.Errorf("stdout should render the created source:\n%s", stdout)
	}
}

func TestListRendersTable(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/event-sources", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[{"name":"AM","slug":"prod-am","kind":"alertmanager","auth_strategy":"hmac-sha256","status":"active","secret_ref":null}],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runCmd(t, newListCmd(), "", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("list: %v\nstderr=%s", err, stderr)
	}
	if !strings.Contains(stdout, "prod-am") || !strings.Contains(stdout, "SLUG") {
		t.Errorf("list should render a table with the slug:\n%s", stdout)
	}
}

func TestDeleteDeclineWithoutConfirm(t *testing.T) {
	t.Parallel()
	// Point --backplane at an unroutable URL; a decline must exit 0 before
	// any HTTP call, so the URL is never dialed.
	stdout, _, err := runCmd(t, newDeleteCmd(), "n\n", "prod-am", "--backplane", "http://127.0.0.1:0")
	if err != nil {
		t.Fatalf("decline should exit 0; got %v", err)
	}
	if !strings.Contains(stdout, "declined") {
		t.Errorf("expected a declined line; got:\n%s", stdout)
	}
}

func TestDescribe404SurfacesError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/event-sources/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":{"error":"no_event_source","slug":"prod-am"}}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	_, stderr, err := runCmd(t, newDescribeCmd(), "", "prod-am", "--backplane", srv.URL)
	if err == nil {
		t.Fatal("expected a non-nil error on 404")
	}
	if !strings.Contains(stderr, "not found") {
		t.Errorf("stderr should surface a not-found message; got:\n%s", stderr)
	}
}
