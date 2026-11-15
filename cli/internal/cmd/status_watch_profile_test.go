// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

// runWatchWithProfile runs `status --watch` for one profile against a
// fake feed that sends no event, and returns what it wrote to stdout.
func runWatchWithProfile(t *testing.T, profile string, jsonOut bool) string {
	t.Helper()
	xdg := withTempXDG(t)
	feed := newFakeFeed(t, http.StatusOK, nil, false)
	seedWatchCreds(t, xdg, feed.URL, jwtMarker)
	if profile != "" {
		store, err := auth.NewFileStore()
		if err != nil {
			t.Fatalf("NewFileStore: %v", err)
		}
		service, user := auth.KeyForProfile(feed.URL, profile)
		if err := store.Save(service, user, auth.StoredToken{
			BackplaneURL: feed.URL,
			AccessToken:  jwtMarker,
			Expiry:       time.Now().Add(time.Hour),
		}); err != nil {
			t.Fatalf("store.Save: %v", err)
		}
	}

	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	ctx, cancel := context.WithCancel(auth.WithProfile(context.Background(), profile))
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	err := runWatch(ctx, watchOptions{
		BackplaneURL: feed.URL,
		JSONOut:      jsonOut,
		Stdout:       stdout,
		Stderr:       stderr,
		HTTPClient:   feed.client(),
		Backoff:      fastBackoff,
	})
	if err != nil {
		t.Fatalf("runWatch returned: %v\nstderr=%s", err, stderr.String())
	}
	return stdout.String()
}

// A named profile prints the profile line once, before the stream.
func TestRunWatch_NamedProfilePrintsProfileLine(t *testing.T) {
	out := runWatchWithProfile(t, "approver", false)
	if !strings.HasPrefix(out, "Profile: approver\n") {
		t.Errorf("stdout must start with the profile line; got %q", out)
	}
	if n := strings.Count(out, "Profile:"); n != 1 {
		t.Errorf("profile line printed %d times; want once: %q", n, out)
	}
}

// The default profile prints no profile line.
func TestRunWatch_DefaultProfilePrintsNoProfileLine(t *testing.T) {
	if out := runWatchWithProfile(t, "", false); strings.Contains(out, "Profile:") {
		t.Errorf("default profile must print no profile line; got %q", out)
	}
}

// JSON mode keeps stdout clean for the event lines.
func TestRunWatch_JSONModePrintsNoProfileLine(t *testing.T) {
	if out := runWatchWithProfile(t, "approver", true); strings.Contains(out, "Profile:") {
		t.Errorf("--json must print no profile line; got %q", out)
	}
}
