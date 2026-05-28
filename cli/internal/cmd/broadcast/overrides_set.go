// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newOverridesSetCmd() *cobra.Command {
	var (
		opIDPattern       string
		scopeField        string
		scopeValue        string
		detail            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "set",
		Short: "Create a broadcast-detail override rule",
		Long: "set calls POST /api/v1/broadcast/overrides to create a new " +
			"rule. --op-id-pattern accepts globs (* + literals) -- regex " +
			"chars are rejected by the backend with 422. --scope-field and " +
			"--scope-value must both be set or both omitted (the backend " +
			"422s a half-set pair). --detail is one of full|aggregate. A " +
			"duplicate rule (same pattern + scope triple) returns 409.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runOverridesSet(cmd, overridesSetOptions{
				OpIDPattern:       opIDPattern,
				ScopeField:        scopeField,
				ScopeValue:        scopeValue,
				Detail:            detail,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&opIDPattern, "op-id-pattern", "",
		"op_id glob (e.g. \"vault.kv.*\" or \"k8s.configmap.info\"); regex chars are rejected")
	cmd.Flags().StringVar(&scopeField, "scope-field", "",
		"scope field (one of: namespace, target_name); leave empty for an op-wide rule")
	cmd.Flags().StringVar(&scopeValue, "scope-value", "",
		"scope value (e.g. \"kube-system\"); required when --scope-field is set")
	cmd.Flags().StringVar(&detail, "detail", "",
		"override detail (full | aggregate)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the created row as JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("op-id-pattern")
	_ = cmd.MarkFlagRequired("detail")
	return cmd
}

type overridesSetOptions struct {
	OpIDPattern       string
	ScopeField        string
	ScopeValue        string
	Detail            string
	JSONOut           bool
	BackplaneOverride string
}

func runOverridesSet(cmd *cobra.Command, opts overridesSetOptions) error {
	// CLI-side --detail validation: mirrors the backend's Pydantic
	// Literal["full", "aggregate"] so the operator gets an immediate
	// rejection rather than a remote 422.
	if opts.Detail != "full" && opts.Detail != "aggregate" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--detail must be one of: full, aggregate"),
			opts.JSONOut,
		)
	}

	// CLI-side scope-pair check: both empty → op-wide rule (omit
	// scope_field / scope_value from the request entirely); both
	// set → scoped rule. A half-set pair is rejected at the CLI so
	// the operator gets an immediate error message rather than a
	// remote 422.
	scopeFieldSet := opts.ScopeField != ""
	scopeValueSet := opts.ScopeValue != ""
	if scopeFieldSet != scopeValueSet {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--scope-field and --scope-value must both be set or both be omitted"),
			opts.JSONOut,
		)
	}

	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}

	body := api.BroadcastOverrideCreate{
		OpIdPattern: opts.OpIDPattern,
		Detail:      api.BroadcastOverrideCreateDetail(opts.Detail),
	}
	if scopeFieldSet {
		sf := api.BroadcastOverrideCreateScopeField(opts.ScopeField)
		body.ScopeField = &sf
		body.ScopeValue = &opts.ScopeValue
	}
	entry, err := createOverride(cmd.Context(), client, body)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if entry == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 2xx but no JSON body decoded against BroadcastOverrideRead"),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printOverrideSummary(cmd.OutOrStdout(), entry)
	return nil
}

// createOverride drives the typed-client
// `CreateOverrideApiV1BroadcastOverridesPost` endpoint with a
// one-shot 401-retry around the underlying AuthedClient's refresh
// path. Non-2xx responses come back as `*httpResponseError` so the
// caller can route them through `renderHTTPStatus`. The generated
// client serialises `api.BroadcastOverrideCreate` and reads
// `JSON201` (the route returns 201 Created, not 200 OK).
func createOverride(
	ctx context.Context,
	client *api.AuthedClient,
	body api.BroadcastOverrideCreate,
) (*api.BroadcastOverrideRead, error) {
	params := &api.CreateOverrideApiV1BroadcastOverridesPostParams{}
	resp, err := client.CreateOverrideApiV1BroadcastOverridesPostWithResponse(ctx, params, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.CreateOverrideApiV1BroadcastOverridesPostWithResponse(ctx, params, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON201, nil
}

func printOverrideSummary(w io.Writer, e *api.BroadcastOverrideRead) {
	fmt.Fprintf(w, "%-16s %s\n", "id:", e.Id.String())
	fmt.Fprintf(w, "%-16s %s\n", "tenant_id:", e.TenantId.String())
	fmt.Fprintf(w, "%-16s %s\n", "op_id_pattern:", e.OpIdPattern)
	fmt.Fprintf(w, "%-16s %s\n", "scope_field:", strDerefOrDash(e.ScopeField))
	fmt.Fprintf(w, "%-16s %s\n", "scope_value:", strDerefOrDash(e.ScopeValue))
	fmt.Fprintf(w, "%-16s %s\n", "detail:", e.Detail)
	fmt.Fprintf(w, "%-16s %s\n", "created_by:", e.CreatedBySub)
	fmt.Fprintf(w, "%-16s %s\n", "created_at:", e.CreatedAt.Format("2006-01-02T15:04:05Z07:00"))
}
