// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// errCodeSimilarEntries is the error code of a create that wrote
// nothing because similar entries exist.
const errCodeSimilarEntries = "similar_entries"

// renderSimilarCheck handles the two answers of `POST /api/v1/kb` that
// wrote nothing: HTTP 409 (similar entries exist) and the HTTP 200 of a
// dry run. It reports whether it handled the answer, and the error to
// return (nil for a dry run).
func renderSimilarCheck(
	cmd *cobra.Command,
	backplaneURL string,
	resp *api.CreateKbApiV1KbPostResponse,
	opts addOptions,
) (bool, error) {
	status := resp.StatusCode()
	if status != http.StatusConflict && !(opts.DryRun && status == http.StatusOK) {
		return false, nil
	}
	check := resp.JSON409
	if status == http.StatusOK {
		check = decodeJSONBody[api.KbSimilarCheck](resp.HTTPResponse, resp.Body)
	}
	if check == nil {
		return true, output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP %d without a similar-entries answer", backplaneURL, status,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), check); err != nil {
			return true, err
		}
	} else {
		printSimilarCheck(cmd.OutOrStdout(), check)
	}
	if status == http.StatusOK {
		return true, nil
	}
	return true, output.RenderError(cmd.ErrOrStderr(), &output.StructuredError{
		Code: errCodeSimilarEntries,
		Detail: fmt.Sprintf(
			"%d similar entries exist, nothing was written; update one of them, "+
				"or run again with --create-anyway", len(check.Similar),
		),
		Exit: 1,
	}, opts.JSONOut)
}

// printSimilarCheck renders the similar entries as a short list. Stored
// text goes through output.SafeText so control characters stay visible.
func printSimilarCheck(w io.Writer, c *api.KbSimilarCheck) {
	switch {
	case c.Reason == api.KbSimilarCheckReasonSimilarEntries:
		fmt.Fprintf(w, "not written: similar entries exist for %q\n", c.Slug)
	case c.SlugExists:
		fmt.Fprintf(w, "dry run: %q exists, the write would update it\n", c.Slug)
	case c.WouldWrite:
		fmt.Fprintf(w, "dry run: the create of %q would go through\n", c.Slug)
	default:
		fmt.Fprintf(w, "dry run: the create of %q would not go through, similar entries exist\n", c.Slug)
	}
	for _, e := range c.Similar {
		fmt.Fprintf(w, "- %s  score %.2f", output.SafeText(e.Slug), e.Score)
		if e.Match == api.KbSimilarEntryMatchSameProductVersion {
			fmt.Fprint(w, ", same product and version")
		}
		if !e.MayChange {
			fmt.Fprint(w, ", you may not change it")
		}
		fmt.Fprintln(w)
		if e.Summary != nil && *e.Summary != "" {
			fmt.Fprintf(w, "  %s\n", output.SafeText(*e.Summary))
		}
	}
}

// decodeJSONBody decodes a JSON answer body into T, or returns nil when
// the answer is not JSON or does not decode.
func decodeJSONBody[T any](r *http.Response, body []byte) *T {
	if r == nil || !strings.Contains(r.Header.Get("Content-Type"), "json") {
		return nil
	}
	var v T
	if err := json.Unmarshal(body, &v); err != nil {
		return nil
	}
	return &v
}
