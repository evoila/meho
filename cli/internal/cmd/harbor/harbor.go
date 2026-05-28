// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package harbor hosts the cobra commands under `meho harbor ...` for
// G3.5-T10 (#622) of Initiative #368. v0.2 ships the operator-facing
// alias verbs over the Harbor 2.x read-only core ops and the robot
// lifecycle typed ops, each pre-baking connector_id="harbor-rest-2.x"
// so operators don't type the connector ID on every dispatch:
//
//   - `meho harbor about [--target T]`                         — GET:/api/v2.0/systeminfo
//   - `meho harbor health [--target T]`                        — GET:/api/v2.0/health
//   - `meho harbor project list [--target T]`                  — GET:/api/v2.0/projects
//   - `meho harbor project info <name> [--target T]`           — GET:/api/v2.0/projects/{project_name}
//   - `meho harbor repository list <project> [--target T]`     — GET:/api/v2.0/projects/{project_name}/repositories
//   - `meho harbor repository info <project> <repo> [--target T]` — per-repo detail
//   - `meho harbor artifact list <project> <repo> [--target T]`   — artifact list (tags+digests)
//   - `meho harbor artifact info <project> <repo> <ref> [--target T]` — artifact full metadata
//   - `meho harbor robot list [--target T]`                    — GET:/api/v2.0/robots
//   - `meho harbor robot create --name <n> --project <p> --duration <d>` — harbor.robot.create
//   - `meho harbor robot delete --project <p> --id <id>`       — harbor.robot.delete
//   - `meho harbor operation search "<query>"`                  — search pre-scoped
//   - `meho harbor operation call <op_id> ...`                  — call pre-scoped
//
// Every verb POSTs to `/api/v1/operations/call` (or GETs
// `/api/v1/operations/search` for the search wrapper) with
// connector_id="harbor-rest-2.x" pre-baked. No Harbor logic in the CLI;
// pure Cobra-over-HTTP per CLAUDE.md postulate 5.
package harbor

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho harbor ...` dispatches against.
const ConnectorID = "harbor-rest-2.x"

// NewRootCmd returns the `meho harbor` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "harbor",
		Short: "Pre-scoped CLI verbs for the harbor-rest-2.x connector",
		Long: "harbor is the operator-facing verb tree for the harbor-rest-2.x\n" +
			"connector. Each verb dispatches through POST /api/v1/operations/call\n" +
			"with connector_id=\"harbor-rest-2.x\" pre-baked so operators don't\n" +
			"type the connector ID on every command.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newHealthCmd())
	cmd.AddCommand(newProjectCmd())
	cmd.AddCommand(newRepositoryCmd())
	cmd.AddCommand(newArtifactCmd())
	cmd.AddCommand(newRobotCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	var apiErr *dispatch.APIResponseError
	if errors.As(err, &apiErr) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, apiErr.StatusCode, apiErr.Body)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

func loadParamsFlag(val string) (map[string]any, error) {
	if val == "" {
		return nil, nil
	}
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read params file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse params JSON: %w", err)
	}
	return m, nil
}

func jsonUnmarshalStrict(raw []byte, out any) error {
	return json.Unmarshal(raw, out)
}

func truncate(s string, maxLen int) string {
	if maxLen < 1 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen-1]) + "…"
}

// printErrorTrailer surfaces the dispatcher error + extras envelope.
func printErrorTrailer(w io.Writer, r *CallResult) {
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := dispatch.PrettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

// fallbackResultRender dumps raw result JSON when the typed decoder
// doesn't match the actual response shape.
func fallbackResultRender(w io.Writer, r *CallResult) {
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	pretty, err := dispatch.PrettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}

// strDeref dereferences an optional string pointer.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
