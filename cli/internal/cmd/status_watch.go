// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// watchOptions bundles the parameters runWatch needs. Kept as a
// struct so the cobra dispatch layer doesn't have to thread a long
// argument list, and so tests can construct one without depending on
// cobra at all.
type watchOptions struct {
	BackplaneURL string
	OpClass      string
	Principal    string
	Target       string
	JSONOut      bool
	Stdout       io.Writer
	Stderr       io.Writer

	// HTTPClient overrides http.DefaultClient. Tests pass an
	// httptest.Server's Client(); production code leaves zero.
	HTTPClient *http.Client
	// Store overrides the default TokenStore. Tests pass an
	// in-memory store; production code leaves nil and the resolver
	// uses auth.NewTokenStore() (OS keyring with file fallback).
	Store auth.TokenStore
	// Backoff overrides the default backoff schedule. Tests pass a
	// fast schedule so the suite runs in tens of milliseconds, not
	// tens of seconds.
	Backoff []time.Duration
}

// defaultBackoff is the SSE reconnect schedule from the T5 issue
// body: 1s, 2s, 5s, 10s, 30s, then 30s for every subsequent retry.
// Matches the EventSource spec's "retry: <ms>" convention without
// requiring the server to send the field — the client picks the
// schedule, the server doesn't override it. Each duration is one
// reconnect attempt; after the slice is exhausted, the loop uses
// the final entry indefinitely.
var defaultBackoff = []time.Duration{
	1 * time.Second,
	2 * time.Second,
	5 * time.Second,
	10 * time.Second,
	30 * time.Second,
}

// runWatch opens the /api/v1/feed SSE stream and prints one rendered
// line per broadcast event until ctx is cancelled (Ctrl-C, parent
// cobra command lifetime). Reconnects on transport errors using
// Last-Event-Id for replay; gives up immediately on
// auth/role/contract failures since the operator action is required.
//
// The control flow is a single loop:
//
//  1. Load the bearer token from the configured store.
//  2. Open one HTTP request to the SSE URL with the current
//     Last-Event-Id (empty on the first attempt).
//  3. On HTTP 401 / 403 / unexpected status, return immediately —
//     none are recoverable by retry.
//  4. On HTTP 200, stream events; update lastEventID after each one.
//  5. On stream end (server hang-up, network error, anything that
//     isn't ctx.Err()), wait for the next backoff slot and reconnect.
//  6. On ctx.Done() at any point, return nil (clean Ctrl-C exit).
//
// Exit-code contract: any returned error already passed through
// output.RenderError, so callers (the cobra RunE) propagate it as-is.
func runWatch(ctx context.Context, opts watchOptions) error {
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	store := opts.Store
	if store == nil {
		s, err := auth.NewTokenStore()
		if err != nil {
			return output.RenderError(opts.Stderr,
				output.Unexpected(fmt.Sprintf("token store: %v", err)),
				opts.JSONOut)
		}
		store = s
	}
	backoff := opts.Backoff
	if backoff == nil {
		backoff = defaultBackoff
	}

	service, user := auth.KeyForBackplane(opts.BackplaneURL)
	tok, err := store.Load(service, user)
	if err != nil {
		if errors.Is(err, auth.ErrTokenNotFound) {
			return output.RenderError(opts.Stderr,
				output.AuthExpired(fmt.Sprintf("no stored credentials for %s; run `meho login %s`", opts.BackplaneURL, opts.BackplaneURL)),
				opts.JSONOut)
		}
		return output.RenderError(opts.Stderr,
			output.Unexpected(fmt.Sprintf("load token: %v", err)),
			opts.JSONOut)
	}
	if tok.AccessToken == "" {
		return output.RenderError(opts.Stderr,
			output.AuthExpired(fmt.Sprintf("stored token has no access_token; run `meho login %s`", opts.BackplaneURL)),
			opts.JSONOut)
	}

	feedURL, err := buildFeedURL(opts.BackplaneURL, opts.OpClass, opts.Principal, opts.Target)
	if err != nil {
		return output.RenderError(opts.Stderr,
			output.Unexpected(fmt.Sprintf("build feed URL: %v", err)),
			opts.JSONOut)
	}

	lastEventID := ""
	attempt := 0
	for {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil
		}
		streamErr := streamOnce(ctx, streamArgs{
			HTTPClient:  httpClient,
			URL:         feedURL,
			Bearer:      tok.AccessToken,
			LastEventID: lastEventID,
			JSONOut:     opts.JSONOut,
			Stdout:      opts.Stdout,
			Stderr:      opts.Stderr,
			OnEventID:   func(id string) { lastEventID = id },
		})
		// Terminal conditions: ctx cancelled, or a non-recoverable
		// HTTP-status / contract error already rendered to stderr.
		if streamErr == nil || errors.Is(streamErr, context.Canceled) {
			return nil
		}
		var fatal *fatalStreamError
		if errors.As(streamErr, &fatal) {
			return fatal.RenderedErr
		}
		// Recoverable: log a one-line note to stderr (humans only —
		// JSON mode keeps stdout clean) and sleep until the next
		// backoff slot.
		if !opts.JSONOut {
			fmt.Fprintf(opts.Stderr, "meho: feed connection dropped (%s); retrying in %s\n",
				streamErr.Error(), backoffDuration(attempt, backoff))
		}
		if waitErr := sleepCtx(ctx, backoffDuration(attempt, backoff)); waitErr != nil {
			return nil
		}
		attempt++
	}
}

