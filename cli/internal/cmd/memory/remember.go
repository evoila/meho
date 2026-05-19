// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// NewRememberCmd returns the top-level `meho remember` command.
//
// CLI shape (per issue #424):
//
//	meho remember "body text" \
//	  [--scope user|user-tenant|user-target|tenant|target] \
//	  [--slug SLUG] \
//	  [--target TARGET_NAME] \
//	  [--tag T] \
//	  [--ttl 7d] \
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
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw Entry JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}

type rememberOptions struct {
	BodyArg           string
	ScopeArg          string
	SlugArg           string
	TargetArg         string
	TagsArg           []string
	TTLArg            string
	JSONOut           bool
	BackplaneOverride string
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
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			classifyBackplaneError(err), opts.JSONOut)
	}
	req := rememberRequest{
		Scope:      scope,
		Body:       body,
		Slug:       opts.SlugArg,
		ExpiresAt:  expiresAt,
		TargetName: opts.TargetArg,
	}
	if tags := parseTagsFlag(opts.TagsArg); tags != nil {
		// `tags` lands under `metadata.tags` per consumer-needs.md
		// §G5's description of memory tagging. The backend treats
		// `metadata` as an opaque JSONB blob — no schema constraint
		// on the `tags` key — so the CLI is the contract owner for
		// the convention.
		req.Metadata = map[string]any{"tags": tags}
	}
	entry, err := postRemember(cmd.Context(), backplaneURL, req)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printRememberSummary(cmd.OutOrStdout(), entry)
	return nil
}

func postRemember(ctx context.Context, backplaneURL string, req rememberRequest) (*Entry, error) {
	raw, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal remember request: %w", err)
	}
	resp, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/memory", raw)
	if err != nil {
		return nil, err
	}
	var out Entry
	if err := json.Unmarshal(resp, &out); err != nil {
		return nil, fmt.Errorf("decode remember response: %w", err)
	}
	return &out, nil
}

// printRememberSummary renders the created entry as a compact
// confirmation line plus the natural-key coordinates the operator
// will use for a subsequent `meho recall` / `meho forget`. Body is
// not echoed back — the operator just typed it.
func printRememberSummary(w io.Writer, e *Entry) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "remembered %s/%s\n", e.Scope, e.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "id:", e.ID)
	fmt.Fprintf(w, "%-14s %s\n", "scope:", e.Scope)
	fmt.Fprintf(w, "%-14s %s\n", "slug:", e.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "expires_at:", pluralisePtr(e.ExpiresAt))
	fmt.Fprintf(w, "%-14s %s\n", "user_sub:", pluralisePtr(e.UserSub))
	fmt.Fprintf(w, "%-14s %s\n", "target_name:", pluralisePtr(e.TargetName))
	fmt.Fprintf(w, "%-14s %s\n", "created_at:", e.CreatedAt)
	fmt.Fprintf(w, "%-14s %d bytes\n", "body:", len(e.Body))
}
