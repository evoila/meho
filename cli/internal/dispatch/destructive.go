// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// PreviewAndCall runs an op on the destructive risk level in one step.
// The backplane accepts such a call only with the preview_hash of a
// preview of the same op, target and params. So this method previews
// first, then sends the call with that hash. The call then waits until a
// second person approves it.
//
// If the preview does not come back with status=ok and a hash, no call is
// sent: the CallResult is nil and the PreviewResult says why. Pass it to
// RenderPreviewFailure.
func (c Connector) PreviewAndCall(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*CallResult, *PreviewResult, error) {
	p, err := c.Preview(ctx, backplaneURL, opID, targetSlug, params)
	if err != nil {
		return nil, nil, err
	}
	if p.Status != "ok" || p.PreviewHash == "" {
		return nil, p, nil
	}
	r, err := c.CallWithPreviewHash(ctx, backplaneURL, opID, targetSlug, params, p.PreviewHash)
	if err != nil {
		return nil, p, err
	}
	return r, p, nil
}

// RenderPreviewFailure prints a preview that stopped PreviewAndCall
// before the call, and returns ErrOpError (exit 1). Under --json it
// prints the whole preview envelope. A status outside ok / error /
// unavailable is an unexpected response (exit 4).
func (c Connector) RenderPreviewFailure(
	cmd *cobra.Command,
	opID string,
	p *PreviewResult,
	jsonOut bool,
) error {
	switch p.Status {
	case "ok", "error", "unavailable":
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid preview status %q (expected one of: ok / error / unavailable)",
				p.Status)),
			jsonOut)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), p.Raw); err != nil {
			return err
		}
		return ErrOpError
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "%s %s — preview status=%s\n", c.ID, opID, p.Status)
	fmt.Fprintln(w, "  The preview failed. The call was not sent, and nothing waits for approval.")
	switch {
	case p.Error != nil && *p.Error != "":
		fmt.Fprintf(w, "meho: preview %s: %s\n", p.Status, *p.Error)
	case p.Status == "ok":
		fmt.Fprintln(w, "meho: the preview returned no preview_hash")
	}
	if len(p.Extras) > 0 && string(p.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		if pretty, err := PrettyJSON(p.Extras); err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(p.Extras))
		}
	}
	return ErrOpError
}