// streamArgs bundles the per-attempt parameters of streamOnce. Kept
// distinct from watchOptions so the retry-loop state stays explicit
// (Bearer + LastEventID are mutated across attempts).
type streamArgs struct {
	HTTPClient  *http.Client
	URL         string
	Bearer      string
	LastEventID string
	JSONOut     bool
	Stdout      io.Writer
	Stderr      io.Writer
	// OnEventID is called for every dispatched event so the retry
	// loop can pin the cursor for the next reconnect. Kept as a
	// callback to avoid leaking the loop's state into streamOnce.
	OnEventID func(id string)
}

// fatalStreamError marks a stream error that the retry loop must
// NOT retry — HTTP 401 / 403 / unexpected-status responses where
// the operator needs to do something. RenderedErr already passed
// through output.RenderError so the caller can return it directly.
type fatalStreamError struct {
	RenderedErr error
	Cause       string
}

func (e *fatalStreamError) Error() string { return e.Cause }
func (e *fatalStreamError) Unwrap() error { return e.RenderedErr }

// streamOnce opens one SSE connection and dispatches every event
// it receives until the stream ends. Returns nil when ctx is
// cancelled mid-stream (clean Ctrl-C), a fatalStreamError for
// non-recoverable HTTP statuses, or a plain error for recoverable
// transport / read failures.
//
// SSE wire format (per the WHATWG EventSource spec):
//
//	event: broadcast
//	data: {"event_id":"...", "ts":"...", ...}
//	id: 1715600000000-0
//	<blank line>
//
// Multiple “data:“ lines for one frame are joined with newlines
// before parsing. Comments (“: heartbeat“) are skipped. A blank
// line dispatches whatever frame is being built; the parser then
// resets and waits for the next frame.
func streamOnce(ctx context.Context, args streamArgs) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, args.URL, nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+args.Bearer)
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Cache-Control", "no-cache")
	if args.LastEventID != "" {
		req.Header.Set("Last-Event-Id", args.LastEventID)
	}

	resp, err := args.HTTPClient.Do(req)
	if err != nil {
		// ctx cancelled while dialing — surface as nil so the
		// retry loop unwinds cleanly.
		if ctx.Err() != nil {
			return nil
		}
		return fmt.Errorf("connect: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		// happy path; fall through to the parser
	case http.StatusUnauthorized:
		rendered := output.RenderError(args.Stderr,
			output.AuthExpired("backplane rejected stored credentials; run `meho login`"),
			args.JSONOut)
		return &fatalStreamError{RenderedErr: rendered, Cause: "401"}
	case http.StatusForbidden:
		rendered := output.RenderError(args.Stderr,
			output.InsufficientRole("operator role required for the SSE feed; ask your tenant admin for an operator-role grant"),
			args.JSONOut)
		return &fatalStreamError{RenderedErr: rendered, Cause: "403"}
	case http.StatusBadRequest:
		// 400 on the SSE feed today means an invalid cursor
		// (Last-Event-Id outside the Valkey-stream-id shape, or a
		// rejected ``since`` query parameter). Surface the body's
		// detail because the operator may have hand-edited a
		// reconnect cursor in a wrapper script.
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		rendered := output.RenderError(args.Stderr,
			output.Unexpected(fmt.Sprintf("backplane returned 400: %s", strings.TrimSpace(string(body)))),
			args.JSONOut)
		return &fatalStreamError{RenderedErr: rendered, Cause: "400"}
	default:
		rendered := output.RenderError(args.Stderr,
			output.Unexpected(fmt.Sprintf("HTTP %d from %s", resp.StatusCode, args.URL)),
			args.JSONOut)
		return &fatalStreamError{RenderedErr: rendered, Cause: fmt.Sprintf("HTTP %d", resp.StatusCode)}
	}

	return parseSSE(ctx, resp.Body, args)
}

