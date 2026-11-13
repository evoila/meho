// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAskCmd returns the `meho docs ask` command.
//
//	meho docs ask <question> --collection <c> \
//	  [--product <p>] [--version <v>] [--limit N] [--json] [--backplane <url>]
//
// Role: operator. Calls POST /api/v1/ask_docs through the same login code as
// `meho docs search` (bearer + 401 refresh). There is no client-side access
// check (#2109): the backplane decides. ask_docs answers from exactly one
// collection, so --collection takes one key and never "all".
//
// Exit codes:
//   - 0   an answer was printed (also "no grounded answer")
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (bad arguments, 409, 422, 502, 503)
//   - 5   insufficient_role (read_only operator, or no entitlement)
func newAskCmd() *cobra.Command {
	var (
		collections       []string
		product           string
		version           string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "ask <question>",
		Short: "Ask one vendor-document collection a question (cited answer)",
		Long: "ask calls POST /api/v1/ask_docs and prints a cited answer, " +
			"its numbered sources and the path that wrote the answer " +
			"(local or upstream). --collection is required and takes " +
			"exactly one key: an answer comes from one collection only. " +
			"--product, --version and --limit are optional. The call " +
			"uses the login of `meho login`; the token is never printed. " +
			"--json prints the response as the server sent it " +
			"(answer, citations, answer_source).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runAsk(cmd, askOptions{
				Question:          args[0],
				Collections:       collections,
				Product:           product,
				Version:           version,
				Limit:             limit,
				Changed:           cmd.Flags().Changed("limit"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringArrayVar(&collections, "collection", nil,
		"collection key to ask (required; exactly one)")
	cmd.Flags().StringVar(&product, "product", "",
		"optional vendor-product refinement")
	cmd.Flags().StringVar(&version, "version", "",
		"optional product-version refinement")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max chunks to ground the answer on (1..50, server default 10 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw AskDocsResponse JSON (answer, citations, answer_source)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type askOptions struct {
	Question    string
	Collections []string
	Product     string
	Version     string
	Limit       int
	// Changed is true when the operator passed --limit, so an explicit 0 is
	// an error while the unset default 0 means "use the server default".
	Changed           bool
	JSONOut           bool
	BackplaneOverride string
}

// validateAsk checks the arguments before any call. It returns the message
// of the first problem, or "" when the arguments are fine.
func validateAsk(opts askOptions) string {
	switch {
	case strings.TrimSpace(opts.Question) == "":
		return "ask requires a non-empty <question> argument"
	case len(opts.Collections) == 0:
		return "ask requires --collection (the mandatory binary scope)"
	case len(opts.Collections) > 1:
		return "ask takes exactly one --collection: an answer comes from one collection only"
	case opts.Collections[0] == allCollectionsSentinel:
		return "ask does not accept --collection all: an answer comes from one collection only"
	case opts.Limit < 0 || opts.Limit > 50 || (opts.Changed && opts.Limit == 0):
		return fmt.Sprintf("--limit must be between 1 and 50 when provided; got %d", opts.Limit)
	}
	return ""
}

func runAsk(cmd *cobra.Command, opts askOptions) error {
	if msg := validateAsk(opts); msg != "" {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := askDocs(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderAskHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without an ask_docs response payload", backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printAskAnswer(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// buildAskBody assembles the POST body. product, version and limit land on
// the wire only when set.
func buildAskBody(opts askOptions) api.AskDocsRequest {
	collection := opts.Collections[0]
	body := api.AskDocsRequest{Query: opts.Question, Collection: &collection}
	if opts.Product != "" {
		product := opts.Product
		body.Product = &product
	}
	if opts.Version != "" {
		version := opts.Version
		body.Version = &version
	}
	if opts.Limit > 0 {
		limit := opts.Limit
		body.Limit = &limit
	}
	return body
}

func askDocs(
	ctx context.Context,
	backplaneURL string,
	opts askOptions,
) (*api.AskDocsEndpointApiV1AskDocsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildAskBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.AskDocsEndpointApiV1AskDocsPostResponse, error) {
			return authed.AskDocsEndpointApiV1AskDocsPostWithResponse(
				ctx,
				&api.AskDocsEndpointApiV1AskDocsPostParams{},
				reqBody,
			)
		},
		func(r *api.AskDocsEndpointApiV1AskDocsPostResponse) int { return r.StatusCode() },
	)
}

// answerErrorDetail is the structured body of a 502 or 503 from ask_docs.
type answerErrorDetail struct {
	Leg            string `json:"leg"`
	Cause          string `json:"cause"`
	Message        string `json:"message"`
	UpstreamStatus *int   `json:"upstream_status"`
	RetryAfter     *int   `json:"retry_after"`
}

// renderAskHTTPStatus shows a non-200 answer. A 502 or 503 carries the
// structured leg, cause and message, which are printed as plain text. Every
// other status goes through the shared docs renderer.
func renderAskHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	resp *api.AskDocsEndpointApiV1AskDocsPostResponse,
	jsonOut bool,
) error {
	status := resp.StatusCode()
	if status != http.StatusBadGateway && status != http.StatusServiceUnavailable {
		return renderHTTPStatus(cmd, backplaneURL, status, resp.Body, jsonOut)
	}
	var env struct {
		Detail answerErrorDetail `json:"detail"`
	}
	if err := json.Unmarshal(resp.Body, &env); err != nil || env.Detail.Leg == "" {
		return renderHTTPStatus(cmd, backplaneURL, status, resp.Body, jsonOut)
	}
	d := env.Detail
	msg := fmt.Sprintf("the answer failed (HTTP %d, leg: %s, cause: %s)",
		status, sanitizeText(d.Leg), sanitizeText(d.Cause))
	if d.Message != "" {
		msg += ": " + sanitizeText(d.Message)
	}
	if d.UpstreamStatus != nil {
		msg += fmt.Sprintf(" (the backend answered HTTP %d)", *d.UpstreamStatus)
	}
	if wait := retryAfterSeconds(resp.HTTPResponse, d.RetryAfter); wait != "" {
		msg += fmt.Sprintf("; try again in %s seconds", wait)
	}
	return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), jsonOut)
}

// retryAfterSeconds returns the wait from the Retry-After header, or from
// the body's retry_after, or "" when neither is set.
func retryAfterSeconds(r *http.Response, fromBody *int) string {
	if r != nil {
		if h := strings.TrimSpace(r.Header.Get("Retry-After")); h != "" {
			return sanitizeText(h)
		}
	}
	if fromBody != nil {
		return fmt.Sprintf("%d", *fromBody)
	}
	return ""
}

// printAskAnswer renders the answer, then the numbered sources. The answer
// is built from untrusted document text, so every control character is
// replaced before printing (line breaks in the answer are kept).
func printAskAnswer(w io.Writer, r *api.AskDocsResponse) {
	source := "unknown"
	if r.AnswerSource != nil {
		source = string(*r.AnswerSource)
	}
	fmt.Fprintf(w, "Answer (source: %s)\n%s\n", sanitizeText(source), sanitizeBlock(r.Answer))
	if len(r.Citations) == 0 {
		return
	}
	fmt.Fprintln(w, "\nSources")
	for i, c := range r.Citations {
		line := fmt.Sprintf("[%d] %s", i+1, sanitizeText(citationTitle(c)))
		if href := citationHref(c); href != "" {
			line += "  " + sanitizeText(href)
		}
		fmt.Fprintln(w, line)
	}
}

// citationTitle picks the title, then the link label, then the chunk id.
func citationTitle(c map[string]interface{}) string {
	if t := stringField(c, "title"); t != "" {
		return t
	}
	if link, ok := c["link"].(map[string]interface{}); ok {
		if l := stringField(link, "label"); l != "" {
			return l
		}
	}
	return stringField(c, "chunk_id")
}

// citationHref returns the link href when the link has one.
func citationHref(c map[string]interface{}) string {
	link, ok := c["link"].(map[string]interface{})
	if !ok {
		return ""
	}
	return stringField(link, "href")
}

func stringField(m map[string]interface{}, key string) string {
	s, _ := m[key].(string)
	return s
}

// sanitizeText replaces every control character (including line breaks)
// with U+FFFD so untrusted text cannot drive the terminal.
func sanitizeText(s string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return '�'
		}
		return r
	}, s)
}

// sanitizeBlock is sanitizeText per line: it keeps the line breaks.
func sanitizeBlock(s string) string {
	lines := strings.Split(s, "\n")
	for i, line := range lines {
		lines[i] = sanitizeText(strings.TrimRight(line, "\r"))
	}
	return strings.Join(lines, "\n")
}
