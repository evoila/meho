// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package operation hosts the cobra commands under `meho operation ...`
// for the G0.6 substrate's three meta-tool routes (Initiative #388).
// v0.2 ships:
//
//   - `meho operation groups <connector_id>` — list enabled
//     operation groups via GET /api/v1/operations/groups.
//   - `meho operation search <connector_id> "<query>" [--group K] [--limit N]`
//     — hybrid BM25 + cosine RRF over endpoint_descriptor rows via
//     GET /api/v1/operations/search.
//   - `meho operation call <connector_id> <op_id> --target <slug> [--params ...]`
//     — invoke the dispatcher via POST /api/v1/operations/call.
//
// Each verb wraps one backplane route and renders the response in
// either a human-readable table or `--json` mode. Authentication
// piggybacks on the token meho login wrote — api.NewAuthedClient
// handles the bearer injection + 401 refresh dance via the generated
// typed client, same as `meho status` and the rest of the G0.12 CLI
// hygiene migration (Initiative #1118).
//
// The fourth route `GET /api/v1/operations/{descriptor_id}` (tenant-
// admin diagnostic) is deferred — the DoD line for G0.6-T13 #481 was
// "three CLI verbs", not four.
package operation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho operation` parent command. The
// command is grafted onto the top-level meho command tree by
// cmd/root.go alongside `meho retrieval`. The parent itself takes
// no args and prints its own help; every piece of behaviour lives
// in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "operation",
		Short:        "G0.6 operation meta-tool surface (groups / search / call)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newGroupsCmd())
	cmd.AddCommand(newSearchCmd())
	cmd.AddCommand(newCallCmd())
	return cmd
}

// operationsAPI is the minimal slice of api.ClientWithResponsesInterface
// the three operation verbs consume, plus the Refresh hook the
// per-verb 401-retry path invokes. Defined as a per-package interface
// so the test suite can substitute a tiny fake without reaching for
// the full ~140-method generated surface. *api.AuthedClient satisfies
// this directly: it embeds *ClientWithResponses (which provides the
// three *WithResponse calls) and defines Refresh of its own.
type operationsAPI interface {
	PostCallApiV1OperationsCallPostWithResponse(
		ctx context.Context,
		params *api.PostCallApiV1OperationsCallPostParams,
		body api.PostCallApiV1OperationsCallPostJSONRequestBody,
		reqEditors ...api.RequestEditorFn,
	) (*api.PostCallApiV1OperationsCallPostResponse, error)
	GetGroupsApiV1OperationsGroupsGetWithResponse(
		ctx context.Context,
		params *api.GetGroupsApiV1OperationsGroupsGetParams,
		reqEditors ...api.RequestEditorFn,
	) (*api.GetGroupsApiV1OperationsGroupsGetResponse, error)
	GetSearchApiV1OperationsSearchGetWithResponse(
		ctx context.Context,
		params *api.GetSearchApiV1OperationsSearchGetParams,
		reqEditors ...api.RequestEditorFn,
	) (*api.GetSearchApiV1OperationsSearchGetResponse, error)
	Refresh(ctx context.Context) error
}

// newAuthedClient is the production-mode factory the verbs default to
// — overridden in tests via newAuthedClientForTest. Returning the
// operationsAPI interface (not *api.AuthedClient) keeps the call
// sites typed against the per-package seam, not the generated client.
var newAuthedClient = func(ctx context.Context, backplaneURL string) (operationsAPI, error) {
	return api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
}

// apiResponseError carries a non-2xx response so renderRequestError
// can pick the right output category (401 → auth_expired, other
// non-2xx → unexpected_response). Constructed from the generated
// *Response.HTTPResponse.StatusCode + Body fields after the per-verb
// 401-retry path has exhausted its refresh shot.
type apiResponseError struct {
	StatusCode int
	Body       string
}

func (e *apiResponseError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.RenderError category. Same
// classification ladder as retrieval/eval.go: token-not-found and
// no-refresh-token surface as auth_expired with `meho login` hints;
// a non-2xx response from the backplane after auth refresh wraps as
// *apiResponseError so the renderer can split 401 from the rest;
// everything else (transport / DNS / TLS) falls through to unreachable.
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	// HTTP-level errors from the backplane (a non-2xx response with a
	// body) come back wrapped in *apiResponseError. 401 here means the
	// one-shot refresh either was not attempted (Refresh failure was
	// already surfaced upstream) or returned a fresh token that the
	// backplane still rejected — either way the operator needs to
	// re-login, so surface as auth_expired (exit 2). Every other non-2xx
	// is a structured backplane disagreement, not a transport failure
	// — classify as unexpected_response (exit 4) so the operator sees
	// the "this is a backend disagreement" hint rather than the "your
	// network is down" one. Pure transport errors (timeouts, DNS,
	// connection refused) still fall through to unreachable (exit 3).
	var apiErr *apiResponseError
	if errors.As(err, &apiErr) {
		if apiErr.StatusCode == http.StatusUnauthorized {
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf(
					"backplane rejected the refreshed token; run `meho login %s`",
					backplaneURL,
				)),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, apiErr.StatusCode, apiErr.Body)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// classifyNon2xx wraps the generated *Response fields into the local
// apiResponseError sentinel renderRequestError extracts via
// errors.As. Pulled out so the three verbs share one body-truncation
// pass — the backplane error responses are JSON envelopes well under
// the 1 MiB ceiling, but maxBodyBytes matches the previous
// pre-migration transport's response read. Name is deliberately not
// `cap` because the revive `redefines-builtin-id` rule (enabled in
// cli/.golangci.yml) treats shadowing the `cap` builtin as a lint error.
func classifyNon2xx(resp *http.Response, body []byte) *apiResponseError {
	const maxBodyBytes = 1 << 20 // 1 MiB
	b := body
	if len(b) > maxBodyBytes {
		b = b[:maxBodyBytes]
	}
	return &apiResponseError{
		StatusCode: resp.StatusCode,
		Body:       strings.TrimSpace(string(b)),
	}
}

// loadParamsFlag parses the --params flag value. Prefixing with '@'
// loads the named file as JSON; otherwise the value itself is parsed
// as inline JSON. Returns nil for an empty value so the caller can
// omit the "params" key from the JSON body.
func loadParamsFlag(val string) (map[string]any, error) {
	if val == "" {
		return nil, nil
	}
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read params file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse params JSON: %w", err)
	}
	return m, nil
}
