// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// fastBackoff is the schedule the tests pass in via watchOptions.
// Five 1 ms slots collapse the production 1/2/5/10/30 s schedule
// into roughly 5 ms total per retry sweep so the table-driven suite
// stays sub-second.
var fastBackoff = []time.Duration{
	time.Millisecond,
	time.Millisecond,
	time.Millisecond,
	time.Millisecond,
	time.Millisecond,
}

// seedWatchCreds writes a token + config like seedCreds in
// status_test.go, but parameterised on the access token. Lifted into
// its own helper so the watch suite can mint distinct tokens per
// scenario without copying the file-store boilerplate.
func seedWatchCreds(t *testing.T, xdg, backplaneURL, accessToken string) {
	t.Helper()
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  accessToken,
		Expiry:       time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(xdg, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL}); err != nil {
		t.Fatalf("save config: %v", err)
	}
}

// fakeFeed stands up an httptest server that serves /api/v1/feed
// with a scripted SSE response. The handler records every received
// Authorization, Last-Event-Id, and query string so the suite can
// assert against the wire shape without scraping logs.
//
// frames is the raw SSE body (frames separated by blank lines).
// status is the HTTP status code to return (200 streams; 401 / 403
// / 400 trigger the fatal-status paths). hangAfter, when > 0,
// closes the connection after writing that many frames so the
// suite exercises the reconnect path.
type fakeFeedRecord struct {
	Authorization string
	LastEventID   string
	Query         string
}

type fakeFeed struct {
	URL     string
	mu      sync.Mutex
	calls   []fakeFeedRecord
	hits    atomic.Int64
	cursor  atomic.Int64
	frames  [][]byte
	status  int
	dropMid bool
}

// Records returns a snapshot of every request the fake feed has
// received. Locked so the test goroutine and the http handler
// goroutine don't race on the underlying slice. Returns a copy so
// the caller can iterate without holding the lock.
func (f *fakeFeed) Records() []fakeFeedRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]fakeFeedRecord, len(f.calls))
	copy(out, f.calls)
	return out
}

// record appends one received-request snapshot under the mutex.
func (f *fakeFeed) record(r fakeFeedRecord) {
	f.mu.Lock()
	f.calls = append(f.calls, r)
	f.mu.Unlock()
}

