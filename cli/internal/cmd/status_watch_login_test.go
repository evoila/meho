// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

// lockedBuffer is a bytes.Buffer that the watch goroutine writes and the
// test reads at the same time.
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

type feedCall struct {
	Authorization string
	LastEventID   string
}

// fakeLoginFeed serves a feed that ends the first stream after one
// announcement, as the server does when the token of the stream
// expires, and then refuses the old token. The fake login server gives
// out "fresh-token" on a refresh.
type fakeLoginFeed struct {
	backplane *httptest.Server
	idp       *httptest.Server
	mu        sync.Mutex
	calls     []feedCall
}

func (f *fakeLoginFeed) Calls() []feedCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]feedCall(nil), f.calls...)
}

func announcementFrame(activity, id string) string {
	return "event: broadcast\ndata: {\"kind\":\"agent_announcement\",\"ts\":\"2026-05-13T14:23:01Z\"," +
		"\"principal_sub\":\"agent-a\",\"phase\":\"update\",\"activity\":\"" + activity + "\"}\nid: " + id + "\n\n"
}

func newFakeLoginFeed(t *testing.T) *fakeLoginFeed {
	t.Helper()
	f := &fakeLoginFeed{}
	feed := http.NewServeMux()
	feed.HandleFunc("/api/v1/feed", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.calls = append(f.calls, feedCall{r.Header.Get("Authorization"), r.Header.Get("Last-Event-Id")})
		first := len(f.calls) == 1
		f.mu.Unlock()
		if !first && r.Header.Get("Authorization") != "Bearer fresh-token" {
			w.WriteHeader(401) // the old token is refused
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		if first {
			_, _ = w.Write([]byte(announcementFrame("first step", "checkpoint-1")))
			return // the server ends the stream
		}
		if r.Header.Get("Last-Event-Id") == "checkpoint-1" {
			_, _ = w.Write([]byte(announcementFrame("second step", "checkpoint-2")))
		}
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	f.backplane = httptest.NewServer(feed)
	t.Cleanup(f.backplane.Close)

	idp := http.NewServeMux()
	idp.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(mustJSON(map[string]any{
			"issuer":                        f.idp.URL,
			"token_endpoint":                f.idp.URL + "/token",
			"device_authorization_endpoint": f.idp.URL + "/device",
		}))
	})
	idp.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"fresh-token","token_type":"Bearer","refresh_token":"refresh-2","expires_in":300}`))
	})
	f.idp = httptest.NewServer(idp)
	t.Cleanup(f.idp.Close)
	return f
}

// TestRunWatch_ReconnectsWithFreshTokenAfterServerEndsStream — the
// server ends the stream and then refuses the old token. Watch
// refreshes the login, reconnects with the fresh token and the
// Last-Event-Id of the last announcement, and misses no announcement.
func TestRunWatch_ReconnectsWithFreshTokenAfterServerEndsStream(t *testing.T) {
	xdg := withTempXDG(t)
	f := newFakeLoginFeed(t)
	seedCreds(t, xdg, f.backplane.URL, auth.StoredToken{
		BackplaneURL: f.backplane.URL,
		AccessToken:  "old-token",
		RefreshToken: "refresh-1",
		Issuer:       f.idp.URL,
		ClientID:     "meho-cli",
		// The stream ended because this token expired.
		Expiry: time.Now().Add(-time.Second),
	})

	stdout, stderr := &lockedBuffer{}, &lockedBuffer{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- runWatch(ctx, watchOptions{
			BackplaneURL: f.backplane.URL,
			Stdout:       stdout,
			Stderr:       stderr,
			HTTPClient:   &http.Client{Timeout: 5 * time.Second},
			Backoff:      fastBackoff,
		})
	}()

	deadline := time.Now().Add(5 * time.Second)
	for !strings.Contains(stdout.String(), "second step") && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("runWatch: %v; stderr: %s; calls: %+v", err, stderr.String(), f.Calls())
	}

	out := stdout.String()
	if strings.Count(out, "first step") != 1 || strings.Count(out, "second step") != 1 {
		t.Fatalf("want each announcement once; got:\n%s", out)
	}
	want := []feedCall{
		{"Bearer old-token", ""},
		{"Bearer old-token", "checkpoint-1"},
		{"Bearer fresh-token", "checkpoint-1"},
	}
	calls := f.Calls()
	if len(calls) != len(want) {
		t.Fatalf("calls = %+v; want %+v", calls, want)
	}
	for i := range want {
		if calls[i] != want[i] {
			t.Errorf("call %d = %+v; want %+v", i, calls[i], want[i])
		}
	}
}
