// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// kbEntryCreateRequest mirrors the backend KbEntryCreate pydantic
// model. The `Metadata` field is sent only when non-nil so the
// backend's default (`{}`) applies when the operator passes no
// `--metadata`. `slug` and `body` are required by the substrate's
// `min_length=1` constraint; the CLI rejects empty values before
// the request goes out.
type kbEntryCreateRequest struct {
	Slug     string         `json:"slug"`
	Body     string         `json:"body"`
	Metadata map[string]any `json:"metadata,omitempty"`
}

// newAddCmd returns the `meho kb add` command.
//
// CLI shape (per issue #418):
//
//	meho kb add <slug> \
//	  --body @file.md|@-|<inline-text> \
//	  [--metadata key=value,key=value] \
//	  [--json] \
//	  [--backplane <url>]
//
// Role: tenant_admin. Operator-role JWT lands as 403
// insufficient_role.
//
// `--body @<path>` reads the named file; `--body @-` reads from
// stdin; bare `--body "text"` accepts inline content. Trailing
// newlines from file / stdin reads are stripped (a 1-line file
// passed via @ doesn't carry a gratuitous final newline through
// the JSON body); embedded newlines are preserved.
//
// `--metadata "k1=v1,k2=v2"` parses to a flat string-keyed map.
// Comma + equals are not escapable in v0.2 — operators who need
// commas / equals inside values should construct the JSON body via
// the REST surface or wait for v0.2.next.
//
// Exit codes:
//   - 0   entry created (201)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 422 invalid_slug / missing body)
//   - 5   insufficient_role
func newAddCmd() *cobra.Command {
	var (
		body              string
		metadata          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "add <slug>",
		Short: "Create or re-index one kb entry (tenant_admin)",
		Long: "add calls POST /api/v1/kb to create or re-index one " +
			"kb entry under the operator's tenant. Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"--body accepts inline text, @<path> to read a file, or " +
			"@- to read from stdin. Trailing newlines from file / " +
			"stdin reads are stripped (a 1-line file passed via @ " +
			"doesn't carry a gratuitous final newline through the " +
			"JSON body); embedded newlines are preserved.\n\n" +
			"--metadata accepts a comma-separated list of key=value " +
			"pairs (`--metadata source=runbook,owner=ops`). Values " +
			"are stored as strings; operators needing richer metadata " +
			"types (lists, nested objects) should construct the JSON " +
			"body via the REST surface directly.\n\n" +
			"The substrate's body-hash short-circuit means re-creating " +
			"an entry with an unchanged body pays only an updated_at " +
			"bump — `meho kb add` against an existing slug with the " +
			"same body is effectively a no-op.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runAdd(cmd, addOptions{
				Slug:              args[0],
				BodyArg:           body,
				MetadataArg:       metadata,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&body, "body", "",
		"entry body: inline text, @<path> to read a file, or @- to read from stdin")
	cmd.Flags().StringVar(&metadata, "metadata", "",
		"comma-separated key=value pairs to attach as entry metadata (e.g. owner=ops,source=runbook)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw KbEntry JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type addOptions struct {
	Slug              string
	BodyArg           string
	MetadataArg       string
	JSONOut           bool
	BackplaneOverride string
}

func runAdd(cmd *cobra.Command, opts addOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("add requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	body, err := loadBodyFlag(cmd, opts.BodyArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	metadata, err := parseMetadataFlag(opts.MetadataArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	entry, err := postAdd(cmd.Context(), backplaneURL, opts.Slug, body, metadata)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printAddSummary(cmd.OutOrStdout(), entry)
	return nil
}

func postAdd(
	ctx context.Context,
	backplaneURL, slug, body string,
	metadata map[string]any,
) (*KbEntry, error) {
	req := kbEntryCreateRequest{Slug: slug, Body: body, Metadata: metadata}
	raw, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal kb add request: %w", err)
	}
	resp, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/kb", raw)
	if err != nil {
		return nil, err
	}
	var out KbEntry
	if err := json.Unmarshal(resp, &out); err != nil {
		return nil, fmt.Errorf("decode kb add response: %w", err)
	}
	return &out, nil
}

// printAddSummary renders the created entry as a compact one-line
// confirmation plus the round-tripped slug / timestamps. Operators
// who want the full body should chase with `meho kb show <slug>`.
func printAddSummary(w io.Writer, e *KbEntry) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "created kb entry %q\n", e.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "id:", e.ID)
	fmt.Fprintf(w, "%-14s %s\n", "tenant_id:", e.TenantID)
	fmt.Fprintf(w, "%-14s %s\n", "created_at:", e.CreatedAt)
	fmt.Fprintf(w, "%-14s %s\n", "updated_at:", e.UpdatedAt)
	fmt.Fprintf(w, "%-14s %d bytes\n", "body:", len(e.Body))
}
