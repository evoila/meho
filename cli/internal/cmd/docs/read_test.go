// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// testReadHandle is an opaque read handle as a search hit carries it.
const testReadHandle = "eyJ2IjoxfQ.c2lnbmF0dXJl"

func ptrBool(v bool) *bool { return &v }

// readServer starts a stub backplane whose /api/v1/read_docs answers with
// handler, and seeds a token for it.
func readServer(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/read_docs", handler)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL, "eyJ.test.token")
	return srv
}

func TestRunReadPrintsTextAndNextCursor(t *testing.T) {
	var bodyOnWire api.ReadDocsRequest
	srv := readServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.DocsReadResult{
			Mode:       api.DocsReadResultModeAround,
			Text:       ptrStr("Before the hit.\nThe hit.\nAfter the hit."),
			Title:      ptrStr("Configuration Maximums"),
			SourceUrl:  ptrStr("https://docs.example/max"),
			Disclosure: api.DocsReadResultDisclosureFull,
			Truncated:  ptrBool(true),
			Next:       ptrStr("cursor-next-1"),
		})
	})

	cmd, stdout, stderr := newRunCmd(t)
	err := runRead(cmd, readOptions{
		ReadHandle: testReadHandle, Collection: "vmware", Mode: "around",
		Before: 2, After: 1, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRead: %v; stderr=%s", err, stderr.String())
	}
	if bodyOnWire.ReadHandle != testReadHandle {
		t.Errorf("expected the handle on the wire unchanged; got %q", bodyOnWire.ReadHandle)
	}
	if bodyOnWire.Collection == nil || *bodyOnWire.Collection != "vmware" {
		t.Errorf("expected collection=vmware; got %+v", bodyOnWire.Collection)
	}
	if bodyOnWire.Mode == nil || *bodyOnWire.Mode != api.ReadDocsRequestModeAround {
		t.Errorf("expected mode=around; got %+v", bodyOnWire.Mode)
	}
	if bodyOnWire.Before == nil || *bodyOnWire.Before != 2 {
		t.Errorf("expected before=2; got %+v", bodyOnWire.Before)
	}
	if bodyOnWire.Cursor != nil {
		t.Errorf("expected no cursor; got %q", *bodyOnWire.Cursor)
	}
	for _, want := range []string{
		"title:  Configuration Maximums",
		"source: https://docs.example/max",
		"The hit.",
		"cut at the size limit",
		"next: cursor-next-1",
	} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunReadSendsCursorAndRefinements(t *testing.T) {
	var bodyOnWire api.ReadDocsRequest
	srv := readServer(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.DocsReadResult{
			Mode: api.DocsReadResultModePage, Text: ptrStr("page"),
			Disclosure: api.DocsReadResultDisclosureFull,
		})
	})

	cmd, _, stderr := newRunCmd(t)
	err := runRead(cmd, readOptions{
		ReadHandle: testReadHandle, Collection: "vmware", Mode: "page",
		Before: 1, After: 1, Cursor: "cursor-next-1", Product: "nsx", Version: "9.0",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRead: %v; stderr=%s", err, stderr.String())
	}
	if bodyOnWire.Cursor == nil || *bodyOnWire.Cursor != "cursor-next-1" {
		t.Errorf("expected the cursor on the wire; got %+v", bodyOnWire.Cursor)
	}
	if bodyOnWire.Product == nil || *bodyOnWire.Product != "nsx" ||
		bodyOnWire.Version == nil || *bodyOnWire.Version != "9.0" {
		t.Errorf("expected product/version on the wire; got %+v", bodyOnWire)
	}
	if bodyOnWire.Mode == nil || *bodyOnWire.Mode != api.ReadDocsRequestModePage {
		t.Errorf("expected mode=page; got %+v", bodyOnWire.Mode)
	}
}