func newFakeFeed(t *testing.T, status int, frames [][]byte, dropMid bool) *fakeFeed {
	t.Helper()
	feed := &fakeFeed{frames: frames, status: status, dropMid: dropMid}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/feed", func(w http.ResponseWriter, r *http.Request) {
		idx := int(feed.hits.Add(1) - 1)
		feed.record(fakeFeedRecord{
			Authorization: r.Header.Get("Authorization"),
			LastEventID:   r.Header.Get("Last-Event-Id"),
			Query:         r.URL.RawQuery,
		})
		if feed.status != http.StatusOK {
			w.WriteHeader(feed.status)
			_, _ = w.Write([]byte(`{"detail":"forced"}`))
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		// Frames are written ONCE across all connections combined.
		// Each connection takes a slice from feed.cursor onward.
		//
		//   - dropMid=false: connection 0 writes everything, then
		//     blocks until the client cancels. Subsequent reconnects
		//     write nothing (cursor at end) and likewise block.
		//   - dropMid=true: connection 0 writes the first half then
		//     returns (EOF → client reconnects). Connection 1
		//     writes the remainder then blocks. Subsequent
		//     reconnects block immediately.
		//
		// This avoids the "fake server replays the same frames
		// every reconnect" failure mode the naive impl had.
		start := int(feed.cursor.Load())
		end := len(feed.frames)
		if feed.dropMid && idx == 0 {
			end = (len(feed.frames) + 1) / 2
		}
		for i := start; i < end; i++ {
			_, _ = w.Write(feed.frames[i])
			if flusher != nil {
				flusher.Flush()
			}
		}
		feed.cursor.Store(int64(end))

		// If we just wrote a partial slice as part of the drop-mid
		// scenario, return so the client EOFs and reconnects.
		if feed.dropMid && idx == 0 {
			return
		}
		// All remaining frames have been written. Hold the
		// connection open so the client's parser sits in
		// scanner.Scan() until the test context is cancelled.
		// Without this hold the handler would return, the client
		// would EOF, and the retry loop would dial back in a busy
		// loop until the test deadline.
		<-r.Context().Done()
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	feed.URL = srv.URL
	return feed
}

// makeFrame builds one SSE frame (event/data/id) with the trailing
// blank line that delimits frames.
func makeFrame(event, data, id string) []byte {
	var sb strings.Builder
	if event != "" {
		sb.WriteString("event: ")
		sb.WriteString(event)
		sb.WriteByte('\n')
	}
	if data != "" {
		sb.WriteString("data: ")
		sb.WriteString(data)
		sb.WriteByte('\n')
	}
	if id != "" {
		sb.WriteString("id: ")
		sb.WriteString(id)
		sb.WriteByte('\n')
	}
	sb.WriteByte('\n')
	return []byte(sb.String())
}

// canonicalEventJSON is the shape T3's publisher emits. Tests build
// frames from this so renderer assertions match the production
// wire format end-to-end.
func canonicalEventJSON(t *testing.T, principal, opID, opClass, status string, target *string) string {
	t.Helper()
	payload := map[string]any{
		"event_id":      "00000000-0000-0000-0000-000000000001",
		"ts":            "2026-05-13T14:23:01Z",
		"tenant_id":     "11111111-1111-1111-1111-111111111111",
		"principal_sub": principal,
		"target_name":   target,
		"op_id":         opID,
		"op_class":      opClass,
		"result_status": status,
		"audit_id":      "22222222-2222-2222-2222-222222222222",
		"payload":       map[string]any{"op_class": opClass, "params": map[string]any{}, "result_status": status},
	}
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal event: %v", err)
	}
	return string(b)
}

// TestRunWatch_RendersHumanLine — end-to-end check that one event
// frame produces one rendered line with the expected columns.
func TestRunWatch_RendersHumanLine(t *testing.T) {
	xdg := withTempXDG(t)
	target := "rdc-vcenter"
	frame := makeFrame("broadcast", canonicalEventJSON(t, "alice", "vsphere.vm.list", "read", "ok", &target), "1715600000000-0")
	feed := newFakeFeed(t, http.StatusOK, [][]byte{frame}, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		// Cancel a beat after the frame has had time to render +
		// the parser to hit EOF + the retry loop to kick once.
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})
	if err != nil {
		t.Fatalf("runWatch returned: %v\nstderr=%s", err, stderr.String())
	}

	out := stdout.String()
	if !strings.Contains(out, "2026-05-13T14:23:01Z") {
		t.Errorf("missing timestamp in line: %q", out)
	}
	if !strings.Contains(out, "alice") {
		t.Errorf("missing principal in line: %q", out)
	}
	if !strings.Contains(out, "vsphere.vm.list") {
		t.Errorf("missing op_id in line: %q", out)
	}
	if !strings.Contains(out, "target=rdc-vcenter") {
		t.Errorf("missing target suffix in line: %q", out)
	}
	if strings.Contains(out, jwtMarker) {
		t.Errorf("JWT marker leaked into watch stdout:\n%s", out)
	}
}

// TestRunWatch_AggregateOnlyClass — credential_read renders the
// (aggregate-only) placeholder instead of the target / params.
func TestRunWatch_AggregateOnlyClass(t *testing.T) {
	xdg := withTempXDG(t)
	frame := makeFrame("broadcast", canonicalEventJSON(t, "bob", "vault.kv.read", "credential_read", "ok", nil), "1715600000001-0")
	feed := newFakeFeed(t, http.StatusOK, [][]byte{frame}, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	if err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	}); err != nil {
		t.Fatalf("runWatch returned: %v", err)
	}

	if !strings.Contains(stdout.String(), "(aggregate-only)") {
		t.Errorf("expected (aggregate-only) placeholder, got:\n%s", stdout.String())
	}
}

