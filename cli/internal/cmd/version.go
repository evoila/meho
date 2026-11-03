// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
	"github.com/evoila/meho/cli/internal/version"
)

// newVersionCmd returns the `meho version` subcommand. It prints the
// CLI build information. When a backplane is known (the --backplane
// flag, or the URL the last `meho login` stored), it also reads the
// backplane's GET /version and prints the backplane build and the
// connector packages its image carries (#5036). Without a known
// backplane the output is the single CLI line, as before.
func newVersionCmd() *cobra.Command {
	var backplaneOverride string
	cmd := &cobra.Command{
		Use:   "version",
		Short: "Print CLI version and build metadata, and the backplane's version and connector packages",
		Long: "version prints the CLI version, commit and build date.\n\n" +
			"When a backplane is known (--backplane, or the URL stored by the " +
			"last `meho login`), it also reads the backplane's /version endpoint " +
			"(no login needed) and prints the backplane build and the connector " +
			"packages its image carries. If the backplane cannot be reached, a " +
			"one-line warning goes to stderr and the command still succeeds.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			info := version.Get()
			// Route the version line through cmd.OutOrStdout() with
			// fmt.Fprintf — NOT cmd.Print/Printf/Println. In cobra
			// v1.10.2 (command.go:1434-1446) the Print* helpers
			// delegate to OutOrStderr(), so the un-configured binary
			// writes its version banner to stderr. Install scripts
			// and smoke tests parse stdout, so the output contract
			// for `meho version` is "version line on stdout, nothing
			// on stderr". The unit test below pins both halves.
			fmt.Fprintf(
				cmd.OutOrStdout(),
				"meho %s (commit %s, built %s)\n",
				info.Version, info.Commit, info.Date,
			)
			backplaneURL, err := versionBackplaneURL(backplaneOverride)
			if err != nil || backplaneURL == "" {
				return err
			}
			printBackplaneVersion(cmd.Context(), backplaneURL, cmd.OutOrStdout(), cmd.ErrOrStderr())
			return nil
		},
	}
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to read the version from (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// versionBackplaneURL returns the backplane `meho version` reads, or ""
// when none is known. An invalid --backplane value is an error; a
// missing or unreadable stored config is not, because `meho version`
// must work before the first login.
func versionBackplaneURL(override string) (string, error) {
	if override != "" {
		return normalizeBackplaneURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil || cfg.BackplaneURL == "" {
		return "", nil
	}
	u, err := normalizeBackplaneURL(cfg.BackplaneURL)
	if err != nil {
		return "", nil
	}
	return u, nil
}

// printBackplaneVersion writes the backplane build line and its
// connector packages to out. Best effort: when GET /version fails it
// writes one warning line to errOut and returns.
func printBackplaneVersion(ctx context.Context, backplaneURL string, out, errOut io.Writer) {
	ctx, cancel := context.WithTimeout(ctx, versionCheckTimeout)
	defer cancel()
	bp, err := version.FetchBackplane(ctx, &http.Client{}, backplaneURL)
	if err != nil {
		fmt.Fprintf(errOut, "warning: could not read the backplane version from %s: %s\n",
			backplaneURL, redactedError(err))
		return
	}
	release := "unknown"
	if bp.ChartVersion != nil && *bp.ChartVersion != "" {
		release = *bp.ChartVersion
	}
	fmt.Fprintf(out, "backplane %s (commit %s, built %s) at %s\n",
		release, bp.GitSHA, bp.BuildDate, backplaneURL)
	// A backplane older than #5036 sends no list; say nothing then.
	if bp.ConnectorPackages != nil {
		fmt.Fprint(out, output.FormatConnectorPackages("", bp.ConnectorPackages))
	}
}