func TestRunReadLinkOnlyPrintsLinkWithoutText(t *testing.T) {
	srv := readServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"mode":"around","text":null,"title":"Guide",`+
			`"source_url":"https://docs.example/guide","disclosure":"link",`+
			`"reason":"link_only","located":null,"truncated":false,"next":null,"up":null}`)
	})

	cmd, stdout, stderr := newRunCmd(t)
	err := runRead(cmd, readOptions{
		ReadHandle: testReadHandle, Collection: "vmware", Mode: "around",
		Before: 1, After: 1, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRead: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	if !strings.Contains(out, "source: https://docs.example/guide") {
		t.Errorf("expected the link; got %q", out)
	}
	if !strings.Contains(out, "allows only its link") {
		t.Errorf("expected the link-only note; got %q", out)
	}
	if strings.Contains(out, "next:") {
		t.Errorf("expected no next cursor; got %q", out)
	}
}

func TestRunReadJSON(t *testing.T) {
	srv := readServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.DocsReadResult{
			Mode: api.DocsReadResultModeSection, Text: ptrStr("section text"),
			Disclosure: api.DocsReadResultDisclosureFull, Next: ptrStr("c2"),
		})
	})

	cmd, stdout, stderr := newRunCmd(t)
	err := runRead(cmd, readOptions{
		ReadHandle: testReadHandle, Collection: "vmware", Mode: "section",
		Before: 1, After: 1, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRead: %v; stderr=%s", err, stderr.String())
	}
	var got api.DocsReadResult
	readJSONBodyOf(t, stdout.Bytes(), &got)
	if got.Text == nil || *got.Text != "section text" || got.Next == nil || *got.Next != "c2" {
		t.Errorf("expected the raw result; got %+v", got)
	}
}

func TestRunReadRejectsBadFlagsBeforeTheCall(t *testing.T) {
	cases := []struct {
		name string
		opts readOptions
		want string
	}{
		{"no handle", readOptions{Collection: "vmware", Mode: "around"}, "non-empty <read-handle>"},
		{"no collection", readOptions{ReadHandle: testReadHandle, Mode: "around"}, "requires --collection"},
		{"bad mode", readOptions{ReadHandle: testReadHandle, Collection: "vmware", Mode: "all"}, "--mode must be"},
		{"before too big", readOptions{ReadHandle: testReadHandle, Collection: "vmware", Mode: "around", Before: 4}, "--before must be"},
		{"after negative", readOptions{ReadHandle: testReadHandle, Collection: "vmware", Mode: "around", After: -1}, "--after must be"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cmd, _, stderr := newRunCmd(t)
			// No server: a bad flag must fail before any network call.
			tc.opts.BackplaneOverride = "http://127.0.0.1:1"
			err := runRead(cmd, tc.opts)
			if got := exitCodeOf(t, err); got != output.ExitUnexpected {
				t.Errorf("expected exit %d; got %d", output.ExitUnexpected, got)
			}
			if !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("expected %q in stderr; got %q", tc.want, stderr.String())
			}
			if strings.Contains(stderr.String(), testReadHandle) {
				t.Errorf("the read handle must never be echoed; got %q", stderr.String())
			}
		})
	}
}

func TestRunReadMapsStatuses(t *testing.T) {
	cases := []struct {
		name       string
		status     int
		body       string
		retryAfter string
		wantExit   int
		want       string
	}{
		{
			"404 not found", http.StatusNotFound,
			`{"detail":{"error":"not_found","message":"docs source not found"}}`, "",
			output.ExitUnexpected, "docs source not found",
		},
		{
			"409 search again", http.StatusConflict,
			`{"detail":{"error":"search_again","message":"x"}}`, "",
			output.ExitUnexpected, "run `meho docs search` again",
		},
		{
			"429 with wait", http.StatusTooManyRequests,
			`{"detail":{"error":"rate_limited","retry_after":7}}`, "7",
			output.ExitUnexpected, "wait 7 seconds",
		},
		{
			"429 without wait", http.StatusTooManyRequests,
			`{"detail":{"error":"rate_limited","retry_after":null}}`, "",
			output.ExitUnexpected, "wait a moment",
		},
		{
			"503 unavailable", http.StatusServiceUnavailable,
			`{"detail":{"error":"read_unavailable","message":"corpus unreachable"}}`, "",
			output.ExitUnexpected, "corpus unreachable",
		},
		{
			"403 role", http.StatusForbidden,
			`{"detail":"operator role required"}`, "",
			output.ExitInsufficientRole, "operator role required",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := readServer(t, func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if tc.retryAfter != "" {
					w.Header().Set("Retry-After", tc.retryAfter)
				}
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			})
			cmd, _, stderr := newRunCmd(t)
			err := runRead(cmd, readOptions{
				ReadHandle: testReadHandle, Collection: "vmware", Mode: "around",
				Before: 1, After: 1, BackplaneOverride: srv.URL,
			})
			if got := exitCodeOf(t, err); got != tc.wantExit {
				t.Errorf("expected exit %d; got %d (stderr=%q)", tc.wantExit, got, stderr.String())
			}
			if !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("expected %q in stderr; got %q", tc.want, stderr.String())
			}
			if strings.Contains(stderr.String(), testReadHandle) {
				t.Errorf("the read handle must never be echoed; got %q", stderr.String())
			}
		})
	}
}

func TestReadCmdIsRegisteredWithDefaults(t *testing.T) {
	root := NewRootCmd()
	var read bool
	for _, sub := range root.Commands() {
		if sub.Name() == "read" {
			read = true
		}
	}
	if !read {
		t.Fatalf("expected `meho docs read` to be registered")
	}
	cmd := newReadCmd()
	for flag, want := range map[string]string{"mode": "around", "before": "1", "after": "1"} {
		if got := cmd.Flags().Lookup(flag).DefValue; got != want {
			t.Errorf("--%s default: want %q, got %q", flag, want, got)
		}
	}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{})
	if err := cmd.Execute(); err == nil {
		t.Errorf("expected an error with no <read-handle> argument")
	}
}

func TestRunReadTakesHandleAndCursorFromStdin(t *testing.T) {
	cases := []struct {
		name       string
		handleArg  string
		cursorFlag string
		stdin      string
		wantCursor string
	}{
		{"handle only", "-", "", testReadHandle + "\n", ""},
		{"cursor only", testReadHandle, "-", "cursor-next-1\n", "cursor-next-1"},
		{"handle then cursor", "-", "-", testReadHandle + "\r\ncursor-next-1\r\n", "cursor-next-1"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var bodyOnWire api.ReadDocsRequest
			srv := readServer(t, func(w http.ResponseWriter, r *http.Request) {
				raw, _ := io.ReadAll(r.Body)
				readJSONBodyOf(t, raw, &bodyOnWire)
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(api.DocsReadResult{
					Mode: api.DocsReadResultModeAround, Text: ptrStr("text"),
					Disclosure: api.DocsReadResultDisclosureFull,
				})
			})
			cmd, _, stderr := newRunCmd(t)
			cmd.SetIn(strings.NewReader(tc.stdin))
			err := runRead(cmd, readOptions{
				ReadHandle: tc.handleArg, Cursor: tc.cursorFlag, Collection: "vmware",
				Mode: "around", Before: 1, After: 1, BackplaneOverride: srv.URL,
			})
			if err != nil {
				t.Fatalf("runRead: %v; stderr=%s", err, stderr.String())
			}
			if bodyOnWire.ReadHandle != testReadHandle {
				t.Errorf("expected the handle from stdin on the wire; got %q", bodyOnWire.ReadHandle)
			}
			switch {
			case tc.wantCursor == "" && bodyOnWire.Cursor != nil:
				t.Errorf("expected no cursor; got %q", *bodyOnWire.Cursor)
			case tc.wantCursor != "" && (bodyOnWire.Cursor == nil || *bodyOnWire.Cursor != tc.wantCursor):
				t.Errorf("expected cursor %q; got %+v", tc.wantCursor, bodyOnWire.Cursor)
			}
		})
	}
}

func TestRunReadStdinRejectsTheWrongShapeBeforeTheCall(t *testing.T) {
	cases := []struct {
		name       string
		handleArg  string
		cursorFlag string
		stdin      string
		want       string
	}{
		{"empty for the handle", "-", "", "", "must hold the read handle and nothing else"},
		{"two values for the handle", "-", "", testReadHandle + "\nextra\n", "must hold the read handle and nothing else"},
		{"two values for the cursor", testReadHandle, "-", "c1 c2\n", "must hold the cursor and nothing else"},
		{"one value for both", "-", "-", testReadHandle + "\n", "must hold two lines"},
		{"too long", "-", "", strings.Repeat("a", readStdinCap+1), "longer than"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cmd, _, stderr := newRunCmd(t)
			cmd.SetIn(strings.NewReader(tc.stdin))
			// No server: a bad standard input must fail before any network call.
			err := runRead(cmd, readOptions{
				ReadHandle: tc.handleArg, Cursor: tc.cursorFlag, Collection: "vmware",
				Mode: "around", Before: 1, After: 1, BackplaneOverride: "http://127.0.0.1:1",
			})
			if got := exitCodeOf(t, err); got != output.ExitUnexpected {
				t.Errorf("expected exit %d; got %d", output.ExitUnexpected, got)
			}
			if !strings.Contains(stderr.String(), tc.want) {
				t.Errorf("expected %q in stderr; got %q", tc.want, stderr.String())
			}
			if strings.Contains(stderr.String(), testReadHandle) {
				t.Errorf("the read handle must never be echoed; got %q", stderr.String())
			}
		})
	}
}
