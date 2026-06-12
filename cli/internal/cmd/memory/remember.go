// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"bytes"
	"context"
	"encoding/json"
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

// NewRememberCmd returns the top-level `meho remember` command.
//
// CLI shape (per issue #424; --persist added by G5.2-T2 #624):
//
//	meho remember "body text" \
//	  [--scope user|user-tenant|user-target|tenant|target] \
//	  [--slug SLUG] \
//	  [--target TARGET_NAME] \
//	  [--tag T] \
//	  [--ttl 7d] \
//	  [--persist] \
//	  [--json] \
//	  [--backplane <url>]
//
// Default scope: `user-tenant` (consumer-needs.md §G5 — most common
// case: the operator's notes scoped to one tenant).
//
// Body can be piped: `echo "body" | meho remember -`. The bare `-`
// is the explicit stdin sentinel; an inline string is the
// alternative. (Compare `meho kb add --body @-`; the body lives on
// a flag there because the slug is the positional. Here the body is
// the positional and `--slug` is a flag, so we use `-` as the
// in-band stdin marker.)
//
// `--persist` opts out of the G5.2-T2 (#624) backend default-TTL
// injection: the verb emits “"expires_at": null“ on the wire so
// the backend's :func:`_resolve_default_ttl` sees the field as
// explicitly present-and-null and refuses to inject the default 7-
// day cutoff. Mutually exclusive with `--ttl`: passing both surfaces
// a CLI-side error before the HTTP round-trip (`--ttl` already
// computes an absolute cutoff; emitting `null` alongside it would
// be self-contradictory).
//
// Role: any authenticated operator (`read_only` excluded at the
// FastAPI gate). The service-layer MemoryRbacResolver further
// restricts `tenant` writes to `tenant_admin`; that surfaces as
// 403 insufficient_role.
//
// Exit codes:
//   - 0   created (201)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 422 invalid_slug / missing body)
//   - 5   insufficient_role (e.g. operator writing `tenant` scope)
func NewRememberCmd() *cobra.Command {
	var (
		scopeFlag         string
		slugFlag          string
		targetFlag        string
		tagsFlag          []string
		ttlFlag           string
		persistFlag       bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "remember <body>",
		Short: "Persist one memory in the backplane (POST /api/v1/memory)",
		Long: "remember calls POST /api/v1/memory to persist one memory " +
			"under the operator's tenant. Default --scope is " +
			"`user-tenant` (the operator's notes scoped to one tenant — " +
			"consumer-needs.md §G5's most-common case).\n\n" +
			"The positional <body> argument carries the memory's text. " +
			"Pass `-` to read the body from stdin (`echo \"note\" | meho " +
			"remember -`); the trailing newline is stripped so a piped " +
			"`echo` doesn't smuggle a gratuitous `\\n` into the stored " +
			"body.\n\n" +
			"--scope=target / user-target requires --target NAME; the " +
			"check fires client-side so a forgotten flag surfaces " +
			"without a round-trip.\n\n" +
			"--ttl accepts shorthand durations like `7d`, `36h`, `30m`. " +
			"The CLI translates this into an absolute `expires_at` " +
			"timestamp before sending; the backend only ever sees a " +
			"clock-aligned cutoff. Memory rows past their expires_at " +
			"are filtered out of list / recall reads (G5.2 #374 ships " +
			"the daily cleanup task that physically removes them).\n\n" +
			"--tag may be repeated; tags land under `metadata.tags` as " +
			"a JSON list, mirroring the shape consumer-needs.md §G5 " +
			"describes. --slug overrides the auto-generated identifier " +
			"with an operator-supplied one (legal characters: letters, " +
			"digits, hyphen, underscore, dot).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRemember(cmd, rememberOptions{
				BodyArg:           args[0],
				ScopeArg:          scopeFlag,
				SlugArg:           slugFlag,
				TargetArg:         targetFlag,
				TagsArg:           tagsFlag,
				TTLArg:            ttlFlag,
				Persist:           persistFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&scopeFlag, "scope", string(ScopeUserTenant),
		"memory scope: user|user-tenant|user-target|tenant|target")
	cmd.Flags().StringVar(&slugFlag, "slug", "",
		"override the auto-generated slug with an operator-supplied identifier")
	cmd.Flags().StringVar(&targetFlag, "target", "",
		"target name (required when --scope=target or user-target)")
	cmd.Flags().StringSliceVar(&tagsFlag, "tag", nil,
		"tag to attach to the memory; repeat for multiple tags")
	cmd.Flags().StringVar(&ttlFlag, "ttl", "",
		"time-to-live shorthand (e.g. `7d`, `36h`, `30m`) — set expires_at")
	cmd.Flags().BoolVar(&persistFlag, "persist", false,
		"persist forever — opt out of the backend's default-7-day TTL on memory-user writes (sends expires_at=null)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw MemoryEntry JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}

type rememberOptions struct {
	BodyArg   string
	ScopeArg  string
	SlugArg   string
	TargetArg string
	TagsArg   []string
	TTLArg    string
	// Persist is the wire-shape flag for the G5.2-T2 (#624) backend
	// default-TTL opt-out: when true and TTLArg is empty, the
	// request emits ``"expires_at": null`` so the backend skips
	// the default 7-day injection and the memory persists forever.
	Persist           bool
	JSONOut           bool
	BackplaneOverride string
}

// rememberRequest captures the inputs to the POST /api/v1/memory
// request the verb sends. The struct exists alongside the generated
// `api.RememberBody` because the wire contract requires a tri-state
// for `expires_at` (absent → backend default fires; null → opt-out;
// value → explicit cutoff) that the generated type can't express:
// `api.RememberBody.ExpiresAt` is `*time.Time` *without* `omitempty`,
// so encoding/json would always emit either a value or `null` and
// the "absent → default fires" branch would be unreachable.
//
// The custom :func:`MarshalJSON` below handles the tri-state by
// emitting via a `map[string]any`, branching on Persist + ExpiresAt:
//
//   - Persist=false, ExpiresAt="" → field OMITTED; backend default
//     fires on user-scope writes.
//   - Persist=false, ExpiresAt="<RFC3339>" → field sent verbatim;
//     backend honours it as the explicit cutoff.
//   - Persist=true → field emitted as JSON `null`; backend sees the
//     field as present in :attr:`BaseModel.model_fields_set` with
//     value None and skips the default. Wire shape for
//     `meho remember --persist`.
type rememberRequest struct {
	Scope      Scope
	Body       string
	Slug       string
	Metadata   map[string]any
	ExpiresAt  string
	TargetName string
	// Persist, when true, emits ``"expires_at": null`` on the wire
	// even when ExpiresAt is empty. Mutually exclusive with a non-
	// empty ExpiresAt in practice; the verb-level CLI gate refuses
	// `--persist` + `--ttl` together so the operator's intent is
	// unambiguous. The struct doesn't enforce that mutual exclusion
	// itself (callers do) -- when both are set, ExpiresAt wins
	// because emitting "null" alongside a real timestamp would be
	// nonsensical.
	Persist bool
}

// MarshalJSON renders rememberRequest with explicit handling for the
// G5.2-T2 (#624) tri-state “expires_at“ contract. See the type-level
// docstring for the three states and their wire shapes.
//
// Implementation notes:
//
//   - Uses a positional emit (json.Marshal over a map[string]any)
//     rather than struct tags so the `expires_at` rendering can branch
//     on the Persist bool without leaking into the public field set.
//   - The map ordering does not affect JSON correctness; the backend
//     parses fields by key, not position.
//   - "Persist + ExpiresAt set" lets ExpiresAt win (we already wrote
//     the operator's intent to a clock-aligned cutoff at the
//     parseTTLFlag stage; emitting `null` would silently override
//     their `--ttl` value). The verb-level mutual-exclusion gate is
//     where this collision is reported back to the operator.
func (r rememberRequest) MarshalJSON() ([]byte, error) {
	out := map[string]any{
		"scope": r.Scope,
		"body":  r.Body,
	}
	if r.Slug != "" {
		out["slug"] = r.Slug
	}
	if r.Metadata != nil {
		out["metadata"] = r.Metadata
	}
	if r.TargetName != "" {
		out["target_name"] = r.TargetName
	}
	switch {
	case r.ExpiresAt != "":
		out["expires_at"] = r.ExpiresAt
	case r.Persist:
		// Explicit JSON `null` so backend's
		// :func:`_resolve_default_ttl` sees "expires_at" in
		// :attr:`BaseModel.model_fields_set` and skips the default.
		out["expires_at"] = nil
	}
	return json.Marshal(out)
}

func runRemember(cmd *cobra.Command, opts rememberOptions) error {
	scope, err := parseScope(opts.ScopeArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	if err := requireTargetForScope(scope, opts.TargetArg); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	// `--persist` opts out of the backend default TTL; `--ttl` sets
	// an explicit cutoff. Both at once is self-contradictory (the
	// operator wants "never expire" and "expire on X" simultaneously),
	// so refuse client-side rather than letting the JSON marshaler
	// silently let ExpiresAt win and discard --persist's intent.
	if opts.Persist && strings.TrimSpace(opts.TTLArg) != "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--persist and --ttl are mutually exclusive: --persist means \"never expire\", --ttl sets a finite cutoff"),
			opts.JSONOut)
	}
	body, err := loadBody(cmd, opts.BodyArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	expiresAt, err := parseTTLFlag(opts.TTLArg, time.Now)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}
	req := rememberRequest{
		Scope:      scope,
		Body:       body,
		Slug:       opts.SlugArg,
		ExpiresAt:  expiresAt,
		TargetName: opts.TargetArg,
		Persist:    opts.Persist,
	}
	if tags := parseTagsFlag(opts.TagsArg); tags != nil {
		// `tags` lands under `metadata.tags` per consumer-needs.md
		// §G5's description of memory tagging. The backend treats
		// `metadata` as an opaque JSONB blob — no schema constraint
		// on the `tags` key — so the CLI is the contract owner for
		// the convention.
		req.Metadata = map[string]any{"tags": tags}
	}
	resp, err := postRemember(cmd.Context(), backplaneURL, req)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Generated `ParseRememberApiV1MemoryPostResponse` populates
	// JSON201 only when the response has Content-Type containing
	// json AND status 201 (`cli/internal/api/client.gen.go`). A
	// backplane / proxy that returns 201 with a missing or mistyped
	// content-type leaves JSON201 nil; without this guard the verb
	// would emit `null` in `--json` mode and silently no-op in
	// summary mode. Mirrors the convention in
	// `cli/internal/cmd/status.go:142` and the kb sibling's
	// post-iter-2 nil-guard pattern (PR #1282).
	entry := resp.JSON201
	if entry == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without a memory entry payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printRememberSummary(cmd.OutOrStdout(), entry)
	return nil
}

