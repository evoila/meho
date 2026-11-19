// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

type watchCall struct {
	Authorization string
	LastEventID   string
}

// TestRunBroadcastWatchReconnectsWithFreshTokenAfterServerEndsStream —
// the server ends the first stream after one announcement, as it does
// when the token of the stream expires, and then refuses the old token.
// Watch refreshes the login, reconnects with the fresh token and the
// Last-Event-Id of the last announcement, and misses no announcement.
func TestRunBroadcastWatchReconnectsWithFreshTokenAfterServerEndsStream(t *testing.T) {
	saved := watchBackoff
	watchBackoff = []time.Duration{time.Millisecond}
	t.Cleanup(func() { watchBackoff = saved })

	var (
		mu    sync.Mutex
		calls []watchCall
	)
	frame := func(activity, id string) string {
		return "event: broadcast\ndata: {\"activity\":\"" + activity + "\"}\nid: " + id + "\n\n"
	}
	feed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls = append(calls, watchCall{r.Header.Get("Authorization"), r.Header.Get("Last-Event-Id")})
		first := len(calls) == 1
		mu.Unlock()
		if !first && r.Header.Get("Authorization") != "Bearer fresh-token" {
			w.WriteHeader(401) // the old token is refused
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		if first {
			_, _ = w.Write([]byte(frame("first step", "checkpoint-1")))
			return // the server ends the stream
		}
		if r.Header.Get("Last-Event-Id") == "checkpoint-1" {
			_, _ = w.Write([]byte(frame("second step", "checkpoint-2")))
		}
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer feed.Close()

	var idp *httptest.Server
	idpMux := http.NewServeMux()
	idpMux.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"issuer":                        idp.URL,
			"token_endpoint":                idp.URL + "/token",
			"device_authorization_endpoint": idp.URL + "/device",
		})
	})
	idpMux.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"fresh-token","token_type":"Bearer","refresh_token":"refresh-2","expires_in":300}`))
	})
	idp = httptest.NewServer(idpMux)
	defer idp.Close()

	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(feed.URL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: feed.URL,
		AccessToken:  "old-token",
		RefreshToken: "refresh-1",
		Issuer:       idp.URL,
		ClientID:     "meho-cli",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(-time.Second), // the stream ended because it expired
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(dir, "meho", "config.json"), auth.Config{BackplaneURL: feed.URL}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}

	stdout, stderr := &lockedBuffer{}, &lockedBuffer{}
	cmd := &cobra.Command{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cmd.SetContext(ctx)
	done := make(chan error, 1)
	go func() { done <- runBroadcastWatch(cmd, watchOptions{JSONOut: true, BackplaneOverride: feed.URL}) }()

	deadline := time.Now().Add(5 * time.Second)
	for !strings.Contains(stdout.String(), "second step") && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("runBroadcastWatch: %v; stderr: %s", err, stderr.String())
	}

	want := "{\"activity\":\"first step\"}\n{\"activity\":\"second step\"}\n"
	if got := stdout.String(); got != want {
		t.Errorf("output = %q; want %q", got, want)
	}
	mu.Lock()
	defer mu.Unlock()
	wantCalls := []watchCall{
		{"Bearer old-token", ""},
		{"Bearer old-token", "checkpoint-1"},
		{"Bearer fresh-token", "checkpoint-1"},
	}
	if len(calls) != len(wantCalls) {
		t.Fatalf("calls = %+v; want %+v", calls, wantCalls)
	}
	for i := range wantCalls {
		if calls[i] != wantCalls[i] {
			t.Errorf("call %d = %+v; want %+v", i, calls[i], wantCalls[i])
		}
	}
}

// TestRenderBroadcastSSEReportsFrameIDs — each frame id reaches the
// callback, so a reconnect can resume after the last announcement.
func TestRenderBroadcastSSEReportsFrameIDs(t *testing.T) {
	var ids []string
	var out bytes.Buffer
	in := "event: broadcast\ndata: {\"a\":1}\nid: c1\n\n: heartbeat\n\nevent: broadcast\ndata: {\"a\":2}\nid: c2\n\n"
	if err := renderBroadcastSSE(strings.NewReader(in), &out, true, func(id string) { ids = append(ids, id) }); err != nil {
		t.Fatalf("renderBroadcastSSE: %v", err)
	}
	if strings.Join(ids, ",") != "c1,c2" {
		t.Errorf("ids = %v; want [c1 c2]", ids)
	}
}
