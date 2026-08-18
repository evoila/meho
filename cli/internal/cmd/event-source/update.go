// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package eventsource

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newUpdateCmd returns `meho event-source update <slug>`.
func newUpdateCmd() *cobra.Command {
	var (
		kind              string
		authStrategy      string
		statusFlag        string
		extrasJSON        string
		secretStdin       bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "update <slug>",
		Short: "Apply a partial update to one event source (tenant_admin)",
		Long: "update calls PATCH /api/v1/event-sources/{slug}. Only the flags " +
			"you set are sent. name and slug are immutable. --status paused " +
			"takes effect on the ingest path immediately. Rotate the secret " +
			"with --secret-stdin or " + SecretEnvVar + ".",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body := map[string]any{}
			fs := cmd.Flags()
			if fs.Changed("kind") {
				body["kind"] = kind
			}
			if fs.Changed("auth-strategy") {
				body["auth_strategy"] = authStrategy
			}
			if fs.Changed("status") {
				body["status"] = statusFlag
			}
			if fs.Changed("extras") {
				var extras map[string]any
				if err := json.Unmarshal([]byte(extrasJSON), &extras); err != nil {
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected(fmt.Sprintf("invalid --extras JSON: %v", err)), jsonOut)
				}
				body["extras"] = extras
			}
			secret, hasSecret, err := resolveSecret(cmd, secretStdin)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			if hasSecret {
				body["secret"] = secret
			}
			if len(body) == 0 {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("update requires at least one field flag or a secret to rotate"), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			payload, err := json.Marshal(body)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("marshal request body: %v", err)), jsonOut)
			}
			path := "/api/v1/event-sources/" + pathEscape(args[0])
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodPatch, path, payload)
			if err != nil {
				return renderRequestErr(cmd, backplaneURL, err, jsonOut)
			}
			var es EventSource
			if err := json.Unmarshal(raw, &es); err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("decode updated event source: %v", err)), jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), &es)
			}
			printEventSource(cmd.OutOrStdout(), &es)
			return nil
		},
	}
	cmd.Flags().StringVar(&kind, "kind", "", "new producer kind")
	cmd.Flags().StringVar(&authStrategy, "auth-strategy", "", "new auth strategy")
	cmd.Flags().StringVar(&statusFlag, "status", "", "new status: active | paused")
	cmd.Flags().StringVar(&extrasJSON, "extras", "", "replace per-source tuning with this JSON object")
	cmd.Flags().BoolVar(&secretStdin, "secret-stdin", false,
		"rotate the auth secret, reading it from stdin (else "+SecretEnvVar+")")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the updated event source as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to `meho login`'s)")
	return cmd
}
