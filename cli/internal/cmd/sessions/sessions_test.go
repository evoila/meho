// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

func seedToken(t *testing.T, backplaneURL string) {
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
		Expiry:       time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
}

func newCmd(t *testing.T, stdin string) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetIn(strings.NewReader(stdin))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

func summary(id string) map[string]any {
	return map[string]any{
		"session_id": id, "project": "infra", "app": "claude-code", "app_version": nil,
		"uploaded_by_sub": "op-alice", "started_at": nil, "ended_at": nil, "state": "open",
		"outcome": nil, "created_at": "2026-11-01T10:00:00Z", "last_seen_at": "2026-11-02T10:00:00Z",
		"upload_count": 2,
	}
}

func TestListFollowsEveryPageWithAll(t *testing.T) {
	var cursors []string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions", func(w http.ResponseWriter, r *http.Request) {
		cursor := r.URL.Query().Get("cursor")
		cursors = append(cursors, cursor)
		w.Header().Set("Content-Type", "application/json")
		if cursor == "" {
			next := "1"
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{summary("a")}, "next_cursor": next})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{summary("b")}, "next_cursor": nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, stdout, stderr := newCmd(t, "")
	if err := runList(cmd, listOptions{Limit: 1, All: true, JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	var got listResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(got.Items) != 2 || got.NextCursor != nil {
		t.Fatalf("want two sessions and no next cursor, got %+v", got)
	}
	if strings.Join(cursors, ",") != ",1" {
		t.Fatalf("cursors = %q", cursors)
	}
}

func TestListWithoutAllSaysMorePagesExist(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{summary("a")}, "next_cursor": "1"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, stdout, _ := newCmd(t, "")
	if err := runList(cmd, listOptions{Limit: 1, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v", err)
	}
	if !strings.Contains(stdout.String(), "more sessions exist") {
		t.Fatalf("missing the more-pages line: %q", stdout.String())
	}
}

func TestListRefusesAnUnknownState(t *testing.T) {
	cmd, _, stderr := newCmd(t, "")
	if err := runList(cmd, listOptions{State: "done", Limit: 10}); err == nil {
		t.Fatal("want an error")
	}
	if !strings.Contains(stderr.String(), "--state") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestListSendsTheTimeWindow(t *testing.T) {
	var got []string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions", func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.URL.Query().Get("since"), r.URL.Query().Get("until"))
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{summary("inside")}, "next_cursor": nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, stdout, stderr := newCmd(t, "")
	opts := listOptions{
		Since: "2026-11-02T00:00:00Z", Until: "2026-11-09T00:00:00+01:00", Limit: 50, BackplaneOverride: srv.URL,
	}
	if err := runList(cmd, opts); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if strings.Join(got, ",") != "2026-11-02T00:00:00Z,2026-11-09T00:00:00+01:00" {
		t.Fatalf("since,until = %q", got)
	}
	if !strings.Contains(stdout.String(), "inside") {
		t.Fatalf("out = %q", stdout.String())
	}
}

func TestListRefusesABadTimeWindow(t *testing.T) {
	for _, opts := range []listOptions{
		{Since: "2026-11-02T00:00:00", Limit: 10},
		{Until: "last week", Limit: 10},
		{Since: "2026-11-09T00:00:00Z", Until: "2026-11-02T00:00:00Z", Limit: 10},
	} {
		cmd, _, stderr := newCmd(t, "")
		if err := runList(cmd, opts); err == nil {
			t.Fatalf("%+v: want an error", opts)
		}
		if !strings.Contains(stderr.String(), "--since") && !strings.Contains(stderr.String(), "--until") {
			t.Fatalf("stderr = %q", stderr.String())
		}
	}
}

func TestShowWithoutTheRightIsInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions/abc", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"insufficient_role"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, _, stderr := newCmd(t, "")
	if err := runShow(cmd, "abc", false, srv.URL); err == nil {
		t.Fatal("want an error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestShowPrintsFilesAndQuarantine(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions/abc", func(w http.ResponseWriter, _ *http.Request) {
		body := summary("abc")
		body["files"] = map[string]any{"abc.jsonl": map[string]any{"size": 10, "sha256": "f00d"}}
		body["uploads"] = []any{map[string]any{
			"seq": 1, "uploaded_at": "2026-11-02T10:00:00Z", "archive_bytes": 99,
			"archive_sha256": "beef", "quarantined": true, "redactions": 0,
			"files": map[string]any{"abc.jsonl": map[string]any{"offset": 0, "added": 10, "size": 10, "sha256": "f00d"}},
		}}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(body)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, stdout, stderr := newCmd(t, "")
	if err := runShow(cmd, "abc", false, srv.URL); err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"abc.jsonl", "sha256 f00d", "in quarantine", "state:        open"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("missing %q in %q", want, stdout.String())
		}
	}
}

func TestDeleteAsksFirstAndSendsDelete(t *testing.T) {
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions/abc", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("method = %s", r.Method)
		}
		calls++
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, stdout, _ := newCmd(t, "n\n")
	if err := runDelete(cmd, "abc", false, false, srv.URL); err != nil {
		t.Fatalf("runDelete declined: %v", err)
	}
	if calls != 0 || !strings.Contains(stdout.String(), "declined") {
		t.Fatalf("declined delete still called the server: calls=%d out=%q", calls, stdout.String())
	}
	cmd, stdout, _ = newCmd(t, "y\n")
	if err := runDelete(cmd, "abc", false, false, srv.URL); err != nil {
		t.Fatalf("runDelete: %v", err)
	}
	if calls != 1 || !strings.Contains(stdout.String(), "deleted session abc") {
		t.Fatalf("calls=%d out=%q", calls, stdout.String())
	}
}

func TestDeleteNotFoundIsAnError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions/abc", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"session_not_found"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedToken(t, srv.URL)

	cmd, _, stderr := newCmd(t, "")
	if err := runDelete(cmd, "abc", true, false, srv.URL); err == nil {
		t.Fatal("want an error")
	}
	if !strings.Contains(stderr.String(), "session_not_found") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}
