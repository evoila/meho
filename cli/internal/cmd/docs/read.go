// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// readAroundMax is the most chunks `--before` / `--after` may ask for. It
// mirrors the backplane's bound (0..3) so the CLI fails fast locally.
const readAroundMax = 3

// readDefaultAround is the server's default for --before / --after.
const readDefaultAround = 1

// newReadCmd returns the `meho docs read` command.
//
// CLI shape:
//
//	meho docs read <read-handle> --collection <c> \
//	  [--mode around|page|section] [--before N] [--after N] \
//	  [--cursor <next>] [--product <p>] [--version <v>] [--json]
//
// Role: operator. Calls POST /api/v1/read_docs with the opaque
// read_handle a `meho docs search --json` hit (or an ask_docs citation)
// carries, and prints the text around the hit, the whole page, or the
// hit's section. The response is read under the same 1 MiB cap as every
// docs verb (responseBodyCap).
//
// Exit codes:
//   - 0   the read returned (including a link-only file, which prints its link)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (not found, search again, rate limited,
//     unavailable, a bad flag)
//   - 5   insufficient_role (a read_only operator)
func newReadCmd() *cobra.Command {
	var opts readOptions
	cmd := &cobra.Command{
		Use:   "read <read-handle>",
		Short: "Read the text around a docs hit (mandatory --collection)",
		Long: "read calls POST /api/v1/read_docs and prints the text around " +
			"a docs hit: the chunks before and after it (--mode around, the " +
			"default; --before / --after 0-3), the whole page (--mode page), " +
			"or the hit's section (--mode section). Pass the hit's " +
			"read_handle (shown by `meho docs search --json` when the " +
			"collection supports reading) and the same --collection. When " +
			"there is more, the output ends with a `next:` cursor; run the " +
			"same command with --cursor <next> to read on. Some files allow " +
			"only their link: then the command prints the link and no text. " +
			"Any refusal (an unknown or not-entitled collection, a collection " +
			"without reading, an unknown handle) is the same \"docs source not " +
			"found\" error. A handle expires: on a \"search again\" error, run " +
			"`meho docs search` again. --product / --version are needed only " +
			"on a collection that applies scope filters: pass the values you " +
			"searched with, and only when the hit came from a search of one " +
			"collection. Leave them out for a hit from a cross-collection " +
			"search (--collection all, or --collection given more than once): " +
			"that search ignores --product / --version, so its handle has " +
			"none. --json emits the raw DocsReadResult.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.ReadHandle = args[0]
			return runRead(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.Collection, "collection", "",
		"collection key the hit came from (required; e.g. vmware)")
	cmd.Flags().StringVar(&opts.Mode, "mode", "around",
		"what to read: around (default), page or section")
	cmd.Flags().IntVar(&opts.Before, "before", readDefaultAround,
		"chunks to read before the hit, for --mode around (0..3)")
	cmd.Flags().IntVar(&opts.After, "after", readDefaultAround,
		"chunks to read after the hit, for --mode around (0..3)")
	cmd.Flags().StringVar(&opts.Cursor, "cursor", "",
		"the `next` cursor of an earlier read, to read on")
	cmd.Flags().StringVar(&opts.Product, "product", "",
		"the product you searched with (only for a collection that applies scope filters; "+
			"leave it out for a hit from a cross-collection search)")
	cmd.Flags().StringVar(&opts.Version, "version", "",
		"the version you searched with (only for a collection that applies scope filters; "+
			"leave it out for a hit from a cross-collection search)")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false,
		"emit the raw DocsReadResult JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type readOptions struct {
	ReadHandle        string
	Collection        string
	Mode              string
	Before            int
	After             int
	Cursor            string
	Product           string
	Version           string
	JSONOut           bool
	BackplaneOverride string
}

// validReadModes is the set --mode accepts (the server's ReadDocsRequest.mode).
var validReadModes = map[string]api.ReadDocsRequestMode{
	"around":  api.ReadDocsRequestModeAround,
	"page":    api.ReadDocsRequestModePage,
	"section": api.ReadDocsRequestModeSection,
}

func runRead(cmd *cobra.Command, opts readOptions) error {
	if msg := validateReadOptions(opts); msg != "" {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := readDocs(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderReadHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a read_docs response payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printReadResult(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// validateReadOptions fails fast on the constraints the route would 422 on.
// It returns "" when the options are valid. The read handle and the cursor
// are never echoed: they carry a few words of the hit.
func validateReadOptions(opts readOptions) string {
	switch {
	case strings.TrimSpace(opts.ReadHandle) == "":
		return "read requires a non-empty <read-handle> argument"
	case strings.TrimSpace(opts.Collection) == "":
		return "read requires --collection (the collection the hit came from)"
	case validReadModes[opts.Mode] == "":
		return fmt.Sprintf("--mode must be around, page or section; got %q", opts.Mode)
	case opts.Before < 0 || opts.Before > readAroundMax:
		return fmt.Sprintf("--before must be between 0 and %d; got %d", readAroundMax, opts.Before)
	case opts.After < 0 || opts.After > readAroundMax:
		return fmt.Sprintf("--after must be between 0 and %d; got %d", readAroundMax, opts.After)
	}
	return ""
}

// buildReadBody assembles the typed POST body for /api/v1/read_docs. The
// optional fields land only when set, so the server's defaults apply on
// absence.
func buildReadBody(opts readOptions) api.ReadDocsRequest {
	collection := strings.TrimSpace(opts.Collection)
	mode := validReadModes[opts.Mode]
	before := opts.Before
	after := opts.After
	body := api.ReadDocsRequest{
		ReadHandle: opts.ReadHandle,
		Collection: &collection,
		Mode:       &mode,
		Before:     &before,
		After:      &after,
	}
	if opts.Cursor != "" {
		cursor := opts.Cursor
		body.Cursor = &cursor
	}
	if opts.Product != "" {
		product := opts.Product
		body.Product = &product
	}
	if opts.Version != "" {
		version := opts.Version
		body.Version = &version
	}
	return body
}

func readDocs(
	ctx context.Context,
	backplaneURL string,
	opts readOptions,
) (*api.ReadDocsEndpointApiV1ReadDocsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildReadBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ReadDocsEndpointApiV1ReadDocsPostResponse, error) {
			return authed.ReadDocsEndpointApiV1ReadDocsPostWithResponse(
				ctx,
				&api.ReadDocsEndpointApiV1ReadDocsPostParams{},
				reqBody,
			)
		},
		func(r *api.ReadDocsEndpointApiV1ReadDocsPostResponse) int { return r.StatusCode() },
	)
}

// renderReadHTTPStatus maps the read route's own statuses to plain
// messages, and hands the rest to the shared docs renderer:
//
//   - 404 → "docs source not found" (every refusal, one message).
//   - 409 → search again: the handle is too old or the page changed.
//   - 429 → too many reads, with the Retry-After wait when sent.
//   - 503 → the collection is not ready, or its backend is unavailable.
func renderReadHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	resp *api.ReadDocsEndpointApiV1ReadDocsPostResponse,
	jsonOut bool,
) error {
	switch resp.StatusCode() {
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("docs source not found"),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("this read handle is too old or the page has changed; "+
				"run `meho docs search` again and use the new read_handle"),
			jsonOut,
		)
	case http.StatusTooManyRequests:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(rateLimitedMessage(resp.HTTPResponse)),
			jsonOut,
		)
	case http.StatusServiceUnavailable:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"docs read is unavailable right now; try again later: %s",
				detailMessage(string(resp.Body)),
			)),
			jsonOut,
		)
	default:
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
	}
}

