// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <session-id>",
		Short: "Show one stored agent session (GET /api/v1/sessions/{session_id})",
		Long: "show prints the index row of one session, the size and hash of each " +
			"file, and every stored upload. An upload in quarantine is marked; only " +
			"a tenant_admin can open it. Needs the operator role.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, args[0], jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "print the session as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by `meho login`)")
	return cmd
}

func runShow(cmd *cobra.Command, sessionID string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	resp, err := retryOn401(cmd.Context(), authed,
		func(ctx context.Context) (*api.ShowAgentSessionApiV1SessionsSessionIdGetResponse, error) {
			return authed.ShowAgentSessionApiV1SessionsSessionIdGetWithResponse(ctx, sessionID, nil)
		},
		func(r *api.ShowAgentSessionApiV1SessionsSessionIdGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printDetail(cmd, resp.JSON200)
	return nil
}

func printDetail(cmd *cobra.Command, s *api.SessionDetail) {
	out := cmd.OutOrStdout()
	when := func(t *time.Time) string {
		if t == nil {
			return "-"
		}
		return t.UTC().Format(time.RFC3339)
	}
	fmt.Fprintf(out, "session:      %s\n", s.SessionId)
	fmt.Fprintf(out, "project:      %s\n", s.Project)
	fmt.Fprintf(out, "app:          %s %s\n", s.App, orNone(s.AppVersion))
	fmt.Fprintf(out, "uploaded by:  %s\n", s.UploadedBySub)
	fmt.Fprintf(out, "state:        %s\n", s.State)
	fmt.Fprintf(out, "outcome:      %s\n", orNone(s.Outcome))
	fmt.Fprintf(out, "started:      %s\n", when(s.StartedAt))
	fmt.Fprintf(out, "ended:        %s\n", when(s.EndedAt))
	fmt.Fprintf(out, "last upload:  %s\n", s.LastSeenAt.UTC().Format(time.RFC3339))

	paths := make([]string, 0, len(s.Files))
	for p := range s.Files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	fmt.Fprintf(out, "\nfiles (%d):\n", len(paths))
	tw := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	for _, p := range paths {
		fmt.Fprintf(tw, "  %s\t%d bytes\tsha256 %s\n", p, s.Files[p].Size, s.Files[p].Sha256)
	}
	_ = tw.Flush()

	fmt.Fprintf(out, "\nuploads (%d):\n", len(s.Uploads))
	tw = tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	for _, u := range s.Uploads {
		mark := ""
		if u.Quarantined {
			mark = "in quarantine"
		}
		fmt.Fprintf(tw, "  #%d\t%s\t%d bytes\t%d replaced\t%s\n",
			u.Seq, u.UploadedAt.UTC().Format(time.RFC3339), u.ArchiveBytes, u.Redactions, mark)
	}
	_ = tw.Flush()
}
