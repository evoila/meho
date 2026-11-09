// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package settings

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

// newHistoryCmd returns `meho settings history`.
//
// Exit codes: 0 listed (also when empty), 2 auth_expired, 3 unreachable,
// 4 unexpected, 5 insufficient_role.
func newHistoryCmd() *cobra.Command {
	var jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{
		Use:   "history",
		Short: "List the applied settings imports, newest first",
		Long: "history calls GET /api/v1/settings/history. Every applied import " +
			"is one revision: who, when, and how much changed per section. " +
			"--json prints the endpoint's answer, with each revision's diff.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := ResolveBackplane(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			resp, err := fetchHistory(cmd.Context(), backplaneURL)
			if err != nil {
				return RenderError(cmd, backplaneURL, err, jsonOut)
			}
			if jsonOut {
				printRaw(cmd.OutOrStdout(), resp.Body)
				return nil
			}
			if resp.JSON200 == nil {
				return RenderError(cmd, backplaneURL,
					&HTTPError{Status: resp.StatusCode(), Body: resp.Body}, false)
			}
			printHistory(cmd.OutOrStdout(), *resp.JSON200)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "print the endpoint's answer as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by 'meho login')")
	return cmd
}

func fetchHistory(ctx context.Context, backplaneURL string) (*api.HistoryApiV1SettingsHistoryGetResponse, error) {
	resp, err := call(ctx, backplaneURL,
		func(ctx context.Context, authed *api.AuthedClient) (*api.HistoryApiV1SettingsHistoryGetResponse, error) {
			return authed.HistoryApiV1SettingsHistoryGetWithResponse(ctx, &api.HistoryApiV1SettingsHistoryGetParams{})
		},
		func(r *api.HistoryApiV1SettingsHistoryGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &HTTPError{Status: resp.StatusCode(), Body: resp.Body}
	}
	return resp, nil
}

func printHistory(w io.Writer, revisions []api.SettingsRevision) {
	if len(revisions) == 0 {
		fmt.Fprintln(w, "No settings import has been applied yet.")
		return
	}
	table := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(table, "WHEN\tWHO\tCHANGED\tREVISION")
	for _, revision := range revisions {
		fmt.Fprintf(table, "%s\t%s\t%s\t%s\n",
			revision.Ts.UTC().Format(time.RFC3339), revision.ActorSub,
			summaryText(revision.Summary), revision.Id)
	}
	_ = table.Flush()
}

// summaryText reads a revision's {section: {added, changed}} summary.
func summaryText(summary map[string]any) string {
	parts := []string{}
	for _, section := range []string{"tenants", "policies", "conventions", "targets"} {
		raw, err := json.Marshal(summary[section])
		if err != nil {
			continue
		}
		var counts struct {
			Added   int `json:"added"`
			Changed int `json:"changed"`
		}
		if json.Unmarshal(raw, &counts) != nil || counts.Added+counts.Changed == 0 {
			continue
		}
		parts = append(parts, fmt.Sprintf("%s +%d ~%d", section, counts.Added, counts.Changed))
	}
	if len(parts) == 0 {
		return "-"
	}
	return strings.Join(parts, ", ")
}
