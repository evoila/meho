// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package tenants

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newModelPricesCmd returns `meho tenants model-prices`.
//
// A tenant admin gives an agent model that MEHO has no price for (an open
// model on the tenant's own model server, for example) a price in USD per
// million tokens. Runs on that model then get a cost, and the cost counts
// against the spend limits. A model MEHO already has a price for keeps it.
func newModelPricesCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "model-prices",
		Short: "Prices for agent models MEHO has no price for (tenant_admin)",
		Long: "List, set and remove the tenant's prices for agent models that MEHO has no " +
			"price for. A run on such a model uses the price for its cost, and the cost counts " +
			"against the spend limits. Without a price, the run costs 0.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newModelPricesListCmd())
	cmd.AddCommand(newModelPricesSetCmd())
	cmd.AddCommand(newModelPricesRemoveCmd())
	return cmd
}

type modelPricesOptions struct {
	input             string
	output            string
	jsonOut           bool
	backplaneOverride string
}

func addCommonFlags(cmd *cobra.Command, opts *modelPricesOptions) {
	cmd.Flags().BoolVar(&opts.jsonOut, "json", false,
		"emit the tenant's prices as JSON instead of a table")
	cmd.Flags().StringVar(&opts.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
}

func newModelPricesListCmd() *cobra.Command {
	var opts modelPricesOptions
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List the tenant's model prices (tenant_admin)",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runModelPrices(cmd, opts, nil)
		},
	}
	addCommonFlags(cmd, &opts)
	return cmd
}

func newModelPricesSetCmd() *cobra.Command {
	var opts modelPricesOptions
	cmd := &cobra.Command{
		Use:   "set <model> --input <usd> --output <usd>",
		Short: "Set the price of one model, in USD per million tokens (tenant_admin)",
		Long: "set gives <model> a price in USD per million tokens. <model> is the model as " +
			"an agent run shows it: provider:model (openai:my-model) or the model name alone. " +
			"MEHO refuses a model it already has a price for.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body, err := buildModelPriceBody(args[0], &opts)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.jsonOut)
			}
			return runModelPrices(cmd, opts, body)
		},
	}
	cmd.Flags().StringVar(&opts.input, "input", "", "price of one million input tokens, in USD")
	cmd.Flags().StringVar(&opts.output, "output", "", "price of one million output tokens, in USD")
	addCommonFlags(cmd, &opts)
	return cmd
}

func newModelPricesRemoveCmd() *cobra.Command {
	var opts modelPricesOptions
	cmd := &cobra.Command{
		Use:           "remove <model>",
		Short:         "Remove the price of one model (tenant_admin)",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body, err := buildModelPriceBody(args[0], nil)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.jsonOut)
			}
			return runModelPrices(cmd, opts, body)
		},
	}
	addCommonFlags(cmd, &opts)
	return cmd
}

// buildModelPriceBody builds the PATCH body. A nil price removes the model's
// price (JSON null). The prices go as strings, so the backplane reads the
// exact decimal the operator typed.
func buildModelPriceBody(model string, price *modelPricesOptions) (map[string]any, error) {
	if model == "" {
		return nil, fmt.Errorf("<model> must not be empty")
	}
	if price == nil {
		return map[string]any{"model": model, "price": nil}, nil
	}
	if price.input == "" || price.output == "" {
		return nil, fmt.Errorf("set needs both --input and --output")
	}
	return map[string]any{
		"model": model,
		"price": map[string]string{
			"input_per_million":  price.input,
			"output_per_million": price.output,
		},
	}, nil
}

// runModelPrices reads the prices (body nil) or sends one change, then prints
// the tenant's prices.
func runModelPrices(cmd *cobra.Command, opts modelPricesOptions, body map[string]any) error {
	backplaneURL, err := backplane.Resolve(opts.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.jsonOut)
	}
	var payload []byte
	if body != nil {
		if payload, err = json.Marshal(body); err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("encode request body: %v", err)), opts.jsonOut)
		}
	}
	resp, err := callModelPrices(cmd.Context(), backplaneURL, payload)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.jsonOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode, resp.Body, opts.jsonOut)
	}
	var prices api.TenantModelPrices
	if err := json.Unmarshal(resp.Body, &prices); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode prices response: %v", err)), opts.jsonOut)
	}
	if opts.jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), prices)
	}
	printModelPrices(cmd.OutOrStdout(), &prices)
	return nil
}

func callModelPrices(ctx context.Context, backplaneURL string, payload []byte) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		if payload == nil {
			return authed.GetModelPricesApiV1TenantsModelPricesGet(
				ctx, &api.GetModelPricesApiV1TenantsModelPricesGetParams{})
		}
		return authed.UpdateModelPriceApiV1TenantsModelPricesPatchWithBody(
			ctx,
			&api.UpdateModelPriceApiV1TenantsModelPricesPatchParams{},
			"application/json",
			bytes.NewReader(payload),
		)
	})
}

// printModelPrices renders the tenant's prices as a table.
func printModelPrices(w io.Writer, p *api.TenantModelPrices) {
	if p == nil {
		return
	}
	if len(p.Prices) == 0 {
		fmt.Fprintln(w, "no model prices set")
		return
	}
	fmt.Fprintf(w, "%-40s %16s %16s\n", "MODEL", "INPUT USD/1M", "OUTPUT USD/1M")
	for _, entry := range p.Prices {
		fmt.Fprintf(w, "%-40s %16s %16s\n", entry.Model, entry.InputPerMillion, entry.OutputPerMillion)
	}
}