func postRemember(
	ctx context.Context,
	backplaneURL string,
	req rememberRequest,
) (*api.RememberApiV1MemoryPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	// rememberRequest's custom MarshalJSON owns the tri-state
	// expires_at contract that the generated `api.RememberBody`
	// struct can't express (the generated `ExpiresAt *time.Time`
	// has no `omitempty` tag, so encoding/json would always emit
	// either a value or `null` and never omit the field). We send
	// the hand-marshaled body through the `*WithBodyWithResponse`
	// shape that accepts an `io.Reader`; the typed `*WithResponse`
	// variant takes a generated `RememberBody` and would break the
	// tri-state.
	raw, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal remember request: %w", err)
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RememberApiV1MemoryPostResponse, error) {
			return authed.RememberApiV1MemoryPostWithBodyWithResponse(
				ctx,
				&api.RememberApiV1MemoryPostParams{},
				"application/json",
				bytes.NewReader(raw),
			)
		},
		func(r *api.RememberApiV1MemoryPostResponse) int { return r.StatusCode() },
	)
}

// printRememberSummary renders the created entry as a compact
// confirmation line plus the natural-key coordinates the operator
// will use for a subsequent `meho recall` / `meho forget`. Body is
// not echoed back — the operator just typed it.
func printRememberSummary(w io.Writer, e *api.MemoryEntry) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "remembered %s/%s\n", e.Scope, e.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "id:", e.Id.String())
	fmt.Fprintf(w, "%-14s %s\n", "scope:", e.Scope)
	fmt.Fprintf(w, "%-14s %s\n", "slug:", e.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "expires_at:", formatTimePtr(e.ExpiresAt))
	fmt.Fprintf(w, "%-14s %s\n", "user_sub:", pluralisePtr(e.UserSub))
	fmt.Fprintf(w, "%-14s %s\n", "target_name:", pluralisePtr(e.TargetName))
	fmt.Fprintf(w, "%-14s %s\n", "created_at:", e.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintf(w, "%-14s %d bytes\n", "body:", len(e.Body))
}

// formatTimePtr renders a *time.Time as a UTC-ISO8601 string when
// set, or "(none)" when nil. The generated `api.MemoryEntry`
// surfaces optional timestamps as `*time.Time`; this helper keeps
// the summary printers' time-rendering identical to the
// `pluralisePtr(*string)` shape they already use for other optional
// fields.
func formatTimePtr(t *time.Time) string {
	if t == nil {
		return "(none)"
	}
	return t.UTC().Format("2006-01-02T15:04:05Z")
}
