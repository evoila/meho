// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
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

// noGovernedSubopsReason is the `reason` the governed-subops route puts
// in its 404 body when the op has no child steps (#3349).
const noGovernedSubopsReason = "no_governed_subops"

// newSubopsCmd returns the `meho operation subops <op_id>` command (#4983).
//
//	meho operation subops <op_id> \
//	  [--connector <connector_id>]   # which connector to ask for
//	  [--json]                       # the answer as it came
//	  [--backplane <url>]
//
// Exit codes:
//   - 0   child steps listed, or the op has no child steps
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response
func newSubopsCmd() *cobra.Command {
	var (
		connectorID       string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "subops <op_id>",
		Short: "List the child steps of a multi-step operation and whether a grant may cover each",
		Long: "subops calls GET /api/v1/operations/governed-subops and lists the " +
			"child steps a multi-step operation (a composite) may run. Each row " +
			"shows the step's op id, its risk level, and whether a standing grant " +
			"may cover it (with the reason when not). A grant on the composite does " +
			"not cover its steps: an unattended service principal needs its own " +
			"grant for each step. --connector asks for one connector id; without it " +
			"the backplane uses the connector the composite belongs to. An op with " +
			"no child steps prints a short note and exits 0.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSubops(cmd, args[0], connectorID, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&connectorID, "connector", "",
		"connector id to ask for (e.g. vmware-rest-8.0); default: the composite's own connector")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"print the backplane's answer as it came instead of the table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSubops(cmd *cobra.Command, opID, connectorID string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	resp, err := getGovernedSubops(cmd.Context(), client, opID, connectorID)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if resp.StatusCode() == http.StatusNotFound {
		if jsonOut {
			_, werr := cmd.OutOrStdout().Write(append(resp.Body, '\n'))
			return werr
		}
		fmt.Fprintf(cmd.OutOrStdout(), "%s has no child steps that need their own grant.\n", opID)
		return nil
	}
	if jsonOut {
		_, werr := cmd.OutOrStdout().Write(append(resp.Body, '\n'))
		return werr
	}
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against GovernedSubopsResponse"),
			jsonOut,
		)
	}
	printSubopsTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// getGovernedSubops calls the governed-subops route through the
// generated client with the one-shot 401 refresh. It returns the
// response for a 200 and for the 404 `no_governed_subops` answer (a
// normal answer: the op has no child steps); every other non-2xx
// comes back as *apiResponseError.
func getGovernedSubops(
	ctx context.Context,
	client operationsAPI,
	opID, connectorID string,
) (*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse, error) {
	params := &api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetParams{OpId: opID}
	if connectorID != "" {
		c := connectorID
		params.ConnectorId = &c
	}
	resp, err := client.GetGovernedSubopsApiV1OperationsGovernedSubopsGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetGovernedSubopsApiV1OperationsGovernedSubopsGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	switch {
	case resp.StatusCode() == http.StatusOK:
		return resp, nil
	case resp.StatusCode() == http.StatusNotFound && isNoGovernedSubops(resp.Body):
		return resp, nil
	default:
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
}

// isNoGovernedSubops reports whether a 404 body is the route's
// "this op has no child steps" answer. Any other 404 (for example a
// backplane without the route) stays an error.
func isNoGovernedSubops(body []byte) bool {
	var env struct {
		Detail struct {
			Reason string `json:"reason"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return false
	}
	return env.Detail.Reason == noGovernedSubopsReason
}

// printSubopsTable prints one row per child step: op id, risk level
// ("unknown" when the step has no descriptor), and whether a grant may
// cover it. The reason is printed whole so the operator knows why a
// step still needs a person.
func printSubopsTable(w io.Writer, r *api.GovernedSubopsResponse) {
	fmt.Fprintf(w, "%s (connector %s): %d child step(s); each needs its own grant\n",
		r.OpId, r.ConnectorId, len(r.GovernedSubops))
	if len(r.GovernedSubops) == 0 {
		return
	}
	fmt.Fprintf(w, "%-48s %-12s %s\n", "OP_ID", "RISK", "GRANT")
	for _, s := range r.GovernedSubops {
		risk := strDeref(s.SafetyLevel)
		if risk == "" {
			risk = "unknown"
		}
		grant := "yes"
		if !s.Grantable {
			grant = "no"
			if reason := strDeref(s.UngrantableReason); reason != "" {
				grant = "no: " + reason
			}
		}
		fmt.Fprintf(w, "%-48s %-12s %s\n", s.OpId, risk, grant)
	}
}
