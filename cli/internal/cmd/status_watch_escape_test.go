// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
	"unicode"
)

// firstRawControl returns the first control character in s that a
// terminal would act on (C0 other than the line feed, DEL, or C1), or
// -1 when there is none.
func firstRawControl(s string) rune {
	for _, r := range s {
		if r != '\n' && unicode.Is(unicode.Cc, r) {
			return r
		}
	}
	return -1
}

// controlAnnouncementJSON is an announcement whose activity holds an
// erase-line code, a carriage return and BEL, and whose target holds a
// window-title (OSC) code.
func controlAnnouncementJSON(t *testing.T) string {
	t.Helper()
	b, err := json.Marshal(map[string]any{
		"kind":          "agent_announcement",
		"ts":            "2026-05-13T14:23:01Z",
		"principal_sub": "agent-bot\x1b[31m",
		"phase":         "start",
		"activity":      "rotating tokens\x1b[2K\rall good\x07",
		"target":        "\x1b]0;new title\x07cluster-x",
		"targets":       []string{"kube\u009b2J"},
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}

// TestRunWatch_EscapesControlCharacters — `meho status --watch` prints
// every text field of a feed line with control characters shown as
// visible escapes.
func TestRunWatch_EscapesControlCharacters(t *testing.T) {
	xdg := withTempXDG(t)
	frame := makeFrame("broadcast", controlAnnouncementJSON(t), "1715600000000-0")
	feed := newFakeFeed(t, 200, [][]byte{frame}, false)
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
		t.Fatalf("runWatch returned: %v\nstderr=%s", err, stderr.String())
	}

	out := stdout.String()
	if out == "" {
		t.Fatal("no line printed")
	}
	if r := firstRawControl(out); r != -1 {
		t.Fatalf("raw control character %U in output: %q", r, out)
	}
	for _, want := range []string{
		`agent-bot\x1b[31m`,
		`rotating tokens\x1b[2K\x0dall good\x07`,
		`target=\x1b]0;new title\x07cluster-x, kube\x9b2J`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output %q missing %q", out, want)
		}
	}
}

// TestHumanLine_EscapesOperationFields — the operation line escapes the
// principal, op id, status and target too.
func TestHumanLine_EscapesOperationFields(t *testing.T) {
	target := "vc\x1b]0;x\x07"
	data := canonicalEventJSON(t, "alice\r", "vsphere.vm.list\x07", "read", "ok\x1b[2K", &target)
	got, err := humanLine(data)
	if err != nil {
		t.Fatalf("humanLine: %v", err)
	}
	if r := firstRawControl(got); r != -1 {
		t.Fatalf("raw control character %U in line: %q", r, got)
	}
	for _, want := range []string{`alice\x0d`, `vsphere.vm.list\x07`, `ok\x1b[2K`, `target=vc\x1b]0;x\x07`} {
		if !strings.Contains(got, want) {
			t.Errorf("line %q missing %q", got, want)
		}
	}
}

// TestRenderEvent_EscapesUnparseableEvent — the "unparseable event"
// line shows the raw data with control characters escaped.
func TestRenderEvent_EscapesUnparseableEvent(t *testing.T) {
	var buf bytes.Buffer
	renderEvent(&buf, "broadcast", "not json\x1b[2K\r\x07", false)
	got := buf.String()
	if r := firstRawControl(got); r != -1 {
		t.Fatalf("raw control character %U in output: %q", r, got)
	}
	if want := `meho: unparseable event: not json\x1b[2K\x0d\x07` + "\n"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}