// TestRunWatch_JSONLines — --json mode emits one JSON document per
// line, byte-identical to the SSE data field.
func TestRunWatch_JSONLines(t *testing.T) {
	xdg := withTempXDG(t)
	target := "rdc-k8s"
	dataA := canonicalEventJSON(t, "alice", "vsphere.vm.list", "read", "ok", &target)
	dataB := canonicalEventJSON(t, "bob", "vault.kv.read", "credential_read", "ok", nil)
	frames := [][]byte{
		makeFrame("broadcast", dataA, "1715600000000-0"),
		makeFrame("broadcast", dataB, "1715600000001-0"),
	}
	feed := newFakeFeed(t, http.StatusOK, frames, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(80 * time.Millisecond)
		cancel()
	}()

	if err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		JSONOut:      true,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	}); err != nil {
		t.Fatalf("runWatch returned: %v", err)
	}

	lines := strings.Split(strings.TrimRight(stdout.String(), "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 JSON lines, got %d:\n%s", len(lines), stdout.String())
	}
	for _, ln := range lines {
		var got map[string]any
		if err := json.Unmarshal([]byte(ln), &got); err != nil {
			t.Errorf("non-JSON line %q: %v", ln, err)
		}
	}
}

// TestRunWatch_FilterFlagsForwardToQuery — --op-class / --principal
// / --target each land in the SSE URL's query string.
func TestRunWatch_FilterFlagsForwardToQuery(t *testing.T) {
	xdg := withTempXDG(t)
	feed := newFakeFeed(t, http.StatusOK, nil, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(30 * time.Millisecond)
		cancel()
	}()

	_ = runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		OpClass:      "write",
		Principal:    "alice",
		Target:       "rdc-vcenter",
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})

	records := feed.Records()
	if len(records) == 0 {
		t.Fatal("fake feed never received a request")
	}
	q := records[0].Query
	for _, kv := range []string{"op_class=write", "principal=alice", "target=rdc-vcenter"} {
		if !strings.Contains(q, kv) {
			t.Errorf("missing %q in query %q", kv, q)
		}
	}
}

// TestRunWatch_AuthHeaderSent — the stored bearer token reaches the
// backplane verbatim (no double-encoding, no leak into stdout).
func TestRunWatch_AuthHeaderSent(t *testing.T) {
	xdg := withTempXDG(t)
	feed := newFakeFeed(t, http.StatusOK, nil, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(30 * time.Millisecond)
		cancel()
	}()

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	_ = runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})

	records := feed.Records()
	if len(records) == 0 {
		t.Fatal("no requests recorded")
	}
	if records[0].Authorization != "Bearer "+jwtMarker {
		t.Errorf("Authorization header = %q, want Bearer <marker>", records[0].Authorization)
	}
	if strings.Contains(stdout.String(), jwtMarker) || strings.Contains(stderr.String(), jwtMarker) {
		t.Errorf("JWT marker leaked into output")
	}
}

// TestRunWatch_ReconnectsWithLastEventID — first connection drops
// after one frame; the reconnect carries Last-Event-Id of the last
// rendered frame.
func TestRunWatch_ReconnectsWithLastEventID(t *testing.T) {
	xdg := withTempXDG(t)
	target := "rdc-vcenter"
	frames := [][]byte{
		makeFrame("broadcast", canonicalEventJSON(t, "alice", "vsphere.vm.list", "read", "ok", &target), "1715600000000-0"),
		makeFrame("broadcast", canonicalEventJSON(t, "alice", "vsphere.vm.create", "write", "ok", &target), "1715600000001-0"),
	}
	feed := newFakeFeed(t, http.StatusOK, frames, true) // drop after first frame
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		// Allow first connection's first frame, the drop, the
		// 1 ms backoff, the reconnect, the second frame, and a
		// margin before cancelling.
		time.Sleep(200 * time.Millisecond)
		cancel()
	}()

	_ = runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})

	records := feed.Records()
	if len(records) < 2 {
		t.Fatalf("expected at least 2 requests (initial + reconnect); got %d", len(records))
	}
	if records[0].LastEventID != "" {
		t.Errorf("first request should have empty Last-Event-Id, got %q", records[0].LastEventID)
	}
	if records[1].LastEventID != "1715600000000-0" {
		t.Errorf("reconnect Last-Event-Id = %q, want 1715600000000-0", records[1].LastEventID)
	}
}

