// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func TestWorkingSurfaceCommandsAreRegistered(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{"recent": false, "announce": false, "watch": false}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered", name)
		}
	}
}

func TestRunRecentUsesGeneratedClientAndFilters(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/broadcast/recent" {
			t.Errorf("path: %s", r.URL.Path)
		}
		if got := r.URL.Query().Get("target"); got != "cluster-a" {
			t.Errorf("target: %q", got)
		}
		if got := r.URL.Query().Get("limit"); got != "5" {
			t.Errorf("limit: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"events":[{"cursor":"1-0","kind":"announcement"}],"next_cursor":"1-0"}`)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	cmd, stdout, _ := newRunCmd(t)
	if err := runRecent(cmd, recentOptions{Target: "cluster-a", Limit: 5, JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRecent: %v", err)
	}
	if !strings.Contains(stdout.String(), `"next_cursor": "1-0"`) {
		t.Errorf("unexpected JSON: %s", stdout.String())
	}
}

func TestRunAnnouncePostsGeneratedRequest(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/broadcast/announce" || r.Method != http.MethodPost {
			t.Errorf("request: %s %s", r.Method, r.URL.Path)
		}
		var body api.BroadcastAnnounceRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode: %v", err)
		}
		if body.Activity != "Checking cluster-a" {
			t.Errorf("activity: %q", body.Activity)
		}
		if body.Target == nil || *body.Target != "cluster-a" {
			t.Errorf("target: %v", body.Target)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"event_id":"11111111-1111-1111-1111-111111111111","cursor":"1-0","targets":null,"planned_op_class":null,"ttl_minutes":null,"work_ref":null,"run_id":null}`)
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	cmd, stdout, _ := newRunCmd(t)
	if err := runAnnounce(cmd, announceOptions{Activity: "Checking cluster-a", Target: "cluster-a", Phase: "start", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runAnnounce: %v", err)
	}
	if !strings.Contains(stdout.String(), "cursor=1-0") {
		t.Errorf("summary: %s", stdout.String())
	}
}

func TestRenderBroadcastSSEEmitsWireData(t *testing.T) {
	cmd, stdout, _ := newRunCmd(t)
	if err := renderBroadcastSSE(strings.NewReader("event: broadcast\ndata: {\"kind\":\"announcement\"}\n\n"), cmd.OutOrStdout(), true); err != nil {
		t.Fatalf("renderBroadcastSSE: %v", err)
	}
	if got := stdout.String(); got != "{\"kind\":\"announcement\"}\n" {
		t.Errorf("output: %q", got)
	}
}
