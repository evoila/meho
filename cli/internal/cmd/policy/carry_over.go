// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package policy

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newCarryOverCmd() *cobra.Command {
	var opts fileOptions
	var dryRun bool
	cmd := &cobra.Command{
		Use:   "carry-over --dry-run",
		Short: "List every saved \"always wait\" and what changes if MEHO reads it as destructive",
		Long: "carry-over --dry-run calls GET /api/v1/policy/carry-over/dry-run. " +
			"It lists every saved \"always wait\" setting: an override with " +
			"approval on, or a policy file entry with always_wait: true. For each " +
			"one it shows where it comes from, the operations it matches, and " +
			"what changes for each setting if MEHO reads it as destructive: a " +
			"step of a multi-step operation is refused, sandbox mode, and the " +
			"preview a call needs first. It only reads and reads no switch. " +
			"Nothing is changed.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if !dryRun {
				return output.RenderError(cmd.ErrOrStderr(), output.Usage(
					"carry-over only lists; add --dry-run"), opts.JSONOut)
			}
			return runCarryOver(cmd, opts)
		},
	}
	opts.bind(cmd)
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "list the settings; change nothing (required)")
	return cmd
}

func runCarryOver(cmd *cobra.Command, opts fileOptions) error {
	if err := opts.check(); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getCarryOver(cmd.Context(), backplaneURL, opts.Scope)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	var answer api.CarryOverDryRun
	if err := json.Unmarshal(resp.Body, &answer); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode the answer: %v", err)), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), json.RawMessage(resp.Body))
	}
	printCarryOver(cmd.OutOrStdout(), answer)
	return nil
}

func getCarryOver(ctx context.Context, backplaneURL, scope string) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	s := api.CarryOverApiV1PolicyCarryOverDryRunGetParamsScope(scope)
	return doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		return authed.CarryOverApiV1PolicyCarryOverDryRunGet(ctx,
			&api.CarryOverApiV1PolicyCarryOverDryRunGetParams{Scope: &s})
	})
}

func printCarryOver(w io.Writer, answer api.CarryOverDryRun) {
	if answer.CustomPolicyOn {
		fmt.Fprintln(w, "`CUSTOM_POLICY` is on.")
	} else {
		fmt.Fprintln(w, "`CUSTOM_POLICY` is off: the list shows what waits once it is on.")
	}
	if answer.SandboxMode {
		fmt.Fprintln(w, "This install is in sandbox mode.")
	}
	if len(answer.Settings) == 0 {
		fmt.Fprintf(w, "No saved \"always wait\" setting in scope %s.\n", sanitize(string(answer.Scope)))
	}
	for _, setting := range answer.Settings {
		fmt.Fprintf(w, "%s %s (%s): %s\n", sanitize(string(setting.Source)),
			sanitize(setting.Ref), sanitize(string(setting.Scope)), sanitize(describeMatch(setting.Match)))
		for _, op := range setting.Operations {
			line := fmt.Sprintf("  operation %s %s", sanitize(op.ConnectorId), sanitize(op.OpId))
			if len(op.StepOf) > 0 {
				line += " (a step of " + sanitize(strings.Join(op.StepOf, ", ")) + ")"
			}
			fmt.Fprintln(w, line)
		}
		for _, warning := range setting.Warnings {
			fmt.Fprintf(w, "  warning: %s\n", sanitize(warning))
		}
	}
	if answer.Message != nil {
		fmt.Fprintln(w, sanitize(*answer.Message))
	}
}

// describeMatch renders the match of a setting in a fixed order.
func describeMatch(match map[string]string) string {
	var parts []string
	for _, key := range []string{"connector", "group", "op"} {
		if value, ok := match[key]; ok {
			parts = append(parts, key+"="+value)
		}
	}
	return strings.Join(parts, " ")
}