// TestRunWatch_401_ExitsAuthExpired — 401 from the feed returns
// auth_expired without retrying.
func TestRunWatch_401_ExitsAuthExpired(t *testing.T) {
	xdg := withTempXDG(t)
	feed := newFakeFeed(t, http.StatusUnauthorized, nil, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})
	if err == nil {
		t.Fatal("expected non-nil error on 401")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != output.ExitAuthExpired {
		t.Errorf("exit = %d, want %d", coder.ExitCode(), output.ExitAuthExpired)
	}
	if got := len(feed.Records()); got != 1 {
		t.Errorf("401 should not retry; got %d calls", got)
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("stderr should hint at `meho login`, got: %q", stderr.String())
	}
}

// TestRunWatch_403_ExitsInsufficientRole — 403 from the feed
// returns insufficient_role (exit code 5).
func TestRunWatch_403_ExitsInsufficientRole(t *testing.T) {
	xdg := withTempXDG(t)
	feed := newFakeFeed(t, http.StatusForbidden, nil, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})
	if err == nil {
		t.Fatal("expected non-nil error on 403")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != output.ExitInsufficientRole {
		t.Errorf("exit = %d, want %d (insufficient_role)", coder.ExitCode(), output.ExitInsufficientRole)
	}
	if !strings.Contains(stderr.String(), output.ErrCodeInsufficientRole) {
		t.Errorf("stderr should name the error code, got: %q", stderr.String())
	}
}

// TestRunWatch_NoCreds_ExitsAuthExpired — no stored token → don't
// even try to dial the backplane.
func TestRunWatch_NoCreds_ExitsAuthExpired(t *testing.T) {
	_ = withTempXDG(t) // empty XDG → no seeded creds

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	err := runWatch(ctx, watchOptions{
		BackplaneURL: "https://example.invalid",
		Stdout:       stdout,
		Stderr:       stderr,
		Backoff:      fastBackoff,
	})
	if err == nil {
		t.Fatal("expected error for no-creds path")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != output.ExitAuthExpired {
		t.Errorf("exit = %d, want %d", coder.ExitCode(), output.ExitAuthExpired)
	}
}

// TestRunWatch_ContextCancel_CleanExit — cancellation during a
// blocked read returns nil so cobra exits 0.
func TestRunWatch_ContextCancel_CleanExit(t *testing.T) {
	xdg := withTempXDG(t)
	feed := newFakeFeed(t, http.StatusOK, nil, false) // server never writes a frame
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // already-cancelled context — runWatch should return promptly

	err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		Stdout:       &bytes.Buffer{},
		Stderr:       &bytes.Buffer{},
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})
	if err != nil {
		t.Errorf("Ctrl-C should return nil, got %v", err)
	}
}

// TestParseSSE_MultilineDataFrame — two “data:“ lines on one
// frame join with a single newline before parsing.
func TestParseSSE_MultilineDataFrame(t *testing.T) {
	frame := []byte("event: broadcast\ndata: line-one\ndata: line-two\nid: 1\n\n")
	stdout := &bytes.Buffer{}
	var lastID string
	err := parseSSE(context.Background(), bytes.NewReader(frame), streamArgs{
		Stdout:    stdout,
		Stderr:    &bytes.Buffer{},
		JSONOut:   true,
		OnEventID: func(id string) { lastID = id },
	})
	// parseSSE returns errStreamEnded on EOF without ctx
	// cancellation — that's the "server hung up, retry loop should
	// reconnect" signal. Any other error is a test failure.
	if err != nil && !errors.Is(err, errStreamEnded) {
		t.Fatalf("parseSSE: %v", err)
	}
	if got := strings.TrimSpace(stdout.String()); got != "line-one\nline-two" {
		t.Errorf("multiline data = %q, want %q", got, "line-one\\nline-two")
	}
	if lastID != "1" {
		t.Errorf("OnEventID = %q, want %q", lastID, "1")
	}
}

