// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package startinggrants holds the --grants and --target flags that
// `meho agent-principal register` and `meho agent create` share. With
// them a person picks an agent's first grants when they create it:
//
//   - none: the agent runs nothing until an admin adds a grant.
//   - read-everything: safe operations run at once on every target;
//     everything else waits for a person's approval.
//   - read-targets: the same, only on the targets named with --target.
//
// The CLI never picks for the person. When MEHO needs a pick and the
// flag is missing, the backend answers 422 starting_grants_required and
// the CLI prints the three choices.
package startinggrants

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// The three sets, as the backend names them.
const (
	SetNone           = string(api.StartingGrantSetNone)
	SetReadEverything = string(api.StartingGrantSetReadEverything)
	SetReadTargets    = string(api.StartingGrantSetReadTargets)
)

// Choices is the text the CLI prints when a pick is missing or wrong.
const Choices = "pick the agent's first grants with --grants:\n" +
	"  --grants none                      the agent runs nothing until an admin adds a grant\n" +
	"  --grants read-everything           safe operations run at once on every target; everything else waits for a person's approval\n" +
	"  --grants read-targets --target <name> [--target <name> ...]\n" +
	"                                     the same, only on the targets you name"

const grantsFlagHelp = "the agent's first grants: none, read-everything or read-targets. " +
	"none: the agent runs nothing until an admin adds a grant. " +
	"read-everything: safe operations run at once on every target; everything else waits for a person's approval. " +
	"read-targets: the same, only on the targets you name with --target"

const targetFlagHelp = "a target name for --grants read-targets; repeat the flag for more targets"

// maxPages bounds the grant read after creation (500 grants per page).
const (
	pageSize = 500
	maxPages = 20
)

// Flags holds the values of --grants and --target.
type Flags struct {
	Set     string
	Targets []string
}

// AddFlags adds --grants and --target to cmd.
func AddFlags(cmd *cobra.Command, f *Flags) {
	cmd.Flags().StringVar(&f.Set, "grants", "", grantsFlagHelp)
	cmd.Flags().StringArrayVar(&f.Targets, "target", nil, targetFlagHelp)
}

// Body checks the flags and returns the pick to send. It returns nil
// when --grants was not given: the backend then decides whether it
// needs a pick. A wrong combination is a usage error.
func (f Flags) Body() (*api.StartingGrants, error) {
	set := strings.TrimSpace(f.Set)
	if set == "" {
		if len(f.Targets) > 0 {
			return nil, fmt.Errorf("--target needs --grants read-targets")
		}
		return nil, nil
	}
	switch set {
	case SetNone, SetReadEverything:
		if len(f.Targets) > 0 {
			return nil, fmt.Errorf("--target is only used with --grants read-targets, not with --grants %s", set)
		}
		return &api.StartingGrants{Set: api.StartingGrantSet(set)}, nil
	case SetReadTargets:
		if len(f.Targets) == 0 {
			return nil, fmt.Errorf("--grants read-targets needs at least one --target <name>")
		}
		targets := make([]string, 0, len(f.Targets))
		for _, name := range f.Targets {
			name = strings.TrimSpace(name)
			if name == "" {
				return nil, fmt.Errorf("--target must not be empty")
			}
			targets = append(targets, name)
		}
		return &api.StartingGrants{Set: api.StartingGrantSetReadTargets, Targets: &targets}, nil
	default:
		return nil, fmt.Errorf("unknown --grants value %q; %s", set, Choices)
	}
}

// RequestError turns a 422 answer about the pick into a CLI error. It
// returns nil for any other answer, so the caller's own mapping runs.
func RequestError(statusCode int, body []byte) *output.StructuredError {
	if statusCode != http.StatusUnprocessableEntity {
		return nil
	}
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil
	}
	var detail string
	if err := json.Unmarshal(env.Detail, &detail); err != nil {
		return nil
	}
	switch {
	case strings.HasPrefix(detail, "starting_grants_required"):
		return output.Usage("MEHO needs a starting grant pick for this agent. Nothing was created. " + Choices)
	case strings.HasPrefix(detail, "starting_grants_"):
		return output.Usage(detail)
	default:
		return nil
	}
}

// ReadGrants returns the live grants of principalSub, every page. The
// commands call it right after creation to show what the pick wrote.
func ReadGrants(ctx context.Context, authed *api.AuthedClient, principalSub string) ([]api.AgentGrantRead, error) {
	var grants []api.AgentGrantRead
	for page := 0; page < maxPages; page++ {
		sub, limit, offset := principalSub, pageSize, page*pageSize
		params := &api.ListGrantsApiV1AgentsGrantsGetParams{PrincipalSub: &sub, Limit: &limit, Offset: &offset}
		resp, err := authed.ListGrantsApiV1AgentsGrantsGetWithResponse(ctx, params)
		if err == nil && resp.StatusCode() == http.StatusUnauthorized {
			if rerr := authed.Refresh(ctx); rerr != nil {
				return nil, rerr
			}
			resp, err = authed.ListGrantsApiV1AgentsGrantsGetWithResponse(ctx, params)
		}
		if err != nil {
			return nil, err
		}
		if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
			return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode(), strings.TrimSpace(string(resp.Body)))
		}
		grants = append(grants, resp.JSON200.Grants...)
		if len(resp.JSON200.Grants) < pageSize {
			if grants == nil {
				grants = []api.AgentGrantRead{}
			}
			return grants, nil
		}
	}
	return nil, fmt.Errorf("the agent has more than %d grants; run `meho agent grant list --principal %s`",
		pageSize*maxPages, principalSub)
}

// WithGrants returns entry as a JSON object with a "grants" field. When
// the grants could not be read, "grants" is null and "grants_error"
// says why, so a failed read never looks like "no grants".
func WithGrants(entry any, grants []api.AgentGrantRead, readErr error) (map[string]any, error) {
	raw, err := json.Marshal(entry)
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	if readErr != nil {
		out["grants"] = nil
		out["grants_error"] = readErr.Error()
		return out, nil
	}
	out["grants"] = grants
	return out, nil
}

// PrintGrants writes the agent's grants for the human summary. A failed
// read is said in plain words, never shown as "no grants".
func PrintGrants(w io.Writer, principalSub string, grants []api.AgentGrantRead, readErr error) {
	if readErr != nil {
		fmt.Fprintf(w, "the agent was created, but its grants could not be read: %v\n", readErr)
		fmt.Fprintf(w, "check them with `meho agent grant list --principal %s`\n", principalSub)
		return
	}
	if len(grants) == 0 {
		fmt.Fprintf(w, "grants of %s: none\n", principalSub)
		return
	}
	fmt.Fprintf(w, "grants of %s:\n", principalSub)
	for _, g := range grants {
		fmt.Fprintf(w, "  %-36s op=%s target=%s verdict=%s\n", g.Id.String(), g.OpPattern, targetLabel(g.TargetScope), g.Verdict)
	}
}

func targetLabel(scope *string) string {
	if scope == nil || *scope == "" || *scope == "*" {
		return "every target"
	}
	return *scope
}
