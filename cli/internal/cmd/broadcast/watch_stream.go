// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// watchBackoff is the wait before each reconnect of `meho broadcast
// watch`. After the last slot, the last one repeats. A stream that the
// server ended cleanly starts the schedule again.
var watchBackoff = []time.Duration{
	1 * time.Second,
	2 * time.Second,
	5 * time.Second,
	10 * time.Second,
	30 * time.Second,
}

// feedConnectError is a connection to the feed that failed before an
// answer came (network error). After a first good connection, watch
// waits and tries again.
type feedConnectError struct{ err error }

func (e *feedConnectError) Error() string { return e.err.Error() }
func (e *feedConnectError) Unwrap() error { return e.err }

// feedReadError is a read failure of an open stream. Watch reconnects.
type feedReadError struct{ err error }

func (e *feedReadError) Error() string { return e.err.Error() }
func (e *feedReadError) Unwrap() error { return e.err }

// runBroadcastWatch prints the tenant's announcements until the command
// context ends. The server ends a stream when its login ends (for
// example when the access token expires). Watch then opens a new stream
// with a fresh token and the Last-Event-Id of the last event, so it
// misses no announcement.
func runBroadcastWatch(cmd *cobra.Command, opts watchOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	ctx := cmd.Context()
	client, err := newAuthedClient(ctx, cmd, backplaneURL, opts.JSONOut)
	if err != nil {
		return err
	}
	feedURL, err := buildBroadcastFeedURL(backplaneURL, opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	lastEventID := ""
	connected := false
	attempt := 0
	for {
		resp, err := openBroadcastFeed(ctx, client, feedURL, lastEventID)
		if ctx.Err() != nil {
			return nil
		}
		var connectErr *feedConnectError
		switch {
		case err != nil && (!connected || !errors.As(err, &connectErr)):
			return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
		case err == nil:
			connected = true
			err = renderBroadcastSSE(resp.Body, cmd.OutOrStdout(), opts.JSONOut, func(id string) { lastEventID = id })
			resp.Body.Close() //nolint:errcheck
			if ctx.Err() != nil {
				return nil
			}
			var readErr *feedReadError
			if err != nil && !errors.As(err, &readErr) {
				return err
			}
			if err == nil {
				attempt = 0
			}
		}
		wait := watchBackoff[min(attempt, len(watchBackoff)-1)]
		attempt++
		if !sleepUntil(ctx, wait) {
			return nil
		}
	}
}

// openBroadcastFeed opens one feed stream with the current access token
// and Last-Event-Id. On a 401 it refreshes the login once and tries
// again. A network error is a *feedConnectError, an answer other than
// 200 is an *httpResponseError, and a failed refresh is returned as it is.
func openBroadcastFeed(ctx context.Context, client *api.AuthedClient, feedURL, lastEventID string) (*http.Response, error) {
	resp, err := sendFeedRequest(ctx, client, feedURL, lastEventID)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == 401 { // the token was refused
		resp.Body.Close() //nolint:errcheck
		if err := client.Refresh(ctx); err != nil {
			return nil, err
		}
		if resp, err = sendFeedRequest(ctx, client, feedURL, lastEventID); err != nil {
			return nil, err
		}
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		resp.Body.Close() //nolint:errcheck
		return nil, &httpResponseError{statusCode: resp.StatusCode, body: body}
	}
	return resp, nil
}

func sendFeedRequest(ctx context.Context, client *api.AuthedClient, feedURL, lastEventID string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, feedURL, nil)
	if err != nil {
		return nil, fmt.Errorf("build feed request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+client.AccessToken())
	req.Header.Set("Accept", "text/event-stream")
	api.SetSessionIDHeader(req, client.SessionID())
	if lastEventID != "" {
		req.Header.Set("Last-Event-Id", lastEventID)
	}
	api.SetWorkRefHeader(req)
	api.SetTenantHeader(req)
	resp, err := client.HTTPClient().Do(req)
	if err != nil {
		return nil, &feedConnectError{err: err}
	}
	return resp, nil
}

// sleepUntil waits d; it returns false when ctx ends first.
func sleepUntil(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// renderBroadcastSSE prints the data of each SSE frame and calls onID
// (when set) with the id of each frame that has one. A read failure of
// the stream is a *feedReadError.
func renderBroadcastSSE(r io.Reader, w io.Writer, jsonOut bool, onID func(string)) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var data []string
	id := ""
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if len(data) > 0 {
				if err := printFrameData(w, strings.Join(data, "\n"), jsonOut); err != nil {
					return err
				}
				data = nil
			}
			if id != "" && onID != nil {
				onID(id)
			}
			id = ""
			continue
		}
		if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
		if strings.HasPrefix(line, "id:") {
			id = strings.TrimPrefix(strings.TrimPrefix(line, "id:"), " ")
		}
	}
	if err := scanner.Err(); err != nil {
		return &feedReadError{err: err}
	}
	return nil
}

func printFrameData(w io.Writer, raw string, jsonOut bool) error {
	if jsonOut {
		_, err := fmt.Fprintln(w, raw)
		return err
	}
	var event map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &event); err != nil {
		return err
	}
	// The event text comes from the server and may hold control
	// characters; print them as visible escapes.
	_, err := fmt.Fprintln(w, output.SafeText(fmt.Sprintf("%v", event)))
	return err
}
