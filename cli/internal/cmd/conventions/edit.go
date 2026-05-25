// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// updateRequest mirrors the backend ConventionUpdate pydantic model
// (PATCH /api/v1/conventions/{slug}). Pydantic v2's
// `model_fields_set` distinguishes "field absent from JSON" from "field
// present with null", and the route handler applies only the
// explicitly-set keys to the ORM row. Each field is a pointer so an
// omitted CLI flag is left out of the JSON body via omitempty.
//
// `slug` and `kind` are absent from this struct by design — they're
// not in the PATCH surface. Renaming a convention is delete + recreate
// (the audit log and history rows reference the old slug); changing a
// convention's kind in-place would silently change its preamble-
// inclusion behaviour (operational → reference would disappear from
// every future preamble without an audit signal), so the substrate
// rejects it.
type updateRequest struct {
	Title    *string `json:"title,omitempty"`
	Body     *string `json:"body,omitempty"`
	Priority *int    `json:"priority,omitempty"`
}

// newEditCmd returns the `meho conventions edit` command.
//
//	meho conventions edit <slug> \
//	  [--title T] [--body @file|@-|<inline-text>] [--priority N] \
//	  [--json] [--backplane <url>]
//
// Role: tenant_admin. Two interaction modes:
//
//  1. **Flag-driven PATCH.** When any of --title / --body / --priority
//     is set, the verb runs a partial PATCH with only those fields.
//     Mirrors the agent / kb edit shape. This is the scripting path
//     (CI pipelines, batch updates).
//
//  2. **$EDITOR interactive.** When no field flag is set, the verb
//     fetches the current body, opens $EDITOR (or $VISUAL, fallback
//     `vi`) on the seeded body, and submits the saved content as a
//     PATCH on `body` only. This is the operator path for conversational
//     rule edits — read the current body, edit in vim/nano/emacs/code,
//     save, ship.
//
//     Editor failure or empty saved body aborts with no API call. A
//     422 over-budget response is surfaced inline (estimated and budget
//     token counts) so the operator sees the rejection before the
//     buffer is discarded.
//
// A 404 (`convention_not_found`) covers both genuine absence and
// cross-tenant probes. A 422 over-budget happens only when the saved
// body is an `operational` convention exceeding the preamble token
// budget; surface verbatim.
//
// Exit codes:
//   - 0   convention updated cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404, 422 invalid / over-budget)
//   - 5   insufficient_role
func newEditCmd() *cobra.Command {
	var (
		title             string
		body              string
		priority          int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "edit <slug>",
		Short: "Edit one convention via flags or $EDITOR (tenant_admin)",
		Long: "edit calls PATCH /api/v1/conventions/{slug}. Tenant_admin " +
			"only.\n\n" +
			"Two modes:\n\n" +
			"1. Flag-driven PATCH: when any of --title / --body / " +
			"--priority is set, only those fields are sent. Mirrors the " +
			"agent / kb edit shape — the scripting path.\n\n" +
			"2. $EDITOR interactive: when no field flag is set, the verb " +
			"fetches the current body, opens $EDITOR (or $VISUAL, " +
			"fallback vi) on the seeded body, and submits the saved " +
			"content as a PATCH on body only. Editor failure or empty " +
			"saved body aborts with no API call. A 422 over-budget " +
			"response is surfaced inline (with estimated and budget " +
			"token counts) before the buffer is discarded.\n\n" +
			"--body accepts inline text, @<path>, or @-. The slug and " +
			"kind are not editable: renaming is delete + recreate, " +
			"changing kind would silently change preamble inclusion.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEdit(cmd, editOptions{
				Slug:              args[0],
				Title:             title,
				BodyArg:           body,
				Priority:          priority,
				titleSet:          cmd.Flags().Changed("title"),
				bodySet:           cmd.Flags().Changed("body"),
				prioritySet:       cmd.Flags().Changed("priority"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&title, "title", "", "new short display label")
	cmd.Flags().StringVar(&body, "body", "",
		"new body: inline text, @<path>, or @- (omit for $EDITOR mode)")
	cmd.Flags().IntVar(&priority, "priority", 0,
		"new ranking key (range -32768..32767)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw Convention JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type editOptions struct {
	Slug              string
	Title             string
	BodyArg           string
	Priority          int
	titleSet          bool
	bodySet           bool
	prioritySet       bool
	JSONOut           bool
	BackplaneOverride string
}

func runEdit(cmd *cobra.Command, opts editOptions) error {
	if opts.Slug == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit requires a non-empty <slug> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	req, err := buildEditRequest(cmd, backplaneURL, opts)
	if err != nil {
		// buildEditRequest may surface either a CLI-level validation
		// error (unexpected category) or an upstream HTTP error
		// (rendered via renderRequestError already in $EDITOR mode).
		// Distinguish by checking for our editorAbortError sentinel.
		var abort *editorAbortError
		if errors.As(err, &abort) {
			// Editor mode aborted (editor failure or empty buffer).
			// Render as unexpected; the message includes the specific
			// reason. No API call was made.
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(abort.Error()), opts.JSONOut)
		}
		var preflight *editFetchError
		if errors.As(err, &preflight) {
			// Show-side fetch failed (404 / 401 / transport); render
			// using the standard ladder so 404 carries the backend's
			// convention_not_found detail.
			return renderRequestError(cmd, backplaneURL, preflight.cause, opts.JSONOut)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	if req == nil {
		// Defence in depth — buildEditRequest only returns nil when no
		// fields end up populated, which buildEditRequest itself
		// rejects upstream. Belt-and-braces in case of a future
		// refactor.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit produced an empty PATCH body"), opts.JSONOut)
	}

	conv, err := patchEdit(cmd.Context(), backplaneURL, opts.Slug, *req)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), conv)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "updated convention %q\n", conv.Slug)
	fmt.Fprintf(cmd.OutOrStdout(), "%-14s %s\n", "kind:", conv.Kind)
	fmt.Fprintf(cmd.OutOrStdout(), "%-14s %d\n", "priority:", conv.Priority)
	fmt.Fprintf(cmd.OutOrStdout(), "%-14s %s\n", "title:", conv.Title)
	fmt.Fprintf(cmd.OutOrStdout(), "%-14s %s\n", "updated_at:", conv.UpdatedAt)
	fmt.Fprintf(cmd.OutOrStdout(), "%-14s %d bytes\n", "body:", len(conv.Body))
	return nil
}

