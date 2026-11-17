// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// scriptShownBelow replaces the script text in the printed preview JSON,
// because printScript prints the text line by line.
const scriptShownBelow = "(shown below under \"Script to run\")"

// scriptPreview returns the preview of a request whose preview carries a
// script (linux.script.run), or nil.
func scriptPreview(effect map[string]interface{}) map[string]interface{} {
	preview, ok := effect["preview"].(map[string]interface{})
	if !ok {
		return nil
	}
	_, hasScript := preview["script"].(string)
	_, unavailable := preview["script_unavailable"].(string)
	if !hasScript && !unavailable {
		return nil
	}
	return preview
}

// effectForPrint returns effect with the script text of its preview
// replaced by a pointer to the "Script to run" block. effect itself is
// not changed.
func effectForPrint(effect map[string]interface{}) map[string]interface{} {
	preview := scriptPreview(effect)
	if preview == nil {
		return effect
	}
	if _, ok := preview["script"].(string); !ok {
		return effect
	}
	shownPreview := make(map[string]interface{}, len(preview))
	for k, v := range preview {
		shownPreview[k] = v
	}
	shownPreview["script"] = scriptShownBelow
	shown := make(map[string]interface{}, len(effect))
	for k, v := range effect {
		shown[k] = v
	}
	shown["preview"] = shownPreview
	return shown
}

// printEffect prints the proposed effect as indented JSON under label, and
// then the script block when the preview carries a script.
func printEffect(w io.Writer, indent, label string, effect map[string]interface{}) {
	b, _ := json.MarshalIndent(effectForPrint(effect), indent+"  ", "  ")
	fmt.Fprintf(w, "%s%s\n%s  %s\n", indent, label, indent, output.SafeLines(string(b)))
	printScript(w, indent, effect)
}

// printScript prints the script of a script-run request: its size, its
// SHA-256, the reviewed copy and the hash check, then the text with "| "
// in front of each line. The text is made safe for the terminal line by
// line; a tab stays a tab.
func printScript(w io.Writer, indent string, effect map[string]interface{}) {
	preview := scriptPreview(effect)
	if preview == nil {
		return
	}
	fmt.Fprintf(w, "%sScript to run:\n", indent)
	if size, ok := preview["script_bytes"].(float64); ok {
		fmt.Fprintf(w, "%s  Size:             %d bytes\n", indent, int64(size))
	}
	for _, field := range []struct{ key, label string }{
		{"script_sha256", "SHA-256:         "},
		{"script_ref", "Reviewed copy:   "},
		{"script_check", "Expected SHA-256:"},
	} {
		if v, ok := preview[field.key].(string); ok && v != "" {
			fmt.Fprintf(w, "%s  %s %s\n", indent, field.label, output.SafeText(v))
		}
	}
	if hidden, _ := preview["script_values_hidden"].(bool); hidden {
		fmt.Fprintf(w, "%s  Values that look like secrets are hidden below. MEHO shows no SHA-256 for this script.\n", indent)
	}
	if page, pages, ok := scriptPageOf(preview); ok && preview["script_page"] != nil {
		fmt.Fprintf(w, "%s  Page:             %d of %d\n", indent, page, pages)
	} else if cut, _ := preview["script_truncated"].(bool); cut {
		if sha, _ := preview["script_sha256"].(string); sha != "" {
			fmt.Fprintf(w, "%s  The script is longer than the text below. Compare its SHA-256 with a reviewed copy.\n", indent)
		} else {
			fmt.Fprintf(w, "%s  The script is longer than the text below. Read the full script in a reviewed copy before you approve.\n", indent)
		}
	}
	script, ok := preview["script"].(string)
	if !ok {
		reason, _ := preview["script_unavailable"].(string)
		fmt.Fprintf(w, "%s  %s\n", indent, output.SafeText(reason))
		return
	}
	for _, line := range strings.Split(strings.TrimSuffix(script, "\n"), "\n") {
		parts := strings.Split(line, "\t")
		for i, part := range parts {
			parts[i] = output.SafeText(part)
		}
		fmt.Fprintf(w, "%s  | %s\n", indent, strings.Join(parts, "\t"))
	}
}