// errStreamEnded is the sentinel parseSSE returns when the upstream
// closed the connection cleanly (handler returned, server hung up).
// SSE is a forever-stream: from the client's perspective an EOF on
// a 200 response is always unexpected, so the retry loop treats it
// like a transport error and reconnects with Last-Event-Id rather
// than terminating the watch command.
var errStreamEnded = errors.New("sse stream ended")

// parseSSE reads frames off body and dispatches each completed
// frame to renderEvent. Returns:
//
//   - nil when ctx is cancelled (clean Ctrl-C),
//   - errStreamEnded when the upstream closed (EOF) without ctx
//     being cancelled — the retry loop reconnects,
//   - any other read error from the scanner verbatim.
func parseSSE(ctx context.Context, body io.Reader, args streamArgs) error {
	scanner := bufio.NewScanner(body)
	// SSE frames are bounded by blank lines; individual lines can
	// be long when ``data:`` carries a verbose payload. Lift the
	// scanner buffer to 1 MiB so a fat audit-result entry doesn't
	// trip bufio.ErrTooLong before the blank-line delimiter lands.
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	var (
		eventType string
		dataLines []string
		id        string
	)
	dispatch := func() {
		if len(dataLines) == 0 {
			return
		}
		data := strings.Join(dataLines, "\n")
		renderEvent(args.Stdout, eventType, data, args.JSONOut)
		if id != "" {
			args.OnEventID(id)
		}
		eventType, dataLines, id = "", nil, ""
	}

	for scanner.Scan() {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil
		}
		line := scanner.Text()
		if line == "" {
			dispatch()
			continue
		}
		if strings.HasPrefix(line, ":") {
			// Comment — heartbeat or operator-facing note. Drop.
			continue
		}
		// SSE allows ``field:value`` (no space) and ``field: value``
		// (one space after the colon). Strip exactly one leading
		// space from the value to honour both forms.
		field, value, ok := strings.Cut(line, ":")
		if !ok {
			// A field name with no colon is a no-op per the spec.
			continue
		}
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			eventType = value
		case "data":
			dataLines = append(dataLines, value)
		case "id":
			id = value
		case "retry":
			// Spec field — the server can suggest a reconnect
			// delay. v0.1 ignores: the client picks the schedule
			// per the T5 issue body's explicit 1/2/5/10/30s.
		}
	}
	// Scanner exited the loop. Three causes worth distinguishing:
	//
	//   1. ctx cancellation — return nil so the retry loop unwinds
	//      cleanly (Ctrl-C path).
	//   2. A non-nil scanner error — wrap and return so the retry
	//      loop reports it before reconnecting.
	//   3. Clean EOF — return errStreamEnded so the retry loop
	//      reconnects (SSE streams aren't supposed to end).
	if ctx.Err() != nil {
		return nil
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("read stream: %w", err)
	}
	return errStreamEnded
}

// renderEvent prints one event frame to w in the configured shape.
// The human path is a single space-padded line; the JSON path is
// the raw “data:“ payload (already JSON) verbatim, with one
// newline appended so `jq` consumers see one document per line.
//
// Malformed payloads in the human path render the literal raw
// data with a leading marker so the operator can spot upstream bugs
// without the renderer panicking. JSON-mode malformed payloads pass
// through unchanged — the operator's downstream `jq` will surface
// the parse error.
func renderEvent(w io.Writer, eventType, data string, jsonOut bool) {
	// Only ``event: broadcast`` carries a payload today. The feed
	// emits heartbeats as comments (``: heartbeat``), not events,
	// so they never reach this function — but guard explicitly so
	// a future event type doesn't render as a malformed broadcast.
	if eventType != "" && eventType != "broadcast" {
		return
	}
	if jsonOut {
		fmt.Fprintln(w, data)
		return
	}
	line, err := humanLine(data)
	if err != nil {
		fmt.Fprintf(w, "meho: unparseable event: %s\n", data)
		return
	}
	fmt.Fprintln(w, line)
}

