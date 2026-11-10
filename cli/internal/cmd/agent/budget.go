// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// NewBudgetRootCmd returns the `meho agent budget` parent command: set,
// show and clear the spend caps of an agent login. MEHO checks these caps
// before every hosted agent run and refuses a run that has reached one.
//
// Sub-commands:
//
//   - meho agent budget show <principal> [--window W] [--json]
//   - meho agent budget set <principal> --window W [--token-limit N] [--cost-limit USD] [--request-limit N] [--json]
//   - meho agent budget clear <principal> --window W [--json]
func NewBudgetRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "budget",
		Short: "Set, show and clear the spend caps of an agent login (tenant_admin)",
		Long: "Manage the spend caps of an agent login. A cap has up to three " +
			"limits for one window (daily, weekly or monthly): tokens, cost " +
			"in USD, and number of runs (requests). MEHO checks the caps " +
			"before every hosted agent run. A run that meets a cap is refused " +
			"with budget_exceeded. A cap stays in force in every later " +
			"window until you change or clear it. All sub-commands need " +
			"tenant_admin, and set and clear need a person (not an agent).",
		SilenceUsage: true,
	}
	cmd.AddCommand(newBudgetShowCmd())
	cmd.AddCommand(newBudgetSetCmd())
	cmd.AddCommand(newBudgetClearCmd())
	return cmd
}

var budgetWindows = []string{
	string(api.BudgetWindowKindDaily),
	string(api.BudgetWindowKindWeekly),
	string(api.BudgetWindowKindMonthly),
}

func parseBudgetWindow(window string) (api.BudgetWindowKind, error) {
	for _, w := range budgetWindows {
		if window == w {
			return api.BudgetWindowKind(w), nil
		}
	}
	return "", fmt.Errorf("--window must be one of: %s", strings.Join(budgetWindows, ", "))
}

// ---------------------------------------------------------------------------
// meho agent budget show
// ---------------------------------------------------------------------------

func newBudgetShowCmd() *cobra.Command {
	var (
		window            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <principal>",
		Short: "Show the caps of an agent login and what is used so far (tenant_admin)",
		Long: "show calls GET /api/v1/agent-budgets. It lists the caps of " +
			"the daily, weekly and monthly window, and what the login has " +
			"used so far in the current day, week and month. --window " +
			"shows one window. <principal> is the login of the agent, for " +
			"example agent:pod-reader.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			params := &api.ShowBudgetsApiV1AgentBudgetsGetParams{PrincipalSub: args[0]}
			if window != "" {
				w, err := parseBudgetWindow(window)
				if err != nil {
					return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
				}
				params.Window = &w
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := getBudgets(cmd.Context(), backplaneURL, params)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printBudgetTable(cmd.OutOrStdout(), resp.JSON200.Budgets)
			return nil
		},
	}
	cmd.Flags().StringVar(&window, "window", "", "show one window only: daily | weekly | monthly")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw AgentBudgetListResponse JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func getBudgets(ctx context.Context, backplaneURL string, params *api.ShowBudgetsApiV1AgentBudgetsGetParams) (*api.ShowBudgetsApiV1AgentBudgetsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowBudgetsApiV1AgentBudgetsGetResponse, error) {
			return authed.ShowBudgetsApiV1AgentBudgetsGetWithResponse(ctx, params)
		},
		func(r *api.ShowBudgetsApiV1AgentBudgetsGetResponse) int { return r.StatusCode() },
	)
}

// limitText renders "used / limit", or "used / no cap" when the limit is not set.
func limitText[T int | float32](used T, limit *T) string {
	if limit == nil {
		return fmt.Sprintf("%v / no cap", used)
	}
	return fmt.Sprintf("%v / %v", used, *limit)
}

func printBudgetTable(w io.Writer, budgets []api.AgentBudgetRead) {
	fmt.Fprintf(w, "%-8s %-22s %-22s %s\n", "WINDOW", "TOKENS (used / cap)", "COST USD (used / cap)", "REQUESTS (used / cap)")
	for _, b := range budgets {
		fmt.Fprintf(w, "%-8s %-22s %-22s %s\n", b.Window,
			limitText(b.TokensConsumed, b.TokenLimit),
			limitText(b.CostConsumed, b.CostLimit),
			limitText(b.RequestsConsumed, b.RequestLimit))
	}
}

// ---------------------------------------------------------------------------
// meho agent budget set
// ---------------------------------------------------------------------------