// TestParseSSE_HeartbeatSkipped — `: heartbeat\n\n` comment frame
// produces no output and no OnEventID call.
func TestParseSSE_HeartbeatSkipped(t *testing.T) {
	frame := []byte(": heartbeat\n\n")
	stdout := &bytes.Buffer{}
	var idCount int
	err := parseSSE(context.Background(), bytes.NewReader(frame), streamArgs{
		Stdout:    stdout,
		Stderr:    &bytes.Buffer{},
		JSONOut:   true,
		OnEventID: func(string) { idCount++ },
	})
	if err != nil && !errors.Is(err, errStreamEnded) {
		t.Fatalf("parseSSE: %v", err)
	}
	if stdout.Len() != 0 {
		t.Errorf("heartbeat produced output: %q", stdout.String())
	}
	if idCount != 0 {
		t.Errorf("heartbeat triggered OnEventID %d times", idCount)
	}
}

// TestParseSSE_RetryFieldIgnored — the SSE “retry:“ field is
// recognised (not dispatched as an event) and produces no output.
func TestParseSSE_RetryFieldIgnored(t *testing.T) {
	frame := []byte("retry: 5000\n\nevent: broadcast\ndata: ok\nid: 2\n\n")
	stdout := &bytes.Buffer{}
	err := parseSSE(context.Background(), bytes.NewReader(frame), streamArgs{
		Stdout:    stdout,
		Stderr:    &bytes.Buffer{},
		JSONOut:   true,
		OnEventID: func(string) {},
	})
	if err != nil && !errors.Is(err, errStreamEnded) {
		t.Fatalf("parseSSE: %v", err)
	}
	out := strings.TrimSpace(stdout.String())
	if out != "ok" {
		t.Errorf("retry frame was rendered: stdout=%q (want only 'ok' from the broadcast)", out)
	}
}

// TestBackoffDuration — schedule advances per attempt and clamps
// to the final entry once exhausted.
func TestBackoffDuration(t *testing.T) {
	sched := []time.Duration{1 * time.Second, 2 * time.Second, 5 * time.Second}
	cases := []struct {
		attempt int
		want    time.Duration
	}{
		{-1, time.Second}, // out-of-range guard
		{0, 1 * time.Second},
		{1, 2 * time.Second},
		{2, 5 * time.Second},
		{3, 5 * time.Second},  // past the end → clamp
		{99, 5 * time.Second}, // far past the end → clamp
	}
	for _, c := range cases {
		t.Run(fmt.Sprintf("attempt=%d", c.attempt), func(t *testing.T) {
			if got := backoffDuration(c.attempt, sched); got != c.want {
				t.Errorf("backoffDuration(%d) = %v, want %v", c.attempt, got, c.want)
			}
		})
	}
}

// TestBuildFeedURL — every non-empty filter lands in the query
// string; empty filters do NOT emit “?key=“ clutter.
func TestBuildFeedURL(t *testing.T) {
	cases := []struct {
		name           string
		opClass        string
		principal      string
		target         string
		wantInQuery    []string
		wantNotInQuery []string
	}{
		{
			name:           "no filters",
			wantNotInQuery: []string{"op_class=", "principal=", "target="},
		},
		{
			name:           "single filter",
			opClass:        "read",
			wantInQuery:    []string{"op_class=read"},
			wantNotInQuery: []string{"principal=", "target="},
		},
		{
			name:        "all three filters",
			opClass:     "write",
			principal:   "alice",
			target:      "rdc-vcenter",
			wantInQuery: []string{"op_class=write", "principal=alice", "target=rdc-vcenter"},
		},
		{
			name:           "trailing slash on backplane is stripped",
			opClass:        "read",
			wantInQuery:    []string{"/api/v1/feed"},
			wantNotInQuery: []string{"//api/v1/feed"},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			base := "https://backplane.test"
			if c.name == "trailing slash on backplane is stripped" {
				base = "https://backplane.test/"
			}
			got, err := buildFeedURL(base, c.opClass, c.principal, c.target)
			if err != nil {
				t.Fatalf("buildFeedURL: %v", err)
			}
			for _, want := range c.wantInQuery {
				if !strings.Contains(got, want) {
					t.Errorf("URL %q missing %q", got, want)
				}
			}
			for _, want := range c.wantNotInQuery {
				if strings.Contains(got, want) {
					t.Errorf("URL %q must not contain %q", got, want)
				}
			}
		})
	}
}

