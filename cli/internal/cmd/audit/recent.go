// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"github.com/spf13/cobra"
)

// recentDefaultSince is the operator-friendly "what happened today"
// shorthand the recent verb defaults to. Matches the same default
// the backend `who-touched` / `my-recent` routes ship.
const recentDefaultSince = "24h"

// newRecentCmd returns the `meho audit recent` command.
//
// CLI shape (per issue #467):
//
//	meho audit recent [--limit N] [--json] [--backplane <url>]
//
// Equivalent to `meho audit query --since 24h --limit N`. The verb
// exists as a separate cobra subcommand (rather than a hidden alias
// of `query`) so the help output cleanly enumerates the operator's
// daily-use shortcut alongside the full filter surface.
//
// Exit codes mirror `meho audit query`.
func newRecentCmd() *cobra.Command {
	var (
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "recent",
		Short: "Show the most recent audit rows in the operator's tenant",
		Long: "recent is a shortcut over `meho audit query --since 24h " +
			"--limit N`. It calls POST /api/v1/audit/query with the same " +
			"backend route the full filter uses; the only thing this verb " +
			"does is bind --since to 24h so operators don't have to type " +
			"it on the most common case. --limit caps the page size " +
			"(1..1000, server default 100). --json emits the raw " +
			"QueryResult.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			// Re-use the query runner. `since=24h` is the only filter
			// bound here; the operator can still reach the full filter
			// surface via `meho audit query` directly.
			return runQuery(cmd, queryOptions{
				Since:             recentDefaultSince,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows (1..1000, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw QueryResult JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}