// rateLimitedMessage builds the 429 message, naming the Retry-After wait
// when the backplane sent a whole number of seconds.
func rateLimitedMessage(httpResp *http.Response) string {
	if httpResp != nil {
		if raw := strings.TrimSpace(httpResp.Header.Get("Retry-After")); raw != "" {
			if seconds, err := strconv.Atoi(raw); err == nil && seconds >= 0 {
				return fmt.Sprintf("too many docs reads; wait %d seconds and try again", seconds)
			}
		}
	}
	return "too many docs reads; wait a moment and try again"
}

// detailMessage returns the `detail.message` of a structured error body,
// else the shared plain-detail decoding.
func detailMessage(body string) string {
	var outer detailEnvelope
	if err := json.Unmarshal([]byte(body), &outer); err == nil {
		var inner struct {
			Message string `json:"message"`
		}
		if err := json.Unmarshal(outer.Detail, &inner); err == nil && inner.Message != "" {
			return inner.Message
		}
	}
	return decodeDetailString(body)
}

// printReadResult renders a read as plain text: the title and source link
// when known, the text (or why there is none), then the `next` cursor
// when there is more to read.
func printReadResult(w io.Writer, r *api.DocsReadResult) {
	if r.Title != nil && *r.Title != "" {
		fmt.Fprintf(w, "title:  %s\n", *r.Title)
	}
	if r.SourceUrl != nil && *r.SourceUrl != "" {
		fmt.Fprintf(w, "source: %s\n", *r.SourceUrl)
	}
	fmt.Fprintln(w)
	switch {
	case r.Text != nil:
		fmt.Fprintln(w, strings.TrimRight(*r.Text, "\n"))
	case r.Disclosure == api.DocsReadResultDisclosureLink:
		fmt.Fprintln(w, "The owner of this file allows only its link. Open the source to read it.")
	default:
		fmt.Fprintln(w, "This file cannot be shown as text. Open the source to read it.")
	}
	if r.Truncated != nil && *r.Truncated {
		fmt.Fprintln(w)
		fmt.Fprintln(w, "(the text was cut at the size limit)")
	}
	if r.Next != nil && *r.Next != "" {
		fmt.Fprintln(w)
		fmt.Fprintf(w, "next: %s\n", *r.Next)
	}
}