// broadcastEvent is the subset of the backplane's BroadcastEvent
// shape the CLI renders. The full Pydantic model is wider; we only
// decode the fields the human formatter needs and pass everything
// else through verbatim on the --json path.
type broadcastEvent struct {
	TS           time.Time `json:"ts"`
	PrincipalSub string    `json:"principal_sub"`
	TargetName   *string   `json:"target_name"`
	OpID         string    `json:"op_id"`
	OpClass      string    `json:"op_class"`
	ResultStatus string    `json:"result_status"`
}

// humanLine formats one broadcastEvent as a single line:
//
//	<RFC3339 ts>  <principal>  <op_id>  <result_status>  <summary>
//
// where <summary> is one of:
//
//   - “(aggregate-only)“ for credential_read / audit_query ops —
//     the operator sees that the work happened but not the details.
//   - “target=<name>“ when target_name is set.
//   - empty otherwise.
//
// Column widths are tuned for the issue body's example output; the
// op_id column pads to 18 because the canonical connector op names
// (“vsphere.vm.list“, “vault.kv.read“) cluster around 14-16
// characters. Operators wanting hard-aligned output can pipe through
// “column -t“.
func humanLine(data string) (string, error) {
	var ev broadcastEvent
	if err := json.Unmarshal([]byte(data), &ev); err != nil {
		return "", fmt.Errorf("decode event: %w", err)
	}
	return fmt.Sprintf("%s  %-22s  %-18s  %-6s  %s",
		ev.TS.UTC().Format(time.RFC3339),
		ev.PrincipalSub,
		ev.OpID,
		ev.ResultStatus,
		summariseEvent(ev),
	), nil
}

// summariseEvent picks the payload-summary column value for the
// human-readable line.
//
// “credential_read“ and “audit_query“ op classes surface as
// “(aggregate-only)“ — operators see the event happened (so
// dashboards still tick) but not the parameters (which carry
// sensitive lookup terms). T3's publisher already strips the
// params field on these op classes; the placeholder makes the
// elision visible.
func summariseEvent(ev broadcastEvent) string {
	switch ev.OpClass {
	case "credential_read", "audit_query":
		return "(aggregate-only)"
	}
	if ev.TargetName != nil && *ev.TargetName != "" {
		return "target=" + *ev.TargetName
	}
	return ""
}

// buildFeedURL composes the SSE URL: backplane + /api/v1/feed plus
// any non-empty filter parameters. Empty filter values are dropped
// rather than sent as “?op_class=“ — the backplane treats both
// "absent" and "empty string" as "no filter", but emitting empty
// params clutters the wire and confuses curl debugging.
func buildFeedURL(backplane, opClass, principal, target string) (string, error) {
	base := strings.TrimRight(backplane, "/")
	u, err := url.Parse(base + "/api/v1/feed")
	if err != nil {
		return "", err
	}
	q := u.Query()
	if opClass != "" {
		q.Set("op_class", opClass)
	}
	if principal != "" {
		q.Set("principal", principal)
	}
	if target != "" {
		q.Set("target", target)
	}
	u.RawQuery = q.Encode()
	return u.String(), nil
}

// backoffDuration picks the backoff slot for attempt n. The
// schedule is a fixed slice; once exhausted, the final entry
// repeats — the issue body's 30 s ceiling stays in force forever.
func backoffDuration(attempt int, schedule []time.Duration) time.Duration {
	if attempt < 0 || len(schedule) == 0 {
		return time.Second
	}
	if attempt >= len(schedule) {
		return schedule[len(schedule)-1]
	}
	return schedule[attempt]
}

// sleepCtx waits d or until ctx is cancelled, whichever comes
// first. Returns ctx.Err() on cancellation, nil on timer fire.
// Used by the retry loop instead of a bare “time.Sleep“ so
// Ctrl-C unwinds within milliseconds rather than waiting out a 30s
// backoff slot.
func sleepCtx(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
