// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package eventsource

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAddCmd returns `meho event-source add <slug>` (alias `create`).
func newAddCmd() *cobra.Command {
	var (
		name              string
		kind              string
		authStrategy      string
		statusFlag        string
		extrasJSON        string
		secretStdin       bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:     "add <slug>",
		Aliases: []string{"create"},
		Short:   "Register a single event source (alias `create`)",
		Long: "add registers one event source in the operator's tenant via " +
			"POST /api/v1/event-sources. The <slug> is the URL routing token " +
			"the ingest endpoint resolves; it is globally unique. The auth " +
			"secret is never a flag: set " + SecretEnvVar + " or pass " +
			"--secret-stdin to pipe it in.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body := map[string]any{
				"slug":          args[0],
				"name":          name,
				"kind":          kind,
				"auth_strategy": authStrategy,
			}
			fs := cmd.Flags()
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
			return runCreate(cmd, body, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "human-readable name, unique within the tenant (required)")
	cmd.Flags().StringVar(&kind, "kind", "",
		"producer kind: alertmanager | grafana | vcf-operations | harbor | generic-json (required)")
	cmd.Flags().StringVar(&authStrategy, "auth-strategy", "",
		"auth strategy: hmac-sha256 | static-header | basic (required)")
	cmd.Flags().StringVar(&statusFlag, "status", "active", "initial status: active | paused")
	cmd.Flags().StringVar(&extrasJSON, "extras", "",
		"per-source tuning as a JSON object (body cap, rate limit, replay window, dedupe)")
	cmd.Flags().BoolVar(&secretStdin, "secret-stdin", false,
		"read the auth secret from stdin (else "+SecretEnvVar+"); never a flag value")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the created event source as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by `meho login`)")
	_ = cmd.MarkFlagRequired("name")
	_ = cmd.MarkFlagRequired("kind")
	_ = cmd.MarkFlagRequired("auth-strategy")
	return cmd
}

func runCreate(cmd *cobra.Command, body map[string]any, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("marshal request body: %v", err)), jsonOut)
	}
	raw, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodPost, "/api/v1/event-sources", payload)
	if err != nil {
		return renderRequestErr(cmd, backplaneURL, err, jsonOut)
	}
	var es EventSource
	if err := json.Unmarshal(raw, &es); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode created event source: %v", err)), jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), &es)
	}
	printEventSource(cmd.OutOrStdout(), &es)
	return nil
}

// printEventSource renders one source as a stable key-value summary. It
// prints secret_ref (the Vault path) but never a secret value -- the read
// model does not carry one.
func printEventSource(w io.Writer, es *EventSource) {
	fmt.Fprintf(w, "%-16s %s\n", "slug:", es.Slug)
	fmt.Fprintf(w, "%-16s %s\n", "name:", es.Name)
	fmt.Fprintf(w, "%-16s %s\n", "kind:", es.Kind)
	fmt.Fprintf(w, "%-16s %s\n", "auth_strategy:", es.AuthStrategy)
	fmt.Fprintf(w, "%-16s %s\n", "status:", es.Status)
	if ref := strDeref(es.SecretRef); ref != "" {
		fmt.Fprintf(w, "%-16s %s\n", "secret_ref:", ref)
	} else {
		fmt.Fprintf(w, "%-16s <none>\n", "secret_ref:")
	}
	fmt.Fprintf(w, "%-16s %s\n", "created_by:", es.CreatedBySub)
}
