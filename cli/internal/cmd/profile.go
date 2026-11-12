// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// applyProfile picks the named login for this command: the global
// --profile flag, else MEHO_PROFILE, else the default profile. It puts
// the name on the command context. Every place that reads or writes a
// login (login, the authed client, status --watch) takes the name from
// there, so each profile has its own stored login.
func applyProfile(cmd *cobra.Command) error {
	flagValue, changed := "", false
	if f := cmd.Root().PersistentFlags().Lookup("profile"); f != nil {
		flagValue, changed = f.Value.String(), f.Changed
	}
	name, err := auth.ResolveProfile(flagValue, changed, strings.TrimSpace(os.Getenv(auth.ProfileEnv)))
	if err != nil {
		jsonOut := false
		if f := cmd.Flags().Lookup("json"); f != nil {
			jsonOut = f.Value.String() == "true"
		}
		return output.RenderError(cmd.ErrOrStderr(), output.Usage(err.Error()), jsonOut)
	}
	cmd.SetContext(auth.WithProfile(cmd.Context(), name))
	return nil
}

// printProfile writes the active profile as the first line of the
// human `meho status` output. The default profile prints nothing, so
// the output of a person who never uses profiles is exactly as before
// (scripts read it).
func printProfile(w io.Writer, cmd *cobra.Command) {
	if name := auth.ProfileFrom(cmd.Context()); name != auth.DefaultProfile {
		fmt.Fprintf(w, "Profile: %s\n", name)
	}
}