// scriptPageOf returns the page and the page count of a preview, when it
// has a page count. The page is 1 when the preview names no page (the
// preview of a request is page 1). The numbers come from JSON (float64) or
// from withScriptPage (int).
func scriptPageOf(preview map[string]interface{}) (page, pages int, ok bool) {
	pages, ok = intField(preview["script_pages"])
	if !ok {
		return 0, 0, false
	}
	page, found := intField(preview["script_page"])
	if !found {
		page = 1
	}
	return page, pages, true
}

func intField(v interface{}) (int, bool) {
	switch n := v.(type) {
	case float64:
		return int(n), true
	case int:
		return n, true
	}
	return 0, false
}

// printScriptPagesHint names the flag that reads the other pages. It
// prints only for a script of more than one page, and not when a page was
// asked for already.
func printScriptPagesHint(w io.Writer, id string, effect map[string]interface{}) {
	preview := scriptPreview(effect)
	if preview == nil || preview["script_page"] != nil {
		return
	}
	if _, pages, ok := scriptPageOf(preview); ok && pages > 1 {
		fmt.Fprintf(w, "The script has %d pages. Read page N with: meho approvals show %s --script-page N\n", pages, id)
	}
}

// withScriptPage returns effect with the script fields of its preview
// replaced by those of page. effect itself is not changed. A effect
// without a script preview is returned as it is.
func withScriptPage(effect map[string]interface{}, page *api.ApprovalScriptPageView) map[string]interface{} {
	preview := scriptPreview(effect)
	if preview == nil {
		return effect
	}
	shownPreview := make(map[string]interface{}, len(preview)+2)
	for k, v := range preview {
		switch k {
		case "script", "script_sha256", "script_values_hidden", "script_unavailable", "script_truncated":
		default:
			shownPreview[k] = v
		}
	}
	shownPreview["script_bytes"] = page.ScriptBytes
	shownPreview["script_page"] = page.Page
	shownPreview["script_pages"] = page.Pages
	if page.Script != nil {
		shownPreview["script"] = *page.Script
	}
	if page.ScriptSha256 != nil {
		shownPreview["script_sha256"] = *page.ScriptSha256
	}
	if page.ScriptValuesHidden != nil && *page.ScriptValuesHidden {
		shownPreview["script_values_hidden"] = true
	}
	if page.ScriptUnavailable != nil {
		shownPreview["script_unavailable"] = *page.ScriptUnavailable
	}
	shown := make(map[string]interface{}, len(effect))
	for k, v := range effect {
		shown[k] = v
	}
	shown["preview"] = shownPreview
	return shown
}

// fetchScriptPage reads one page of the script of a waiting request, with
// the same one-shot 401 retry as fetchDetail.
func fetchScriptPage(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
	page int,
) (*api.ApprovalScriptPageView, error) {
	params := &api.GetApprovalScriptPageApiV1ApprovalsRequestIdScriptGetParams{Page: &page}
	resp, err := client.GetApprovalScriptPageApiV1ApprovalsRequestIdScriptGetWithResponse(ctx, requestID, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetApprovalScriptPageApiV1ApprovalsRequestIdScriptGetWithResponse(ctx, requestID, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		return nil, errors.New("backplane returned 200 OK but no JSON body decoded against ApprovalScriptPageView")
	}
	return resp.JSON200, nil
}

// renderScriptPageError prints a failed page read. The backplane answers
// 404 and 410 with a plain sentence in "detail" (no script on the request,
// no such page, MEHO no longer keeps the script): print that sentence.
func renderScriptPageError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	var he *httpResponseError
	if errors.As(err, &he) && (he.statusCode == http.StatusNotFound || he.statusCode == http.StatusGone) {
		var body struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal(he.body, &body) == nil && body.Detail != "" &&
			body.Detail != "approval_request_not_found" {
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(body.Detail), jsonOut)
		}
	}
	return routeRequestError(cmd, backplaneURL, err, jsonOut)
}
