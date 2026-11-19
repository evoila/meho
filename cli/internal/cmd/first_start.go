// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// firstStartTimeout bounds the GET /api/v1/first-start call. The
// backplane runs its own checks (each with a short limit) before it
// answers.
const firstStartTimeout = 30 * time.Second

// runFirstStart implements `meho status --first-start`. It reads the
// list with the login stored by `meho login`: the backplane shows the
// list only to a signed-in person with at least the operator role. With
// --json it prints the backplane's answer as it is. A red line makes the
// exit code 6.
func runFirstStart(cmd *cobra.Command, backplaneURL string, jsonOut bool) error {
	ctx, cancel := context.WithTimeout(cmd.Context(), firstStartTimeout)
	defer cancel()
	stderr := cmd.ErrOrStderr()
	client, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		if api.IsTokenNotFound(err) {
			return output.RenderError(stderr, output.AuthExpired(fmt.Sprintf(
				"the first-start list needs a login; run `meho login %s`, then run this command again, or open %s/ui/first-start in the console",
				backplaneURL, backplaneURL)), jsonOut)
		}
		return output.RenderError(stderr, output.Unexpected(fmt.Sprintf("build authed client: %v", err)), jsonOut)
	}
	resp, err := client.GetFirstStart(ctx)
	if err != nil {
		if api.IsRefreshRejected(err) || api.IsNoRefreshToken(err) {
			return output.RenderError(stderr, output.AuthExpired(fmt.Sprintf(
				"session expired; run `meho login %s`", backplaneURL)), jsonOut)
		}
		var syntaxErr *json.SyntaxError
		var typeErr *json.UnmarshalTypeError
		if errors.As(err, &syntaxErr) || errors.As(err, &typeErr) {
			return output.RenderError(stderr,
				output.Unexpected(fmt.Sprintf("decode %s/api/v1/first-start: %v", backplaneURL, err)), jsonOut)
		}
		return output.RenderError(stderr,
			output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, redactedError(err))), jsonOut)
	}
	switch resp.StatusCode() {
	case 401: // the stored login was refused
		return output.RenderError(stderr, output.AuthExpired(fmt.Sprintf(
			"backplane rejected stored credentials; run `meho login %s`", backplaneURL)), jsonOut)
	case http.StatusForbidden:
		return output.RenderError(stderr, output.InsufficientRole(
			"the first-start list needs the operator role in the current tenant"), jsonOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(stderr, output.Unexpected(fmt.Sprintf(
			"HTTP %d from %s/api/v1/first-start (an older backplane has no first-start checks behind a login)",
			resp.StatusCode(), backplaneURL)), jsonOut)
	}
	list := resp.JSON200
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), list); err != nil {
			return err
		}
	} else {
		printFirstStart(cmd.OutOrStdout(), list)
	}
	if !list.Ok {
		return output.RenderError(stderr, &output.StructuredError{
			Code:   output.ErrCodeFirstStartFailed,
			Detail: "at least one first-start check is red",
			Exit:   output.ExitFirstStartFailed,
		}, jsonOut)
	}
	return nil
}

// printFirstStart writes one line per check: GREEN, RED, WARN or SKIPPED, the
// check name and its sentence.
func printFirstStart(w io.Writer, list *api.FirstStartResponse) {
	for _, line := range list.Checks {
		label := "GREEN"
		switch line.Status {
		case "fail":
			label = "RED"
		case "warn":
			label = "WARN"
		case "skipped":
			label = "SKIPPED"
		}
		fmt.Fprintf(w, "%-7s %-13s %s\n", label, line.Name, line.Message)
	}
}