// editorAbortError signals that $EDITOR mode aborted (editor exited
// non-zero, returned an empty buffer, etc.) — no API call was made.
// The runEdit caller renders this as `unexpected` exit-code 4 so the
// failure is distinguishable from a remote rejection.
type editorAbortError struct {
	reason string
}

func (e *editorAbortError) Error() string { return e.reason }

// editFetchError wraps the pre-edit Show fetch failure so runEdit can
// route it through the standard error-classification ladder (a 404 on
// the show call must surface as convention_not_found, not as a generic
// "couldn't fetch" message).
type editFetchError struct {
	cause error
}

func (e *editFetchError) Error() string { return e.cause.Error() }
func (e *editFetchError) Unwrap() error { return e.cause }

// buildEditRequest assembles the PATCH body from the flag set or from
// $EDITOR depending on which mode the operator invoked. Returns the
// populated updateRequest or an error.
//
// Split from runEdit so the field-selection logic stays unit-testable
// without standing up an httptest.Server in every test.
func buildEditRequest(cmd *cobra.Command, backplaneURL string, opts editOptions) (*updateRequest, error) {
	anyFlagSet := opts.titleSet || opts.bodySet || opts.prioritySet

	if anyFlagSet {
		req := &updateRequest{}
		if opts.titleSet {
			t := opts.Title
			req.Title = &t
		}
		if opts.bodySet {
			body, err := loadBodyFlag(cmd, opts.BodyArg)
			if err != nil {
				return nil, err
			}
			req.Body = &body
		}
		if opts.prioritySet {
			if opts.Priority < -32768 || opts.Priority > 32767 {
				return nil, fmt.Errorf("--priority must be between -32768 and 32767; got %d", opts.Priority)
			}
			p := opts.Priority
			req.Priority = &p
		}
		return req, nil
	}

	// $EDITOR mode: fetch current body, open editor on it, PATCH the
	// saved content as `body` only.
	current, err := getConvention(cmd.Context(), backplaneURL, opts.Slug)
	if err != nil {
		return nil, &editFetchError{cause: err}
	}
	saved, err := runEditor(cmd, current.Body)
	if err != nil {
		return nil, &editorAbortError{reason: fmt.Sprintf("editor session aborted: %v", err)}
	}
	trimmed := strings.TrimRight(saved, "\r\n")
	if trimmed == "" {
		return nil, &editorAbortError{reason: "edited body is empty; aborting without API call"}
	}
	if trimmed == strings.TrimRight(current.Body, "\r\n") {
		return nil, &editorAbortError{reason: "edited body is unchanged; aborting without API call"}
	}
	req := &updateRequest{Body: &trimmed}
	return req, nil
}

func patchEdit(
	ctx context.Context,
	backplaneURL, slug string,
	body updateRequest,
) (*Convention, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal conventions edit request: %w", err)
	}
	resp, err := doAuthedRequest(ctx, backplaneURL, "PATCH", buildShowPath(slug), raw)
	if err != nil {
		return nil, err
	}
	var out Convention
	if err := json.Unmarshal(resp, &out); err != nil {
		return nil, fmt.Errorf("decode conventions edit response: %w", err)
	}
	return &out, nil
}