// TestHumanLine — table-driven check of the human-readable
// formatter (column shape + (aggregate-only) placeholder + bare
// target=… suffix + empty summary).
func TestHumanLine(t *testing.T) {
	target := "rdc-vcenter"
	cases := []struct {
		name    string
		event   broadcastEvent
		wantSub string
	}{
		{
			name: "read with target",
			event: broadcastEvent{
				TS:           time.Date(2026, 5, 13, 14, 23, 1, 0, time.UTC),
				PrincipalSub: "alice",
				OpID:         "vsphere.vm.list",
				OpClass:      "read",
				ResultStatus: "ok",
				TargetName:   &target,
			},
			wantSub: "target=rdc-vcenter",
		},
		{
			name: "credential_read renders aggregate-only",
			event: broadcastEvent{
				TS:           time.Date(2026, 5, 13, 14, 23, 1, 0, time.UTC),
				PrincipalSub: "alice",
				OpID:         "vault.kv.read",
				OpClass:      "credential_read",
				ResultStatus: "ok",
			},
			wantSub: "(aggregate-only)",
		},
		{
			name: "audit_query renders aggregate-only",
			event: broadcastEvent{
				TS:           time.Date(2026, 5, 13, 14, 23, 1, 0, time.UTC),
				PrincipalSub: "alice",
				OpID:         "audit.query",
				OpClass:      "audit_query",
				ResultStatus: "ok",
				TargetName:   &target,
			},
			wantSub: "(aggregate-only)",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			data, err := json.Marshal(c.event)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			got, err := humanLine(string(data))
			if err != nil {
				t.Fatalf("humanLine: %v", err)
			}
			if !strings.Contains(got, c.wantSub) {
				t.Errorf("line %q missing %q", got, c.wantSub)
			}
			if !strings.HasPrefix(got, "2026-05-13T14:23:01Z") {
				t.Errorf("line %q missing RFC3339 timestamp prefix", got)
			}
		})
	}
}

// TestSummariseEvent — the summary picker by op_class + target.
func TestSummariseEvent(t *testing.T) {
	target := "rdc-vcenter"
	cases := []struct {
		opClass string
		target  *string
		want    string
	}{
		{opClass: "credential_read", target: &target, want: "(aggregate-only)"},
		{opClass: "audit_query", target: &target, want: "(aggregate-only)"},
		{opClass: "read", target: &target, want: "target=rdc-vcenter"},
		{opClass: "write", target: &target, want: "target=rdc-vcenter"},
		{opClass: "read", target: nil, want: ""},
	}
	for _, c := range cases {
		t.Run(c.opClass, func(t *testing.T) {
			got := summariseEvent(broadcastEvent{OpClass: c.opClass, TargetName: c.target})
			if got != c.want {
				t.Errorf("summariseEvent(%q) = %q, want %q", c.opClass, got, c.want)
			}
		})
	}
}

// client returns an http.Client that hits the fake feed without
// going through the OS-level routing table — needed when the test
// runs inside a Docker container where the httptest URL points to
// an in-container localhost. Without it, http.DefaultClient still
// works (httptest binds to 127.0.0.1) but tests that want to pin
// transport-level behaviour can override here.
func (f *fakeFeed) client() *http.Client {
	return &http.Client{Timeout: 5 * time.Second}
}