func newBudgetSetCmd() *cobra.Command {
	var (
		window            string
		tokenLimit        int
		costLimit         float64
		requestLimit      int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "set <principal>",
		Short: "Set the spend cap of an agent login for one window (tenant_admin)",
		Long: "set calls PUT /api/v1/agent-budgets. Give --window and at " +
			"least one of --token-limit, --cost-limit (USD) and " +
			"--request-limit. A limit you leave out stays as it is. A limit " +
			"of 0 stops the login from running at all. Example: meho agent " +
			"budget set agent:pod-reader --window daily --cost-limit 5 " +
			"--request-limit 200",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body, err := buildBudgetSetBody(args[0], window,
				optInt(cmd, "token-limit", tokenLimit),
				optFloat(cmd, "cost-limit", costLimit),
				optInt(cmd, "request-limit", requestLimit))
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := putBudget(cmd.Context(), backplaneURL, body)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			return renderBudgetEntry(cmd.OutOrStdout(), resp.JSON200, jsonOut, "set")
		},
	}
	cmd.Flags().StringVar(&window, "window", "", "daily | weekly | monthly (required)")
	cmd.Flags().IntVar(&tokenLimit, "token-limit", 0, "token cap for the window")
	cmd.Flags().Float64Var(&costLimit, "cost-limit", 0, "cost cap for the window, in USD")
	cmd.Flags().IntVar(&requestLimit, "request-limit", 0, "cap on the number of runs in the window")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw AgentBudgetRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func optInt(cmd *cobra.Command, name string, v int) *int {
	if !cmd.Flags().Changed(name) {
		return nil
	}
	return &v
}

func optFloat(cmd *cobra.Command, name string, v float64) *float32 {
	if !cmd.Flags().Changed(name) {
		return nil
	}
	f := float32(v)
	return &f
}

func buildBudgetSetBody(principal, window string, tokenLimit *int, costLimit *float32, requestLimit *int) (api.AgentBudgetSet, error) {
	w, err := parseBudgetWindow(window)
	if err != nil {
		return api.AgentBudgetSet{}, err
	}
	if tokenLimit == nil && costLimit == nil && requestLimit == nil {
		return api.AgentBudgetSet{}, fmt.Errorf("give at least one of --token-limit, --cost-limit, --request-limit")
	}
	if (tokenLimit != nil && *tokenLimit < 0) || (costLimit != nil && *costLimit < 0) ||
		(requestLimit != nil && *requestLimit < 0) {
		return api.AgentBudgetSet{}, fmt.Errorf("a limit cannot be negative")
	}
	return api.AgentBudgetSet{
		PrincipalSub: principal,
		Window:       w,
		TokenLimit:   tokenLimit,
		CostLimit:    costLimit,
		RequestLimit: requestLimit,
	}, nil
}

func putBudget(ctx context.Context, backplaneURL string, body api.AgentBudgetSet) (*api.SetBudgetApiV1AgentBudgetsPutResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.SetBudgetApiV1AgentBudgetsPutResponse, error) {
			return authed.SetBudgetApiV1AgentBudgetsPutWithResponse(ctx, nil, body)
		},
		func(r *api.SetBudgetApiV1AgentBudgetsPutResponse) int { return r.StatusCode() },
	)
}

func renderBudgetEntry(w io.Writer, entry *api.AgentBudgetRead, jsonOut bool, verb string) error {
	if entry == nil {
		return fmt.Errorf("backend returned an empty budget body")
	}
	if jsonOut {
		return output.PrintJSON(w, entry)
	}
	fmt.Fprintf(w, "%s %s cap for %s\n", verb, entry.Window, entry.PrincipalSub)
	printBudgetTable(w, []api.AgentBudgetRead{*entry})
	return nil
}

// ---------------------------------------------------------------------------
// meho agent budget clear
// ---------------------------------------------------------------------------

func newBudgetClearCmd() *cobra.Command {
	var (
		window            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "clear <principal>",
		Short: "Remove the spend cap of an agent login for one window (tenant_admin)",
		Long: "clear calls DELETE /api/v1/agent-budgets. It removes all " +
			"three limits of the window --window, from now on. What the " +
			"login has used so far is kept.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			w, err := parseBudgetWindow(window)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			params := &api.ClearBudgetApiV1AgentBudgetsDeleteParams{PrincipalSub: args[0], Window: w}
			resp, err := deleteBudget(cmd.Context(), backplaneURL, params)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
			}
			return renderBudgetEntry(cmd.OutOrStdout(), resp.JSON200, jsonOut, "cleared")
		},
	}
	cmd.Flags().StringVar(&window, "window", "", "daily | weekly | monthly (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw AgentBudgetRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func deleteBudget(ctx context.Context, backplaneURL string, params *api.ClearBudgetApiV1AgentBudgetsDeleteParams) (*api.ClearBudgetApiV1AgentBudgetsDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ClearBudgetApiV1AgentBudgetsDeleteResponse, error) {
			return authed.ClearBudgetApiV1AgentBudgetsDeleteWithResponse(ctx, params)
		},
		func(r *api.ClearBudgetApiV1AgentBudgetsDeleteResponse) int { return r.StatusCode() },
	)
}
