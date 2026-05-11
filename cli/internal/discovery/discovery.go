// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package discovery implements Goal #11 spec section 5's
// server-driven subcommand discovery: the CLI fetches a manifest
// of dynamic subcommands from the backplane at startup and
// registers them into the cobra tree without a CLI binary release.
//
// v0.1 is scaffold-only. The backplane returns an empty manifest
// (the GET /api/v1/commands endpoint is a v0.2 coordination point
// with G2.2 / G2.7), so the live CLI never registers any extra
// commands. The scaffolding is in place so v0.2 can light up
// dynamic registration without restructuring the cobra root.
//
// The package is deliberately self-contained: it never imports
// internal/api or internal/auth so a discovery fetch can happen
// before login has produced a token (the /api/v1/commands endpoint
// is anonymous by design — the manifest is a public capability
// description, not a privileged operation).
package discovery

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// CommandManifest is the on-the-wire shape of GET /api/v1/commands.
// The schema is intentionally minimal in v0.1 — name + short + an
// optional nested subcommands array. v0.2 will add usage, flags,
// and per-command argument descriptors as operations land.
//
// Field names are stable across CLI releases: v0.2 backplanes
// emit this shape unchanged, and v0.1 CLIs run against v0.2
// backplanes ignore the new fields gracefully.
type CommandManifest struct {
	// Commands is the list of dynamic subcommands the backplane
	// advertises. Empty in v0.1.
	Commands []Command `json:"commands"`
}

// Command describes one dynamically-registered subcommand. The
// Subcommands field is recursive so a single fetch can describe a
// full sub-tree (e.g. `meho k8s deployment list`) without follow-up
// round trips.
type Command struct {
	// Name is the subcommand identifier — what the operator types
	// after `meho ` to invoke it. Required.
	Name string `json:"name"`
	// Short is the one-line description shown in `meho --help`.
	// Optional in the schema but populated by every realistic
	// backplane.
	Short string `json:"short,omitempty"`
	// Subcommands are nested children. Optional; empty in v0.1.
	Subcommands []Command `json:"subcommands,omitempty"`
}

// fetchTimeout is the upper bound on the manifest GET. Discovery
// is best-effort — the CLI must remain usable when the backplane
// is unreachable — so we cap the cost on every invocation rather
// than blocking on a hung TCP connection until the operator's own
// shell session expires.
const fetchTimeout = 5 * time.Second

// Endpoint is the relative path under the backplane that serves
// the manifest. Hoisted into a const so v0.2 coordination with
// G2.2 has a single source-of-truth value to align on.
const Endpoint = "/api/v1/commands"

// Fetch retrieves the manifest from the supplied backplane URL.
// Returns an empty manifest (not an error) on any non-2xx response
// or transport failure — the CLI falls back to its local-only
// subcommand set. Errors propagate only when the request body is
// 2xx but undecodable, which signals a backplane contract break
// the operator deserves to see.
//
// httpClient is injectable so tests pass httptest.Server.Client();
// pass nil for http.DefaultClient. The ctx-derived timeout
// (fetchTimeout) wraps whatever deadline the caller already set —
// the inner timeout never extends the outer one.
func Fetch(ctx context.Context, httpClient *http.Client, backplaneURL string) (*CommandManifest, error) {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	endpoint := strings.TrimRight(backplaneURL, "/") + Endpoint

	fetchCtx, cancel := context.WithTimeout(ctx, fetchTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(fetchCtx, http.MethodGet, endpoint, http.NoBody)
	if err != nil {
		return nil, fmt.Errorf("meho: build discovery request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		// Transport failure (DNS, connection refused, TLS
		// handshake). Return empty manifest, not an error, so the
		// CLI degrades gracefully — the operator running offline
		// or before login can still type `meho --help`.
		return &CommandManifest{}, nil
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode/100 != 2 {
		// Non-2xx (404 before G2.2 ships the endpoint, 502 when
		// the backplane is restarting). Degrade gracefully.
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 16*1024))
		return &CommandManifest{}, nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return nil, fmt.Errorf("meho: read discovery response: %w", err)
	}
	var manifest CommandManifest
	if err := json.Unmarshal(body, &manifest); err != nil {
		return nil, fmt.Errorf("meho: parse discovery response: %w", err)
	}
	return &manifest, nil
}

// Register grafts the manifest's commands onto rootCmd. Each
// dynamic command's RunE is a stub that prints a polite "operation
// not yet implemented locally" message — the v0.1 backplane never
// populates the manifest, so this path is exercised only in tests
// and by future v0.2 manifests that arrive at an older CLI.
//
// Returns an error when a manifest command collides with an
// already-registered local subcommand (login / status / version).
// Collisions are a backplane / CLI contract bug worth surfacing
// loudly; silently shadowing a built-in would be a security
// footgun if a misconfigured backplane ever advertised
// `name: "login"`.
func Register(rootCmd *cobra.Command, manifest *CommandManifest) error {
	if manifest == nil {
		return nil
	}
	existing := map[string]bool{}
	for _, c := range rootCmd.Commands() {
		existing[c.Name()] = true
	}
	var errs []error
	for _, c := range manifest.Commands {
		if existing[c.Name] {
			errs = append(errs, fmt.Errorf("dynamic command %q shadows a built-in subcommand", c.Name))
			continue
		}
		rootCmd.AddCommand(buildCommand(c))
	}
	if len(errs) > 0 {
		return errors.Join(errs...)
	}
	return nil
}

// buildCommand recursively turns a Command spec into a cobra
// command. Each leaf RunE is the v0.1 placeholder; non-leaf
// (sub-bearing) commands have no RunE so cobra prints the
// subcommand help when invoked without an arg.
func buildCommand(c Command) *cobra.Command {
	cmd := &cobra.Command{
		Use:   c.Name,
		Short: c.Short,
		// SilenceUsage matches the rest of the CLI — operator errors
		// shouldn't dump the usage wall.
		SilenceUsage: true,
	}
	if len(c.Subcommands) > 0 {
		for _, sub := range c.Subcommands {
			cmd.AddCommand(buildCommand(sub))
		}
		return cmd
	}
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		// v0.1 stub. The backplane never populates the manifest, so
		// this only fires for v0.2 backplanes hitting an older CLI
		// (or a test). The message points operators at the right
		// remedy: upgrade the CLI to consume the new operation.
		fmt.Fprintf(cmd.OutOrStdout(), "meho: %s is advertised by the backplane but not yet implemented in this CLI.\n", c.Name)
		fmt.Fprintf(cmd.OutOrStdout(), "Upgrade the meho CLI binary to gain this operation.\n")
		return nil
	}
	return cmd
}
